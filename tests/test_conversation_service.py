"""Tests unitarios de ConversationService (estado multi-turno) y sus dataclasses.

Cubre los métodos no ejercitados por test_conversation_reminder.py:
set_state/clear_state, pending movement, last movement y rename.
Todos usan un fake de Redis, sin red real.
"""

from decimal import Decimal

import pytest

from app.services.conversation import (
    ConversationService,
    ConversationState,
    ConversationStateUnavailable,
    LastRegisteredMovement,
    PendingConversationFlow,
    PendingMovement,
    PendingReminder,
)


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
    if storage is None:
        storage = {}

    async def mock_get_client():
        return MockRedisClient(storage, fail_methods)

    monkeypatch.setattr(ConversationService, "_get_client", mock_get_client)
    return storage


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
