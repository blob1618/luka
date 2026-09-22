"""Tests unitarios de ConversationService (estado multi-turno) y sus dataclasses.

Cubre los métodos no ejercitados por test_conversation_reminder.py:
set_state/clear_state, pending movement, last movement y rename.
Todos usan un fake de Redis, sin red real.
"""

import asyncio
from decimal import Decimal
import json

import pytest

from app.services.conversation import (
    ConversationHistoryService,
    ConversationService,
    ConversationState,
    ConversationStateUnavailable,
    LastRegisteredMovement,
    PendingConversationFlow,
    PendingMovement,
    PendingMovementCategoryChange,
    PendingReminder,
)
from tests.conftest import FakeRedis


def _pending_movement(**overrides):
    data = {
        "sender_phone": "5491100001234",
        "whatsapp_message_id": "wamid-1",
        "original_text": "Gasté 5000 en super",
        "movement_type": "egreso",
        "amount": Decimal("5000"),
        "currency": "ARS",
        "description": "super",
        "inferred_category": "supermercado",
        "llm_result_extra": {"intent": "expense"},
    }
    data.update(overrides)
    return PendingMovement(**data)


def _last_movement(**overrides):
    data = {
        "movement_id": "mov-1",
        "sender_phone": "5491100001234",
        "movement_type": "egreso",
        "amount": Decimal("4200"),
        "currency": "ARS",
        "description": "super",
        "category_name": "supermercado",
    }
    data.update(overrides)
    return LastRegisteredMovement(**data)


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


class TestPendingMovement:
    def test_to_dict_converts_amount_to_str(self):
        d = _pending_movement().to_dict()

        assert d["amount"] == "5000"
        assert d["movement_type"] == "egreso"
        assert d["llm_result_extra"] == {"intent": "expense"}

    def test_round_trip_preserves_decimal(self):
        pm = _pending_movement(amount=Decimal("1234.56"))

        restored = PendingMovement.from_dict(pm.to_dict())

        assert restored.amount == Decimal("1234.56")
        assert restored.sender_phone == pm.sender_phone
        assert restored.whatsapp_message_id == pm.whatsapp_message_id
        assert restored.inferred_category == pm.inferred_category


class TestLastRegisteredMovement:
    def test_to_dict_converts_amount_to_str(self):
        d = _last_movement().to_dict()

        assert d["amount"] == "4200"
        assert d["movement_id"] == "mov-1"

    def test_round_trip_preserves_decimal(self):
        lm = _last_movement(amount=Decimal("99.99"), category_name=None)

        restored = LastRegisteredMovement.from_dict(lm.to_dict())

        assert restored.amount == Decimal("99.99")
        assert restored.category_name is None
        assert restored.description == "super"


class TestConversationState:
    def test_from_dict_with_pending_movement(self):
        pm = _pending_movement()
        state = ConversationState.from_dict(
            {
                "step": "awaiting_category_confirmation",
                "pending_movement": pm.to_dict(),
                "pending_reminder": None,
            }
        )

        assert state.step == "awaiting_category_confirmation"
        assert state.pending_movement is not None
        assert state.pending_movement.amount == Decimal("5000")

    def test_from_dict_with_pending_movement_category_change(self):
        pending = PendingMovementCategoryChange(movement_id="movement-123")

        state = ConversationState.from_dict(
            {
                "step": "awaiting_movement_category_change",
                "pending_movement_category_change": pending.to_dict(),
            }
        )

        assert state.step == "awaiting_movement_category_change"
        assert state.pending_movement_category_change == pending


# ---------------------------------------------------------------------------
# ConversationService — estado de conversación
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_state_logs_error_on_redis_failure(monkeypatch, capsys):
    _install_mock_client(monkeypatch, fail_methods=("setex",))

    await ConversationService.set_state("5491100000001", ConversationState.empty())

    captured = capsys.readouterr()
    assert "set_state error" in captured.out


