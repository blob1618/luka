"""Webhook mode — simulates the full dispatcher pipeline."""

import os
import time
from dataclasses import dataclass

import redis.asyncio as redis

from app.services.dispatcher import process_incoming_message
from app.api.whatsapp import WhatsAppImage
from app.services.llm import LLMService
from app.services.conversation import (
    ConversationHistoryService,
    ConversationService,
    _memory_key,
)
from app.services.webhook_idempotency import _visible_reply_text
from testing.config.settings import set_model_env


@dataclass
class WebhookModeResult:
    """Result of a full webhook simulation."""
    reply_text: str
    raw_llm_response: dict | None
    service_invoked: str | None
    intent: str | None
    latency_ms: float
    provider: str
    prompt_path: str
    redis_state: dict | None
    model: str = ""
    memory: list[dict[str, str]] | None = None
    memory_ttl_seconds: int | None = None
    image_png: bytes | None = None


class WebhookModeService:
    """Simulates the full webhook dispatch pipeline without HTTP."""

    async def send_message(
        self,
        text: str,
        phone: str,
        provider: str,
        prompt_path: str,
        model: str = "",
    ) -> WebhookModeResult:
        """
        Send a message through the full dispatcher pipeline.

        Configures the provider and prompt, invokes the dispatcher,
        captures Redis state, and measures latency.
        """
        os.environ["LLM_PROVIDER"] = provider
        set_model_env(provider, model)
        LLMService.reset_provider()
        LLMService.set_prompt_path(prompt_path)

        redis_client = None
        try:
            redis_client = redis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379"),
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            await redis_client.ping()
        except Exception:
            if redis_client is not None:
                try:
                    await redis_client.aclose()
                except Exception:
                    pass
            redis_client = None

        try:
            start = time.perf_counter()

            history: list[dict[str, str]] = []
            if redis_client is not None:
                try:
                    recent = await ConversationHistoryService.get_recent(
                        redis_client, phone
                    )
                    history = [message.to_dict() for message in recent]
                except Exception:
                    history = []

            dispatch_result = await process_incoming_message(
                sender_phone=phone,
                text_body=text,
                whatsapp_message_id=None,
                conversation_history=history,
            )
            latency_ms = (time.perf_counter() - start) * 1000

            memory = None
            memory_ttl_seconds = None
            if redis_client is not None:
                try:
                    if getattr(dispatch_result, "clear_memory", False):
                        await ConversationHistoryService.clear(redis_client, phone)
                    else:
                        await ConversationHistoryService.append_exchange(
                            redis_client,
                            phone,
                            text,
                            _visible_reply_text(dispatch_result),
                            message_id=None,
                        )
                    final_history = await ConversationHistoryService.get_recent(
                        redis_client, phone
                    )
                    memory = [message.to_dict() for message in final_history]
                    ttl = int(await redis_client.ttl(_memory_key(phone)))
                    memory_ttl_seconds = ttl if ttl >= 0 else None
                except Exception:
                    memory = None
                    memory_ttl_seconds = None

            # Capture current Redis state for debug
            redis_state = None
            try:
                state = await ConversationService.get_state(phone)
                redis_state = state.to_dict()
            except Exception:
                redis_state = {"error": "Could not read Redis state"}

            return WebhookModeResult(
                reply_text=dispatch_result.reply_text,
                raw_llm_response=dispatch_result.raw_llm_response,
                service_invoked=dispatch_result.service_invoked,
                intent=dispatch_result.intent,
                latency_ms=latency_ms,
                provider=provider,
                prompt_path=prompt_path,
                redis_state=redis_state,
                model=model,
                memory=memory,
                memory_ttl_seconds=memory_ttl_seconds,
                image_png=(
                    dispatch_result.reply_message.content
                    if isinstance(dispatch_result.reply_message, WhatsAppImage)
                    else None
                ),
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return WebhookModeResult(
                reply_text=f"Error del dispatcher: {type(exc).__name__}: {exc}",
                raw_llm_response=None,
                service_invoked=None,
                intent=None,
                latency_ms=latency_ms,
                provider=provider,
                prompt_path=prompt_path,
                redis_state=None,
                model=model,
                memory=None,
                memory_ttl_seconds=None,
                image_png=None,
            )
        finally:
            if redis_client is not None:
                try:
                    await redis_client.aclose()
                except Exception:
                    pass
