import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.webhook_idempotency import (
    WebhookIdempotencyService,
    process_interactive_message_once,
    process_text_message_once,
)
from app.api.whatsapp import InboundInteractiveReply, WhatsAppText
from app.services.conversation import ConversationHistoryService

from tests.conftest import FakeRedis

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_claim_is_atomic_for_same_message_id():
    redis = FakeRedis()
    first = await WebhookIdempotencyService.claim(redis, "wamid.1")
    second = await WebhookIdempotencyService.claim(redis, "wamid.1")

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_concurrent_duplicate_is_processed_and_sent_once():
    redis = FakeRedis()
    started = asyncio.Event()
    resume = asyncio.Event()

    async def process_message(**_kwargs):
        started.set()
        await resume.wait()
        return SimpleNamespace(reply_text="hola")

    send_message = AsyncMock(return_value=True)
    first = asyncio.create_task(
        process_text_message_once(
            redis_client=redis,
            sender_phone="5491111111111",
            text_body="hola",
            whatsapp_message_id="wamid.cold-start",
            process_message=process_message,
            send_message=send_message,
        )
    )
    await started.wait()

    duplicate = await process_text_message_once(
        redis_client=redis,
        sender_phone="5491111111111",
        text_body="hola",
        whatsapp_message_id="wamid.cold-start",
        process_message=process_message,
        send_message=send_message,
    )
    resume.set()

    assert duplicate == "duplicate"
    assert await first == "completed"
    send_message.assert_awaited_once_with("5491111111111", "hola")


@pytest.mark.asyncio
async def test_failed_send_releases_claim_for_retry():
    redis = FakeRedis()
    process_message = AsyncMock(return_value=SimpleNamespace(reply_text="hola"))

    with pytest.raises(RuntimeError, match="could not be sent"):
        await process_text_message_once(
            redis_client=redis,
            sender_phone="5491111111111",
            text_body="hola",
            whatsapp_message_id="wamid.retry",
            process_message=process_message,
            send_message=AsyncMock(return_value=False),
        )

    retry = await WebhookIdempotencyService.claim(redis, "wamid.retry")
    assert retry is not None


@pytest.mark.asyncio
async def test_interactive_reply_is_processed_and_sent_once():
    redis = FakeRedis()
    reply = InboundInteractiveReply(
        message_id="wamid.interactive",
        sender_phone="5491111111111",
        reply_type="button_reply",
        option_id="flow.v1.node.confirm",
    )
    process_reply = AsyncMock(
        return_value=SimpleNamespace(reply_message=WhatsAppText("Listo"))
    )
    send_message = AsyncMock(return_value=True)

    first = await process_interactive_message_once(
        redis_client=redis,
        interactive_reply=reply,
        process_reply=process_reply,
        send_message=send_message,
    )
    duplicate = await process_interactive_message_once(
        redis_client=redis,
        interactive_reply=reply,
        process_reply=process_reply,
        send_message=send_message,
    )

    assert first == "completed"
    assert duplicate == "duplicate"
    process_reply.assert_awaited_once_with(
        sender_phone="5491111111111",
        option_id="flow.v1.node.confirm",
        reply_type="button_reply",
        whatsapp_message_id="wamid.interactive",
    )
    send_message.assert_awaited_once_with("5491111111111", WhatsAppText("Listo"))


@pytest.mark.asyncio
async def test_text_message_receives_and_updates_recent_history():
    redis = FakeRedis()
    await ConversationHistoryService.append_exchange(
        redis,
        "5491111111111",
        "mostrame mis transacciones",
        "¿Querés ver gastos, ingresos o todos?",
    )
    received_history = None

    async def process_message(**kwargs):
        nonlocal received_history
        received_history = kwargs["conversation_history"]
        return SimpleNamespace(reply_text="Estos son todos tus movimientos")

    status = await process_text_message_once(
        redis_client=redis,
        sender_phone="5491111111111",
        text_body="todos",
        whatsapp_message_id="wamid.history",
        process_message=process_message,
        send_message=AsyncMock(return_value=True),
    )
    stored = await ConversationHistoryService.get_recent(
        redis,
        "5491111111111",
    )

    assert status == "completed"
    assert received_history == [
        {"role": "user", "content": "mostrame mis transacciones"},
        {
            "role": "assistant",
            "content": "¿Querés ver gastos, ingresos o todos?",
        },
    ]
    assert [message.to_dict() for message in stored][-2:] == [
        {"role": "user", "content": "todos"},
        {"role": "assistant", "content": "Estos son todos tus movimientos"},
    ]
