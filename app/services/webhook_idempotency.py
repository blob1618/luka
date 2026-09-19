"""Atomic idempotency for inbound WhatsApp messages."""

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

from app.api.whatsapp import WhatsAppList, WhatsAppReplyButtons, WhatsAppText
from app.services.conversation import ConversationHistoryService


PROCESSING_TTL_SECONDS = 15 * 60
COMPLETED_TTL_SECONDS = 48 * 60 * 60


class IdempotencyUnavailable(RuntimeError):
    """Raised when an inbound message cannot be claimed safely."""


@dataclass(frozen=True)
class InboundMessageClaim:
    message_id: str
    key: str
    token: str

    @property
    def processing_value(self) -> str:
        return f"processing:{self.token}"


class WebhookIdempotencyService:
    @staticmethod
    def _key(message_id: str) -> str:
        digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
        return f"whatsapp:inbound:{digest}"

    @classmethod
    async def claim(cls, client: Any, message_id: str) -> InboundMessageClaim | None:
        if client is None:
            raise IdempotencyUnavailable("Redis client is not initialized")
        normalized_id = str(message_id or "").strip()
        if not normalized_id:
            raise IdempotencyUnavailable("WhatsApp message ID is required")

        claim = InboundMessageClaim(
            message_id=normalized_id,
            key=cls._key(normalized_id),
            token=secrets.token_urlsafe(18),
        )
        try:
            acquired = await client.set(
                claim.key,
                claim.processing_value,
                nx=True,
                ex=PROCESSING_TTL_SECONDS,
            )
        except Exception as exc:
            raise IdempotencyUnavailable("Could not claim inbound message") from exc
        return claim if acquired else None

    @staticmethod
    async def complete(client: Any, claim: InboundMessageClaim) -> bool:
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            redis.call('SET', KEYS[1], 'completed', 'EX', ARGV[2])
            return 1
        end
        return 0
        """
        try:
            result = await client.eval(
                script,
                1,
                claim.key,
                claim.processing_value,
                COMPLETED_TTL_SECONDS,
            )
        except Exception as exc:
            raise IdempotencyUnavailable("Could not complete inbound message") from exc
        return bool(result)

    @staticmethod
    async def release(client: Any, claim: InboundMessageClaim) -> bool:
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        try:
            result = await client.eval(
                script,
                1,
                claim.key,
                claim.processing_value,
            )
        except Exception as exc:
            raise IdempotencyUnavailable("Could not release inbound message") from exc
        return bool(result)


def _visible_reply_text(result: Any) -> str | None:
    reply = getattr(result, "reply_message", None)
    if reply is None:
        reply = getattr(result, "reply_text", None)
    if isinstance(reply, str):
        return reply
    if isinstance(reply, WhatsAppText):
        return reply.body
    if isinstance(reply, WhatsAppReplyButtons):
        options = " | ".join(button.title for button in reply.buttons)
        return f"{reply.body}\nOpciones: {options}"
    if isinstance(reply, WhatsAppList):
        options = " | ".join(
            row.title
            for section in reply.sections
            for row in section.rows
        )
        return f"{reply.body}\nOpciones: {options}"
    return None


async def process_inbound_message_once(
    *,
    redis_client: Any,
    sender_phone: str,
    whatsapp_message_id: str,
    process_message,
    send_message,
) -> str:
    """Claim, process and reply to any inbound message at most once."""
    claim = await WebhookIdempotencyService.claim(redis_client, whatsapp_message_id)
    if claim is None:
        print(
            "[INBOUND_MESSAGE]",
            f"message_id={whatsapp_message_id}",
            "status=duplicate",
        )
        return "duplicate"

    print(
        "[INBOUND_MESSAGE]",
        f"message_id={whatsapp_message_id}",
        "status=claimed",
    )
    send_succeeded = False
    try:
        result = await process_message()
        reply = getattr(result, "reply_message", None)
        if reply is None:
            reply = getattr(result, "reply_text", None)
        if reply:
            send_result = await send_message(sender_phone, reply)
            send_succeeded = send_result is not False
            if send_result is False:
                raise RuntimeError("WhatsApp reply could not be sent")

        await WebhookIdempotencyService.complete(redis_client, claim)
        print(
            "[INBOUND_MESSAGE]",
            f"message_id={whatsapp_message_id}",
            "status=completed",
        )
        return "completed"
    except Exception:
        if not send_succeeded:
            try:
                await WebhookIdempotencyService.release(redis_client, claim)
            except IdempotencyUnavailable as release_error:
                print(
                    "[INBOUND_MESSAGE]",
                    f"message_id={whatsapp_message_id}",
                    "status=release_failed",
                    f"error={type(release_error).__name__}",
                )
        raise


async def process_text_message_once(
    *,
    redis_client: Any,
    sender_phone: str,
    text_body: str,
    whatsapp_message_id: str,
    process_message,
    send_message,
) -> str:
    processed_result = None

    async def process_text():
        nonlocal processed_result
        history = await ConversationHistoryService.get_recent(
            redis_client,
            sender_phone,
        )
        processed_result = await process_message(
            sender_phone=sender_phone,
            text_body=text_body,
            whatsapp_message_id=whatsapp_message_id,
            conversation_history=[message.to_dict() for message in history],
        )
        return processed_result

    status = await process_inbound_message_once(
        redis_client=redis_client,
        sender_phone=sender_phone,
        whatsapp_message_id=whatsapp_message_id,
        process_message=process_text,
        send_message=send_message,
    )
    if status == "completed" and processed_result is not None:
        await ConversationHistoryService.append_exchange(
            redis_client,
            sender_phone,
            text_body,
            _visible_reply_text(processed_result),
        )
    return status


async def process_interactive_message_once(
    *,
    redis_client: Any,
    interactive_reply,
    process_reply,
    send_message,
) -> str:
    processed_result = None

    async def process_interactive():
        nonlocal processed_result
        processed_result = await process_reply(
            sender_phone=interactive_reply.sender_phone,
            option_id=interactive_reply.option_id,
            reply_type=interactive_reply.reply_type,
            whatsapp_message_id=interactive_reply.message_id,
        )
        return processed_result

    status = await process_inbound_message_once(
        redis_client=redis_client,
        sender_phone=interactive_reply.sender_phone,
        whatsapp_message_id=interactive_reply.message_id,
        process_message=process_interactive,
        send_message=send_message,
    )
    if status == "completed" and processed_result is not None:
        await ConversationHistoryService.append_exchange(
            redis_client,
            interactive_reply.sender_phone,
            interactive_reply.title or "",
            _visible_reply_text(processed_result),
        )
    return status