@pytest.mark.asyncio
async def test_clear_state_removes_key(monkeypatch):
    storage = {"conversation:5491100000001": "{}"}
    _install_mock_client(monkeypatch, storage)

    await ConversationService.clear_state("5491100000001")

    assert "conversation:5491100000001" not in storage


@pytest.mark.asyncio
async def test_clear_state_logs_error_on_redis_failure(monkeypatch, capsys):
    _install_mock_client(monkeypatch, fail_methods=("delete",))

    await ConversationService.clear_state("5491100000001")

    captured = capsys.readouterr()
    assert "clear_state error" in captured.out


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


@pytest.mark.asyncio
async def test_set_get_and_check_pending_movement(monkeypatch):
    _install_mock_client(monkeypatch)
    pm = _pending_movement()

    await ConversationService.set_pending_movement("5491100000001", pm)

    assert await ConversationService.is_awaiting_category_confirmation("5491100000001")
    retrieved = await ConversationService.get_pending_movement("5491100000001")
    assert retrieved is not None
    assert retrieved.amount == Decimal("5000")
    assert retrieved.inferred_category == "supermercado"


@pytest.mark.asyncio
async def test_get_pending_movement_returns_none_when_empty(monkeypatch):
    _install_mock_client(monkeypatch)

    assert await ConversationService.get_pending_movement("5491100000001") is None
    assert not await ConversationService.is_awaiting_category_confirmation("5491100000001")


# ---------------------------------------------------------------------------
# ConversationService — último movimiento registrado (cambio de categoría)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_get_and_clear_last_movement(monkeypatch):
    _install_mock_client(monkeypatch)
    lm = _last_movement()

    await ConversationService.set_last_movement("5491100000001", lm)
    retrieved = await ConversationService.get_last_movement("5491100000001")
    assert retrieved is not None
    assert retrieved.amount == Decimal("4200")

    await ConversationService.clear_last_movement("5491100000001")
    assert await ConversationService.get_last_movement("5491100000001") is None


@pytest.mark.asyncio
async def test_get_last_movement_logs_error_on_redis_failure(monkeypatch, capsys):
    _install_mock_client(monkeypatch, fail_methods=("get",))

    assert await ConversationService.get_last_movement("5491100000001") is None

    captured = capsys.readouterr()
    assert "get_last_movement error" in captured.out


@pytest.mark.asyncio
async def test_clear_last_movement_logs_error_on_redis_failure(monkeypatch, capsys):
    _install_mock_client(monkeypatch, fail_methods=("delete",))

    await ConversationService.clear_last_movement("5491100000001")

    captured = capsys.readouterr()
    assert "clear_last_movement error" in captured.out


# ---------------------------------------------------------------------------
# ConversationService — rename por título duplicado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_get_pending_rename(monkeypatch):
    _install_mock_client(monkeypatch)
    pr = PendingReminder(
        sender_phone="5491100000001",
        reminder_concept="luz",
        reminder_day=15,
        reminder_amount=None,
        reminder_currency="ARS",
    )

    await ConversationService.set_pending_rename("5491100000001", pr)

    assert await ConversationService.is_awaiting_rename("5491100000001")
    retrieved = await ConversationService.get_pending_rename("5491100000001")
    assert retrieved is not None
    assert retrieved.reminder_concept == "luz"
    assert retrieved.reminder_day == 15


# ---------------------------------------------------------------------------
# Caché por corrutina / ContextVar para ConversationState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_awaiting_helpers_trigger_single_redis_get(monkeypatch):
    """Siete helpers is_awaiting_* para el mismo usuario producen un solo GET a Redis."""
    storage, client = _install_mock_client_instance(monkeypatch)
    phone = "5491100000001"

    # Ejecutar secuencialmente los helpers que consulta el dispatcher
    assert not await ConversationService.is_awaiting_rename(phone)
    assert not await ConversationService.is_awaiting_reminder_data(phone)
    assert not await ConversationService.is_awaiting_limit_year_confirmation(phone)
    assert not await ConversationService.is_awaiting_limit_category_confirmation(phone)
    assert not await ConversationService.is_awaiting_limit_data(phone)
    assert not await ConversationService.is_awaiting_limit_delete_category(phone)
    assert not await ConversationService.is_awaiting_limit_month_selection(phone)
    assert not await ConversationService.is_awaiting_category_confirmation(phone)

    # Todos los helpers colapsaron a un solo GET en Redis
    assert client.get_calls == ["conversation:5491100000001"]


