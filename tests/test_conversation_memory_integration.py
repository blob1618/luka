"""Integración liviana entre la memoria conversacional y el pipeline de webhook."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.conversation import ConversationHistoryService
from app.services.webhook_idempotency import process_text_message_once

from tests.conftest import FakeRedis

pytestmark = pytest.mark.unit


class _BrokenMemoryRedis(FakeRedis):
    async def lrange(self, key, start, stop):
        raise ConnectionError("redis down")

    async def eval(self, script, numkeys, *keys_and_args):
        if "LRANGE" in script and "RPUSH" in script:
            raise ConnectionError("redis down")
        return await super().eval(script, numkeys, *keys_and_args)


@pytest.mark.asyncio
async def test_users_do_not_share_history():
    redis = FakeRedis()

    async def first_message(**_kwargs):
        return SimpleNamespace(reply_text="respuesta uno")

    async def second_message(**_kwargs):
        return SimpleNamespace(reply_text="respuesta dos")

    await process_text_message_once(
        redis_client=redis,
        sender_phone="5491100000001",
        text_body="mensaje uno",
        whatsapp_message_id="wamid.one",
        process_message=first_message,
        send_message=AsyncMock(return_value=True),
    )
    await process_text_message_once(
        redis_client=redis,
        sender_phone="5491100000002",
        text_body="mensaje dos",
        whatsapp_message_id="wamid.two",
        process_message=second_message,
        send_message=AsyncMock(return_value=True),
    )

    first_history = await ConversationHistoryService.get_recent(redis, "5491100000001")
    second_history = await ConversationHistoryService.get_recent(redis, "5491100000002")

    assert [message.content for message in first_history] == [
        "mensaje uno",
        "respuesta uno",
    ]
    assert [message.content for message in second_history] == [
        "mensaje dos",
        "respuesta dos",
    ]


@pytest.mark.asyncio
async def test_fifth_message_receives_previous_four_turns():
    redis = FakeRedis()
    phone = "5491100000001"
    histories = []

    async def process_message(**kwargs):
        histories.append(kwargs["conversation_history"])
        return SimpleNamespace(reply_text=f"respuesta {len(histories)}")

    for index in range(1, 6):
        status = await process_text_message_once(
            redis_client=redis,
            sender_phone=phone,
            text_body=f"mensaje {index}",
            whatsapp_message_id=f"wamid.{index}",
            process_message=process_message,
            send_message=AsyncMock(return_value=True),
        )
        assert status == "completed"

    assert histories[4] == [
        {"role": "user", "content": "mensaje 1"},
        {"role": "assistant", "content": "respuesta 1"},
        {"role": "user", "content": "mensaje 2"},
        {"role": "assistant", "content": "respuesta 2"},
        {"role": "user", "content": "mensaje 3"},
        {"role": "assistant", "content": "respuesta 3"},
        {"role": "user", "content": "mensaje 4"},
        {"role": "assistant", "content": "respuesta 4"},
    ]

    stored = await ConversationHistoryService.get_recent(redis, phone)
    assert [message.content for message in stored] == [
        "mensaje 2",
        "respuesta 2",
        "mensaje 3",
        "respuesta 3",
        "mensaje 4",
        "respuesta 4",
        "mensaje 5",
        "respuesta 5",
    ]


@pytest.mark.asyncio
async def test_reset_result_clears_window_without_persisting_turn():
    redis = FakeRedis()
    phone = "5491100000001"
    await ConversationHistoryService.append_exchange(redis, phone, "hola", "buenas")

    async def process_message(**_kwargs):
        return SimpleNamespace(
            reply_text="Listo, arrancamos de cero. Olvidé lo anterior.",
            clear_memory=True,
        )

    status = await process_text_message_once(
        redis_client=redis,
        sender_phone=phone,
        text_body="olvidá todo",
        whatsapp_message_id="wamid.reset",
        process_message=process_message,
        send_message=AsyncMock(return_value=True),
    )

    assert status == "completed"
    assert await ConversationHistoryService.get_recent(redis, phone) == []


@pytest.mark.asyncio
async def test_assistant_proposal_reaches_history_as_context():
    redis = FakeRedis()
    phone = "5491100000001"
    proposal = "Puedo compensar $100 moviendo saldo de Comida a Transporte, ¿confirmás?"
    await ConversationHistoryService.append_exchange(
        redis,
        phone,
        "¿Me pasé del límite de transporte?",
        proposal,
    )
    received_history = None

    async def process_message(**kwargs):
        nonlocal received_history
        received_history = kwargs["conversation_history"]
        return SimpleNamespace(
            reply_text="No tengo ninguna operación pendiente para confirmar."
        )

    status = await process_text_message_once(
        redis_client=redis,
        sender_phone=phone,
        text_body="Confirmo",
        whatsapp_message_id="wamid.confirm-proposal",
        process_message=process_message,
        send_message=AsyncMock(return_value=True),
    )

    assert status == "completed"
    assert received_history == [
        {"role": "user", "content": "¿Me pasé del límite de transporte?"},
        {"role": "assistant", "content": proposal},
    ]


@pytest.mark.asyncio
async def test_memory_failure_does_not_block_processing(capsys):
    redis = _BrokenMemoryRedis()
    phone = "5491100000001"

    async def process_message(**_kwargs):
        return SimpleNamespace(reply_text="hola")

    send_message = AsyncMock(return_value=True)
    status = await process_text_message_once(
        redis_client=redis,
        sender_phone=phone,
        text_body="hola",
        whatsapp_message_id="wamid.broken",
        process_message=process_message,
        send_message=send_message,
    )

    assert status == "completed"
    send_message.assert_awaited_once_with(phone, "hola")
    captured = capsys.readouterr()
    assert "get_recent error" in captured.out
    assert "append_exchange error" in captured.out
