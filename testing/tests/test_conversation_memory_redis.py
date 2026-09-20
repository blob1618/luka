"""Integration tests for ConversationHistoryService against a real Redis."""

import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from app.services.conversation import ConversationHistoryService

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380")


@pytest_asyncio.fixture()
async def redis_client():
    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis no disponible")
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture()
async def new_whatsapp_id(redis_client):
    created = []

    def create() -> str:
        value = str(uuid.uuid4())
        created.append(value)
        return value

    yield create
    for value in created:
        await ConversationHistoryService.clear(redis_client, value)


@pytest.mark.asyncio
async def test_window_keeps_last_four_turns(redis_client, new_whatsapp_id, monkeypatch):
    monkeypatch.setenv("CONVERSATION_MEMORY_TURNS", "4")
    whatsapp_id = new_whatsapp_id()

    for index in range(1, 6):
        await ConversationHistoryService.append_exchange(
            redis_client,
            whatsapp_id,
            f"mensaje {index}",
            f"respuesta {index}",
            message_id=f"wamid-{index}",
        )

    history = await ConversationHistoryService.get_recent(redis_client, whatsapp_id)

    assert len(history) == 8
    assert [message.role for message in history] == ["user", "assistant"] * 4
    assert [message.content for message in history] == [
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
async def test_ttl_is_about_24_hours(redis_client, new_whatsapp_id, monkeypatch):
    monkeypatch.setenv("CONVERSATION_MEMORY_TTL_HOURS", "24")
    whatsapp_id = new_whatsapp_id()

    await ConversationHistoryService.append_exchange(
        redis_client, whatsapp_id, "hola", "buenas"
    )

    ttl = await redis_client.ttl(f"conversation_memory:whatsapp:{whatsapp_id}")

    assert 86300 <= ttl <= 86400


@pytest.mark.asyncio
async def test_duplicate_message_id_is_ignored(redis_client, new_whatsapp_id):
    whatsapp_id = new_whatsapp_id()

    await ConversationHistoryService.append_exchange(
        redis_client, whatsapp_id, "hola", "buenas", message_id="wamid-dup"
    )
    await ConversationHistoryService.append_exchange(
        redis_client,
        whatsapp_id,
        "hola de nuevo",
        "otra respuesta",
        message_id="wamid-dup",
    )

    history = await ConversationHistoryService.get_recent(redis_client, whatsapp_id)

    assert [message.content for message in history] == ["hola", "buenas"]


@pytest.mark.asyncio
async def test_users_are_isolated(redis_client, new_whatsapp_id):
    first_id = new_whatsapp_id()
    second_id = new_whatsapp_id()

    await ConversationHistoryService.append_exchange(
        redis_client, first_id, "mensaje uno", "respuesta uno"
    )
    await ConversationHistoryService.append_exchange(
        redis_client, second_id, "mensaje dos", "respuesta dos"
    )

    first = await ConversationHistoryService.get_recent(redis_client, first_id)
    second = await ConversationHistoryService.get_recent(redis_client, second_id)

    assert [message.content for message in first] == ["mensaje uno", "respuesta uno"]
    assert [message.content for message in second] == ["mensaje dos", "respuesta dos"]


@pytest.mark.asyncio
async def test_clear_removes_key(redis_client, new_whatsapp_id):
    whatsapp_id = new_whatsapp_id()
    key = f"conversation_memory:whatsapp:{whatsapp_id}"

    await ConversationHistoryService.append_exchange(
        redis_client, whatsapp_id, "hola", "buenas"
    )
    assert await redis_client.exists(key) == 1

    await ConversationHistoryService.clear(redis_client, whatsapp_id)

    assert await redis_client.exists(key) == 0
