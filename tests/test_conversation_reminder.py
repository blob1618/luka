"""Tests for PendingReminder and ConversationService reminder state (Task 1)."""
from decimal import Decimal

import pytest

from app.services.conversation import (
    ConversationService,
    ConversationState,
    PendingReminder,
)


# ---------------------------------------------------------------------------
# PendingReminder serialization
# ---------------------------------------------------------------------------


def test_pending_reminder_round_trip():
    pr = PendingReminder(
        sender_phone="5491100001111",
        reminder_concept="wifi",
        reminder_day=4,
        reminder_amount=Decimal("7000"),
        reminder_currency="ARS",
    )
    d = pr.to_dict()
    restored = PendingReminder.from_dict(d)
    assert restored.sender_phone == pr.sender_phone
    assert restored.reminder_concept == pr.reminder_concept
    assert restored.reminder_day == pr.reminder_day
    assert restored.reminder_amount == pr.reminder_amount
    assert restored.reminder_currency == pr.reminder_currency


def test_pending_reminder_none_fields():
    pr = PendingReminder(
        sender_phone="5491100001111",
        reminder_concept="luz",
        reminder_day=None,
        reminder_amount=None,
        reminder_currency="ARS",
    )
    d = pr.to_dict()
    restored = PendingReminder.from_dict(d)
    assert restored.reminder_day is None
    assert restored.reminder_amount is None


def test_pending_reminder_default_currency():
    pr = PendingReminder(
        sender_phone="5491100001111",
        reminder_concept="netflix",
        reminder_day=15,
        reminder_amount=None,
    )
    assert pr.reminder_currency == "ARS"
    d = pr.to_dict()
    restored = PendingReminder.from_dict(d)
    assert restored.reminder_currency == "ARS"


# ---------------------------------------------------------------------------
# ConversationState with pending_reminder
# ---------------------------------------------------------------------------


def test_conversation_state_includes_pending_reminder():
    pr = PendingReminder(
        sender_phone="5491100001111",
        reminder_concept="agua",
        reminder_day=None,
        reminder_amount=Decimal("12000"),
        reminder_currency="ARS",
    )
    state = ConversationState(
        step="awaiting_reminder_data",
        pending_reminder=pr,
    )
    d = state.to_dict()
    assert d["step"] == "awaiting_reminder_data"
    assert d["pending_reminder"] is not None
    assert d["pending_reminder"]["reminder_concept"] == "agua"

    restored = ConversationState.from_dict(d)
    assert restored.step == "awaiting_reminder_data"
    assert restored.pending_reminder is not None
    assert restored.pending_reminder.reminder_concept == "agua"
    assert restored.pending_reminder.reminder_amount == Decimal("12000")


def test_conversation_state_empty_has_no_pending_reminder():
    state = ConversationState.empty()
    assert state.step == "none"
    assert state.pending_movement is None
    assert state.pending_reminder is None


def test_conversation_state_backward_compat_no_pending_reminder_key():
    """Old Redis state without 'pending_reminder' key should deserialize cleanly."""
    old_state_dict = {"step": "none", "pending_movement": None}
    state = ConversationState.from_dict(old_state_dict)
    assert state.pending_reminder is None


# ---------------------------------------------------------------------------
# ConversationService reminder methods (unit — no Redis, mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_get_pending_reminder(monkeypatch):
    """set_pending_reminder stores state; get_pending_reminder retrieves it."""
    stored = {}

    class MockClient:
        async def setex(self, key, ttl, value):
            stored[key] = value

        async def get(self, key):
            return stored.get(key)

        async def ping(self):
            return True

    async def mock_get_client():
        return MockClient()

    monkeypatch.setattr(ConversationService, "_get_client", mock_get_client)
    ConversationService._client = None

    pr = PendingReminder(
        sender_phone="5491100001111",
        reminder_concept="netflix",
        reminder_day=None,
        reminder_amount=None,
        reminder_currency="ARS",
    )
    await ConversationService.set_pending_reminder("5491100001111", pr)
    retrieved = await ConversationService.get_pending_reminder("5491100001111")

    assert retrieved is not None
    assert retrieved.reminder_concept == "netflix"
    assert retrieved.reminder_day is None


@pytest.mark.asyncio
async def test_is_awaiting_reminder_data(monkeypatch):
    stored = {}

    class MockClient:
        async def setex(self, key, ttl, value):
            stored[key] = value

        async def get(self, key):
            return stored.get(key)

        async def ping(self):
            return True

    async def mock_get_client():
        return MockClient()

    monkeypatch.setattr(ConversationService, "_get_client", mock_get_client)
    ConversationService._client = None

    pr = PendingReminder(
        sender_phone="5491100002222",
        reminder_concept="luz",
        reminder_day=None,
        reminder_amount=None,
    )
    assert not await ConversationService.is_awaiting_reminder_data("5491100002222")

    await ConversationService.set_pending_reminder("5491100002222", pr)
    assert await ConversationService.is_awaiting_reminder_data("5491100002222")