@pytest.mark.asyncio
async def test_get_state_returns_cached_value(monkeypatch):
    """Un segundo get_state devuelve el valor cacheado sin ir a Redis (con y sin datos)."""
    pm = _pending_movement()
    state = ConversationState(
        step="awaiting_category_confirmation",
        pending_movement=pm,
    )
    storage = {"conversation:5491100000001": json.dumps(state.to_dict())}
    _, client = _install_mock_client_instance(monkeypatch, storage)

    # Primer get_state: miss en caché, consulta Redis
    s1 = await ConversationService.get_state("5491100000001")
    assert s1.step == "awaiting_category_confirmation"
    assert len(client.get_calls) == 1

    # Segundo get_state: hit en caché, no consulta Redis
    s2 = await ConversationService.get_state("5491100000001")
    assert s2.step == "awaiting_category_confirmation"
    assert len(client.get_calls) == 1

    # Usuario sin estado (miss en Redis -> cachea ConversationState.empty())
    empty1 = await ConversationService.get_state("5491100000002")
    assert empty1.step == "none"
    assert client.get_calls.count("conversation:5491100000002") == 1

    empty2 = await ConversationService.get_state("5491100000002")
    assert empty2.step == "none"
    assert client.get_calls.count("conversation:5491100000002") == 1


@pytest.mark.asyncio
async def test_set_state_refreshes_cache_only_on_success(monkeypatch):
    """set_state refresca la entrada solo después del éxito en Redis."""
    storage, client = _install_mock_client_instance(monkeypatch)
    phone = "5491100000001"
    pm = _pending_movement()
    state1 = ConversationState(
        step="awaiting_category_confirmation",
        pending_movement=pm,
    )

    # 1. Éxito: set_state persiste y actualiza la caché
    await ConversationService.set_state(phone, state1)
    # get_state inmediato no debe llamar a Redis
    cached = await ConversationService.get_state(phone)
    assert cached.step == "awaiting_category_confirmation"
    assert len(client.get_calls) == 0

    # 2. Falla: si setex falla, la caché no se actualiza con el nuevo valor
    client.fail_methods.add("setex")
    state2 = ConversationState(step="awaiting_rename")
    await ConversationService.set_state(phone, state2)

    # get_state sigue devolviendo state1 (la caché previa no se corrompe)
    cached_after_fail = await ConversationService.get_state(phone)
    assert cached_after_fail.step == "awaiting_category_confirmation"
    assert len(client.get_calls) == 0


@pytest.mark.asyncio
async def test_clear_state_invalidates_cache_only_on_success(monkeypatch):
    """clear_state invalida la entrada solo después del éxito en Redis."""
    pm = _pending_movement()
    state = ConversationState(
        step="awaiting_category_confirmation",
        pending_movement=pm,
    )
    phone = "5491100000001"
    storage = {f"conversation:{phone}": json.dumps(state.to_dict())}
    _, client = _install_mock_client_instance(monkeypatch, storage)

    # Poblar la caché
    s = await ConversationService.get_state(phone)
    assert s.step == "awaiting_category_confirmation"
    assert len(client.get_calls) == 1

    # Si delete falla en Redis, la caché no se invalida
    client.fail_methods.add("delete")
    await ConversationService.clear_state(phone)
    s_cached = await ConversationService.get_state(phone)
    assert s_cached.step == "awaiting_category_confirmation"
    assert len(client.get_calls) == 1

    # Si delete tiene éxito, la caché se invalida y la próxima lectura va a Redis
    client.fail_methods.remove("delete")
    await ConversationService.clear_state(phone)
    assert f"conversation:{phone}" not in storage

    s_after_clear = await ConversationService.get_state(phone)
    assert s_after_clear.step == "none"
    assert len(client.get_calls) == 2


