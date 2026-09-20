"""Tests para el estado multi-turno de límites de gasto."""
from decimal import Decimal

import pytest

from app.services.conversation import (
    ConversationService,
    ConversationState,
    LastCreatedLimit,
    PendingLimit,
    PendingLimitDelete,
)


# ---------------------------------------------------------------------------
# Serialización de dataclasses
# ---------------------------------------------------------------------------


class TestPendingLimitSerialization:
    def test_round_trip(self):
        pending = PendingLimit(
            sender_phone="5491100001111",
            category="Comida",
            amount=Decimal("300000"),
            month=7,
            year=2026,
            currency="USD",
            is_edit=False,
        )
        restored = PendingLimit.from_dict(pending.to_dict())
        assert restored.sender_phone == pending.sender_phone
        assert restored.category == "Comida"
        assert restored.amount == Decimal("300000")
        assert restored.month == 7
        assert restored.year == 2026
        assert restored.currency == "USD"
        assert restored.is_edit is False

    def test_none_fields(self):
        pending = PendingLimit(
            sender_phone="5491100001111",
            category=None,
            amount=None,
            month=None,
            year=None,
        )
        restored = PendingLimit.from_dict(pending.to_dict())
        assert restored.category is None
        assert restored.amount is None
        assert restored.month is None
        assert restored.year is None

    def test_is_edit_flag(self):
        pending = PendingLimit(
            sender_phone="5491100001111",
            category="Ropa",
            amount=Decimal("1000"),
            month=8,
            year=2026,
            is_edit=True,
        )
        assert PendingLimit.from_dict(pending.to_dict()).is_edit is True


class TestLastCreatedLimitSerialization:
    def test_round_trip(self):
        limit = LastCreatedLimit(
            limit_id="abc-123",
            sender_phone="5491100001111",
            category_name="Ropa",
            amount=Decimal("300000"),
            month=7,
            year=2026,
            currency="USD",
        )
        restored = LastCreatedLimit.from_dict(limit.to_dict())
        assert restored.limit_id == "abc-123"
        assert restored.category_name == "Ropa"
        assert restored.amount == Decimal("300000")
        assert restored.month == 7
        assert restored.year == 2026
        assert restored.currency == "USD"


class TestPendingLimitDeleteSerialization:
    def test_round_trip(self):
        pending = PendingLimitDelete(
            sender_phone="5491100001111",
            category_name="Comida",
            candidates=[
                {"limit_id": "a", "month": 7, "year": 2026, "amount": "300000"},
                {"limit_id": "b", "month": 11, "year": 2026, "amount": "400000"},
            ],
        )
        restored = PendingLimitDelete.from_dict(pending.to_dict())
        assert restored.sender_phone == pending.sender_phone
        assert restored.category_name == "Comida"
        assert len(restored.candidates) == 2
        assert restored.candidates[0]["month"] == 7


class TestConversationStateLimit:
    def test_state_includes_pending_limit(self):
        pending = PendingLimit(
            sender_phone="5491100001111",
            category="Comida",
            amount=Decimal("300000"),
            month=7,
            year=2026,
        )
        state = ConversationState(step="awaiting_limit_data", pending_limit=pending)
        d = state.to_dict()
        assert d["step"] == "awaiting_limit_data"
        assert d["pending_limit"]["category"] == "Comida"

        restored = ConversationState.from_dict(d)
        assert restored.pending_limit is not None
        assert restored.pending_limit.category == "Comida"
        assert restored.pending_limit.amount == Decimal("300000")

    def test_state_includes_pending_limit_delete(self):
        pending = PendingLimitDelete(
            sender_phone="5491100001111",
            category_name="Comida",
            candidates=[{"month": 7, "year": 2026, "amount": "300000"}],
        )
        state = ConversationState(
            step="awaiting_limit_month_selection",
            pending_limit_delete=pending,
        )
        restored = ConversationState.from_dict(state.to_dict())
        assert restored.step == "awaiting_limit_month_selection"
        assert restored.pending_limit_delete is not None
        assert restored.pending_limit_delete.category_name == "Comida"

    def test_backward_compat_no_limit_keys(self):
        old_state_dict = {"step": "none", "pending_movement": None, "pending_reminder": None}
        state = ConversationState.from_dict(old_state_dict)
        assert state.pending_limit is None
        assert state.pending_limit_delete is None

    def test_empty(self):
        state = ConversationState.empty()
        assert state.step == "none"
        assert state.pending_limit is None
        assert state.pending_limit_delete is None


