import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.webhook_idempotency import (
    WebhookIdempotencyService,
    process_interactive_message_once,
    process_text_message_once,
)
from app.api.whatsapp import InboundInteractiveReply, WhatsAppImage, WhatsAppText
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
async def test_duplicate_chart_webhook_sends_one_logical_image():
    redis = FakeRedis()
    image = WhatsAppImage(
        content=b"\x89PNG\r\n\x1a\nchart",
        caption="Gastos por categoria",
    )
    process_message = AsyncMock(
        return_value=SimpleNamespace(
            reply_text=image.caption,
            reply_message=image,
        )
    )
    send_message = AsyncMock(return_value=True)

    first = await process_text_message_once(
        redis_client=redis,
        sender_phone="5491111111111",
        text_body="grafico de gastos",
        whatsapp_message_id="wamid.chart-once",
        process_message=process_message,
        send_message=send_message,
    )
    duplicate = await process_text_message_once(
        redis_client=redis,
        sender_phone="5491111111111",
        text_body="grafico de gastos",
        whatsapp_message_id="wamid.chart-once",
        process_message=process_message,
        send_message=send_message,
    )

    assert first == "completed"
    assert duplicate == "duplicate"
    send_message.assert_awaited_once_with("5491111111111", image)


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
    for index in range(1, 5):
        await ConversationHistoryService.append_exchange(
            redis,
            "5491111111111",
            f"pregunta {index}",
            f"respuesta {index}",
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

    expected_history = []
    for index in range(1, 5):
        expected_history.append({"role": "user", "content": f"pregunta {index}"})
        expected_history.append({"role": "assistant", "content": f"respuesta {index}"})

    assert status == "completed"
    assert received_history == expected_history
    assert [message.to_dict() for message in stored][-2:] == [
        {"role": "user", "content": "todos"},
        {"role": "assistant", "content": "Estos son todos tus movimientos"},
    ]


@pytest.mark.asyncio
async def test_new_turn_evicts_oldest_from_window():
    redis = FakeRedis()
    for index in range(1, 5):
        await ConversationHistoryService.append_exchange(
            redis,
            "5491111111111",
            f"pregunta {index}",
            f"respuesta {index}",
        )

    async def process_message(**_kwargs):
        return SimpleNamespace(reply_text="respuesta 5")

    status = await process_text_message_once(
        redis_client=redis,
        sender_phone="5491111111111",
        text_body="pregunta 5",
        whatsapp_message_id="wamid.evict",
        process_message=process_message,
        send_message=AsyncMock(return_value=True),
    )
    stored = await ConversationHistoryService.get_recent(
        redis,
        "5491111111111",
    )

    assert status == "completed"
    assert len(stored) == 8
    assert stored[0].to_dict() == {"role": "user", "content": "pregunta 2"}
    assert [message.to_dict() for message in stored][-2:] == [
        {"role": "user", "content": "pregunta 5"},
        {"role": "assistant", "content": "respuesta 5"},
    ]


@pytest.mark.asyncio
async def test_clear_memory_result_clears_window_without_appending():
    redis = FakeRedis()
    await ConversationHistoryService.append_exchange(
        redis,
        "5491111111111",
        "hola",
        "buenas",
    )

    async def process_message(**_kwargs):
        return SimpleNamespace(
            reply_text="Listo, arrancamos de cero. Olvidé lo anterior.",
            clear_memory=True,
        )

    status = await process_text_message_once(
        redis_client=redis,
        sender_phone="5491111111111",
        text_body="olvidá todo",
        whatsapp_message_id="wamid.reset",
        process_message=process_message,
        send_message=AsyncMock(return_value=True),
    )

    assert status == "completed"
    assert await ConversationHistoryService.get_recent(redis, "5491111111111") == []


@pytest.mark.asyncio
async def test_text_message_appends_with_message_id():
    redis = FakeRedis()
    process_message = AsyncMock(return_value=SimpleNamespace(reply_text="hola"))
    append = AsyncMock()

    with patch.object(ConversationHistoryService, "append_exchange", append):
        status = await process_text_message_once(
            redis_client=redis,
            sender_phone="5491111111111",
            text_body="hola",
            whatsapp_message_id="wamid.wiring",
            process_message=process_message,
            send_message=AsyncMock(return_value=True),
        )

    assert status == "completed"
    assert append.await_args.kwargs["message_id"] == "wamid.wiring"


@pytest.mark.asyncio
async def test_interactive_message_appends_with_message_id():
    redis = FakeRedis()
    reply = InboundInteractiveReply(
        message_id="wamid.interactive-wiring",
        sender_phone="5491111111111",
        reply_type="button_reply",
        option_id="flow.v1.node.confirm",
    )
    process_reply = AsyncMock(
        return_value=SimpleNamespace(reply_message=WhatsAppText("Listo"))
    )
    append = AsyncMock()

    with patch.object(ConversationHistoryService, "append_exchange", append):
        status = await process_interactive_message_once(
            redis_client=redis,
            interactive_reply=reply,
            process_reply=process_reply,
            send_message=AsyncMock(return_value=True),
        )

    assert status == "completed"
    assert append.await_args.kwargs["message_id"] == "wamid.interactive-wiring"


@pytest.mark.asyncio
async def test_interactive_clear_memory_clears_without_appending():
    redis = FakeRedis()
    reply = InboundInteractiveReply(
        message_id="wamid.interactive-reset",
        sender_phone="5491111111111",
        reply_type="button_reply",
        option_id="flow.v1.node.confirm",
    )
    process_reply = AsyncMock(
        return_value=SimpleNamespace(
            reply_text="Listo, arrancamos de cero. Olvidé lo anterior.",
            clear_memory=True,
        )
    )
    clear = AsyncMock()
    append = AsyncMock()

    with (
        patch.object(ConversationHistoryService, "clear", clear),
        patch.object(ConversationHistoryService, "append_exchange", append),
    ):
        status = await process_interactive_message_once(
            redis_client=redis,
            interactive_reply=reply,
            process_reply=process_reply,
            send_message=AsyncMock(return_value=True),
        )

    assert status == "completed"
    clear.assert_awaited_once_with(redis, "5491111111111")
    append.assert_not_awaited()
