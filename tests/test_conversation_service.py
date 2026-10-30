"""Redis client, published navigation and post-delivery visible history contracts."""

import asyncio

import pytest

from app.services.conversation import ConversationHistoryService, ConversationService, ConversationStateUnavailable, PendingConversationFlow
from tests.conftest import FakeRedis


class MockRedisClient:
    """Fake de cliente Redis: guarda en un dict y puede fallar a pedido."""

    def __init__(self, storage, fail_methods=()):
        self.storage = storage
        self.fail_methods = set(fail_methods)
        self.get_calls: list[str] = []

    async def ping(self):
        return True

    async def setex(self, key, ttl, value):
        if "setex" in self.fail_methods:
            raise ConnectionError("redis down")
        self.storage[key] = value

    async def set(self, key, value, *, ex=None):
        del ex
        if "set" in self.fail_methods:
            raise ConnectionError("redis down")
        self.storage[key] = value
        return True

    async def get(self, key):
        self.get_calls.append(key)
        if "get" in self.fail_methods:
            raise ConnectionError("redis down")
        return self.storage.get(key)

    async def delete(self, key):
        if "delete" in self.fail_methods:
            raise ConnectionError("redis down")
        self.storage.pop(key, None)


def _install_mock_client(monkeypatch, storage=None, fail_methods=()):
    monkeypatch.setattr(ConversationService, "_client", None)
    monkeypatch.setattr(ConversationService, "_loop_id", None)
    ConversationService.reset_cache()
    if storage is None:
        storage = {}

    client = MockRedisClient(storage, fail_methods)

    async def mock_get_client():
        return client

    monkeypatch.setattr(ConversationService, "_get_client", mock_get_client)
    return storage


def _install_mock_client_instance(monkeypatch, storage=None, fail_methods=()):
    monkeypatch.setattr(ConversationService, "_client", None)
    monkeypatch.setattr(ConversationService, "_loop_id", None)
    ConversationService.reset_cache()
    if storage is None:
        storage = {}

    client = MockRedisClient(storage, fail_methods)

    async def mock_get_client():
        return client

    monkeypatch.setattr(ConversationService, "_get_client", mock_get_client)
    return storage, client