# ---------------------------------------------------------------------------
# ConversationService (Redis mockeado)
# ---------------------------------------------------------------------------


class _MockRedis:
    def __init__(self):
        self.storage = {}

    async def setex(self, key, ttl, value):
        self.storage[key] = value

    async def get(self, key):
        return self.storage.get(key)

    async def delete(self, key):
        self.storage.pop(key, None)

    async def ping(self):
        return True


@pytest.fixture()
def mock_redis(monkeypatch):
    client = _MockRedis()

    async def mock_get_client():
        return client

    monkeypatch.setattr(ConversationService, "_get_client", mock_get_client)
    ConversationService._client = None
    return client


@pytest.mark.asyncio
async def test_set_and_get_pending_limit(mock_redis):
    pending = PendingLimit(
        sender_phone="5491100001111",
        category=None,
        amount=None,
        month=1,
        year=2027,
    )
    await ConversationService.set_pending_limit(
        "5491100001111", pending, step="awaiting_limit_year_confirmation"
    )
    assert await ConversationService.is_awaiting_limit_year_confirmation("5491100001111")
    retrieved = await ConversationService.get_pending_limit("5491100001111")
    assert retrieved is not None
    assert retrieved.month == 1
    assert retrieved.year == 2027


@pytest.mark.asyncio
async def test_is_awaiting_limit_data(mock_redis):
    phone = "5491100002222"
    pending = PendingLimit(
        sender_phone=phone,
        category="Comida",
        amount=None,
        month=7,
        year=2026,
    )
    assert not await ConversationService.is_awaiting_limit_data(phone)
    await ConversationService.set_pending_limit(phone, pending, step="awaiting_limit_data")
    assert await ConversationService.is_awaiting_limit_data(phone)


@pytest.mark.asyncio
async def test_is_awaiting_limit_category_confirmation(mock_redis):
    phone = "5491100002223"
    pending = PendingLimit(
        sender_phone=phone,
        category="Ropa",
        amount=Decimal("1000"),
        month=9,
        year=2026,
    )
    await ConversationService.set_pending_limit(
        phone,
        pending,
        step="awaiting_limit_category_confirmation",
    )

    assert await ConversationService.is_awaiting_limit_category_confirmation(phone)


@pytest.mark.asyncio
async def test_set_and_get_pending_limit_delete(mock_redis):
    phone = "5491100003333"
    pending = PendingLimitDelete(
        sender_phone=phone,
        category_name="Comida",
        candidates=[{"month": 7, "year": 2026, "amount": "300000"}],
    )
    assert not await ConversationService.is_awaiting_limit_month_selection(phone)
    await ConversationService.set_pending_limit_delete(phone, pending)
    assert await ConversationService.is_awaiting_limit_month_selection(phone)
    retrieved = await ConversationService.get_pending_limit_delete(phone)
    assert retrieved is not None
    assert retrieved.category_name == "Comida"


@pytest.mark.asyncio
async def test_last_limit_set_get_clear(mock_redis):
    phone = "5491100004444"
    limit = LastCreatedLimit(
        limit_id="abc",
        sender_phone=phone,
        category_name="Ropa",
        amount=Decimal("300000"),
        month=7,
        year=2026,
    )
    assert await ConversationService.get_last_limit(phone) is None
    await ConversationService.set_last_limit(phone, limit)
    retrieved = await ConversationService.get_last_limit(phone)
    assert retrieved is not None
    assert retrieved.category_name == "Ropa"
    assert retrieved.amount == Decimal("300000")
    await ConversationService.clear_last_limit(phone)
    assert await ConversationService.get_last_limit(phone) is None