@pytest.mark.asyncio
async def test_redis_failures_do_not_corrupt_previous_cache(monkeypatch):
    """Fallos de Redis no corrompen una entrada previa."""
    phone = "5491100000001"
    pm = _pending_movement()
    state = ConversationState(
        step="awaiting_category_confirmation",
        pending_movement=pm,
    )
    storage = {f"conversation:{phone}": json.dumps(state.to_dict())}
    _, client = _install_mock_client_instance(monkeypatch, storage)

    # Cargar en caché
    assert (
        await ConversationService.get_state(phone)
    ).step == "awaiting_category_confirmation"
    assert len(client.get_calls) == 1

    # Redis se cae por completo (get, setex, delete fallan)
    client.fail_methods.update({"get", "setex", "delete"})

    # Intento de set_state falla
    await ConversationService.set_state(phone, ConversationState.empty())
    # Intento de clear_state falla
    await ConversationService.clear_state(phone)

    # La entrada previa en caché sigue intacta y get_state no explota
    recovered = await ConversationService.get_state(phone)
    assert recovered.step == "awaiting_category_confirmation"
    assert len(client.get_calls) == 1  # No intentó Redis porque estaba en caché


@pytest.mark.asyncio
async def test_concurrent_tasks_and_different_users_isolated(monkeypatch):
    """Dos tareas concurrentes y usuarios distintos no comparten ni contaminan caché."""
    storage, client = _install_mock_client_instance(monkeypatch)

    user1 = "5491100000001"
    user2 = "5491100000002"

    state1 = ConversationState(step="awaiting_reminder_data")
    state2 = ConversationState(step="awaiting_category_confirmation")

    barrier = asyncio.Barrier(2)

    async def task_1():
        # Setea user1
        await ConversationService.set_state(user1, state1)
        await barrier.wait()
        # Lee user1 de su caché
        u1_state = await ConversationService.get_state(user1)
        # Lee user2 (miss en su caché local -> consulta Redis)
        u2_state = await ConversationService.get_state(user2)
        return u1_state, u2_state

    async def task_2():
        # Setea user2
        await ConversationService.set_state(user2, state2)
        await barrier.wait()
        # Lee user2 de su caché
        u2_state = await ConversationService.get_state(user2)
        # Lee user1 (miss en su caché local -> consulta Redis)
        u1_state = await ConversationService.get_state(user1)
        return u1_state, u2_state

    (t1_u1, t1_u2), (t2_u1, t2_u2) = await asyncio.gather(task_1(), task_2())

    assert t1_u1.step == "awaiting_reminder_data"
    assert t1_u2.step == "awaiting_category_confirmation"
    assert t2_u1.step == "awaiting_reminder_data"
    assert t2_u2.step == "awaiting_category_confirmation"


@pytest.mark.asyncio
async def test_copy_on_write_prevents_context_cache_mutation(monkeypatch):
    """Copy-on-write asegura que tareas hijas o contextos heredados no muten el dict del padre."""
    storage, client = _install_mock_client_instance(monkeypatch)
    phone_parent = "5491100000001"
    phone_child = "5491100000002"

    # Contexto padre establece phone_parent
    await ConversationService.set_state(
        phone_parent,
        ConversationState(step="awaiting_category_confirmation"),
    )
    parent_cache_dict = ConversationService._state_cache.get()
    assert parent_cache_dict is not None
    assert phone_parent in parent_cache_dict

    async def child_task():
        # Tarea hija hereda el contexto, pero al escribir debe usar COW
        await ConversationService.set_state(
            phone_child,
            ConversationState(step="awaiting_reminder_data"),
        )
        child_cache = ConversationService._state_cache.get()
        assert phone_child in child_cache
        assert phone_parent in child_cache

    task = asyncio.create_task(child_task())
    await task

    # El padre NO debe tener phone_child en su diccionario de caché
    assert phone_child not in ConversationService._state_cache.get()
    # Y el diccionario original del padre no fue mutado in-place
    assert phone_child not in parent_cache_dict