@pytest.mark.asyncio
async def test_window_keeps_last_four_turns(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MEMORY_TURNS", "4")
    phone = "5491100000001"
    client = FakeRedis()
    for index in range(1, 6):
        await ConversationHistoryService.append_exchange(
            client,
            phone,
            f"user {index}",
            f"assistant {index}",
        )

    history = await ConversationHistoryService.get_recent(client, phone)

    assert len(history) == 8
    assert [message.role for message in history] == ["user", "assistant"] * 4
    assert history[0].content == "user 2"
    assert history[1].content == "assistant 2"
    assert history[-1].content == "assistant 5"


@pytest.mark.asyncio
async def test_window_respects_turns_env(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MEMORY_TURNS", "1")
    phone = "5491100000001"
    client = FakeRedis()
    await ConversationHistoryService.append_exchange(client, phone, "uno", "respuesta uno")
    await ConversationHistoryService.append_exchange(client, phone, "dos", "respuesta dos")
    await ConversationHistoryService.append_exchange(client, phone, "tres", "respuesta tres")

    history = await ConversationHistoryService.get_recent(client, phone)

    assert [(message.role, message.content) for message in history] == [
        ("user", "tres"),
        ("assistant", "respuesta tres"),
    ]


@pytest.mark.asyncio
async def test_users_are_isolated():
    client = FakeRedis()
    await ConversationHistoryService.append_exchange(
        client, "5491100000001", "mensaje uno", "respuesta uno"
    )
    await ConversationHistoryService.append_exchange(
        client, "5491100000002", "mensaje dos", "respuesta dos"
    )

    first = await ConversationHistoryService.get_recent(client, "5491100000001")
    second = await ConversationHistoryService.get_recent(client, "5491100000002")

    assert [message.content for message in first] == ["mensaje uno", "respuesta uno"]
    assert [message.content for message in second] == ["mensaje dos", "respuesta dos"]


@pytest.mark.asyncio
async def test_ttl_reads_env_and_refreshes_on_each_turn(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MEMORY_TTL_HOURS", "1")
    phone = "5491100000001"
    key = "conversation_memory:whatsapp:5491100000001"
    client = FakeRedis()

    await ConversationHistoryService.append_exchange(client, phone, "uno", "respuesta uno")
    assert client._expirations[key] == 3600

    client._expirations[key] = 1
    await ConversationHistoryService.append_exchange(client, phone, "dos", "respuesta dos")
    assert client._expirations[key] == 3600


@pytest.mark.asyncio
async def test_duplicate_message_id_is_ignored():
    phone = "5491100000001"
    key = "conversation_memory:whatsapp:5491100000001"
    client = FakeRedis()

    await ConversationHistoryService.append_exchange(
        client, phone, "hola", "buenas", message_id="wamid-1"
    )
    client._expirations[key] = 1
    await ConversationHistoryService.append_exchange(
        client, phone, "hola de nuevo", "otra respuesta", message_id="wamid-1"
    )

    history = await ConversationHistoryService.get_recent(client, phone)

    assert [message.content for message in history] == ["hola", "buenas"]
    assert client._expirations[key] == 1


@pytest.mark.asyncio
async def test_order_matches_append_order():
    phone = "5491100000001"
    client = FakeRedis()
    await ConversationHistoryService.append_exchange(client, phone, "primero", "r1")
    await ConversationHistoryService.append_exchange(client, phone, "segundo", "r2")
    await ConversationHistoryService.append_exchange(client, phone, "tercero", "r3")

    history = await ConversationHistoryService.get_recent(client, phone)

    assert [(message.role, message.content) for message in history] == [
        ("user", "primero"),
        ("assistant", "r1"),
        ("user", "segundo"),
        ("assistant", "r2"),
        ("user", "tercero"),
        ("assistant", "r3"),
    ]


@pytest.mark.asyncio
async def test_corrupt_entry_is_skipped_and_valid_turns_survive():
    phone = "5491100000001"
    key = "conversation_memory:whatsapp:5491100000001"
    client = FakeRedis()
    await ConversationHistoryService.append_exchange(client, phone, "primero", "r1")
    await ConversationHistoryService.append_exchange(client, phone, "segundo", "r2")

    client._lists[key].insert(1, "{entrada corrupta")

    history = await ConversationHistoryService.get_recent(client, phone)

    assert [(message.role, message.content) for message in history] == [
        ("user", "primero"),
        ("assistant", "r1"),
        ("user", "segundo"),
        ("assistant", "r2"),
    ]


@pytest.mark.asyncio
async def test_concurrent_appends_keep_last_four_turns(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MEMORY_TURNS", "4")
    phone = "5491100000001"
    client = FakeRedis()

    await asyncio.gather(
        *(
            ConversationHistoryService.append_exchange(
                client,
                phone,
                f"user {index}",
                f"assistant {index}",
                message_id=f"wamid-{index}",
            )
            for index in range(1, 7)
        )
    )

    history = await ConversationHistoryService.get_recent(client, phone)

    assert len(history) == 8
    assert [message.content for message in history] == [
        "user 3",
        "assistant 3",
        "user 4",
        "assistant 4",
        "user 5",
        "assistant 5",
        "user 6",
        "assistant 6",
    ]


@pytest.mark.asyncio
async def test_each_operation_uses_single_round_trip():
    phone = "5491100000001"
    client = FakeRedis()

    await ConversationHistoryService.append_exchange(
        client, phone, "hola", "buenas", message_id="wamid-1"
    )

    assert client.eval_calls == 1
    assert client.get_calls == 0

    history = await ConversationHistoryService.get_recent(client, phone)

    assert client.lrange_calls == 1
    assert client.eval_calls == 1
    assert client.get_calls == 0
    assert [message.content for message in history] == ["hola", "buenas"]


class _BrokenRedis:
    async def lrange(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def eval(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def delete(self, *args, **kwargs):
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_redis_failures_degrade_silently(capsys):
    phone = "5491100000001"
    client = _BrokenRedis()

    assert await ConversationHistoryService.get_recent(client, phone) == []
    await ConversationHistoryService.append_exchange(client, phone, "hola", "buenas")
    await ConversationHistoryService.clear(client, phone)

    captured = capsys.readouterr()
    assert "get_recent error" in captured.out
    assert "append_exchange error" in captured.out
    assert "clear error" in captured.out


@pytest.mark.asyncio
async def test_clear_removes_key():
    phone = "5491100000001"
    key = "conversation_memory:whatsapp:5491100000001"
    client = FakeRedis()
    await ConversationHistoryService.append_exchange(client, phone, "hola", "buenas")

    assert client._lists.get(key)

    await ConversationHistoryService.clear(client, phone)

    assert key not in client._lists
    assert await ConversationHistoryService.get_recent(client, phone) == []


@pytest.mark.asyncio
async def test_sanitization_redacts_urls_and_tokens():
    phone = "5491100000001"
    client = FakeRedis()

    await ConversationHistoryService.append_exchange(
        client,
        phone,
        "https://example.test/path token=secreto",
        "ok",
    )

    history = await ConversationHistoryService.get_recent(client, phone)

    assert history[0].content == "[enlace] token=[redactado]"


@pytest.mark.asyncio
async def test_sanitization_truncates_to_env_max_chars(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MEMORY_MAX_CHARS", "10")
    phone = "5491100000001"
    client = FakeRedis()

    await ConversationHistoryService.append_exchange(client, phone, "x" * 50, "y" * 50)

    history = await ConversationHistoryService.get_recent(client, phone)

    assert [message.content for message in history] == ["x" * 10, "y" * 10]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ConversationService — estado de conversación
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_flow_state_round_trip_and_clear(monkeypatch):
    _install_mock_client(monkeypatch)
    pending = PendingConversationFlow(
        flow_id="flow-1",
        version_id="version-1",
        event_key="category.confirmation_required",
        node_id="question",
        variables={"category": "Agua"},
    )

    await ConversationService.set_pending_conversation_flow("5491100000001", pending)
    restored = await ConversationService.get_pending_conversation_flow(
        "5491100000001"
    )
    await ConversationService.clear_pending_conversation_flow("5491100000001")

    assert restored == pending
    assert (
        await ConversationService.get_pending_conversation_flow("5491100000001")
        is None
    )


@pytest.mark.asyncio
async def test_dynamic_flow_state_does_not_degrade_silently(monkeypatch):
    _install_mock_client(monkeypatch, fail_methods=("set",))
    pending = PendingConversationFlow(
        flow_id="flow-1",
        version_id="version-1",
        event_key="category.confirmation_required",
        node_id="question",
    )

    with pytest.raises(ConversationStateUnavailable):
        await ConversationService.set_pending_conversation_flow(
            "5491100000001",
            pending,
        )


# ---------------------------------------------------------------------------
# ConversationService — movimiento pendiente (confirmación de categoría)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ConversationService — último movimiento registrado (cambio de categoría)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ConversationService — rename por título duplicado
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Caché por corrutina / ContextVar para ConversationState
# ---------------------------------------------------------------------------
