"""Tests de PendingCompensation (estado multi-turno de compensación de presupuesto).

Usa el FakeRedis autouse de conftest.py a través de ConversationService.
"""

import pytest

from app.services.conversation import (
    ConversationService,
    ConversationState,
    PendingCompensation,
)


PROPOSAL = {
    "proposal_id": "prop-1",
    "user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "currency": "ARS",
    "period_start": "2026-09-01",
    "period_end": "2026-09-30",
    "amount": "1500.00",
    "target": {
        "limit_id": "limit-1",
        "category_id": "cat-1",
        "category_name": "Supermercado",
        "before_limit": "10000.00",
        "after_limit": "11500.00",
        "spent_amount": "11000.00",
        "available_before": "-1000.00",
    },
    "donors": [
        {
            "limit_id": "limit-2",
            "category_id": "cat-2",
            "category_name": "Transporte",
            "before_limit": "5000.00",
            "after_limit": "3500.00",
            "spent_amount": "1000.00",
            "available_before": "4000.00",
        }
    ],
    "created_at": "2026-09-20T10:00:00-03:00",
    "expires_at": "2026-09-20T10:30:00-03:00",
    "snapshot": {"limit-1": "10000.00", "limit-2": "5000.00"},
}


class TestPendingCompensation:
    def test_round_trip_preserves_nested_proposal(self):
        pending = PendingCompensation(sender_phone="5491100000001", proposal=PROPOSAL)

        restored = PendingCompensation.from_dict(pending.to_dict())

        assert restored.sender_phone == "5491100000001"
        assert restored.proposal == PROPOSAL
        assert restored.proposal["target"]["category_name"] == "Supermercado"
        assert restored.proposal["donors"][0]["before_limit"] == "5000.00"


class TestConversationState:
    def test_from_dict_with_pending_compensation(self):
        state = ConversationState.from_dict(
            {
                "step": "awaiting_compensation_confirmation",
                "pending_compensation": {
                    "sender_phone": "5491100000001",
                    "proposal": PROPOSAL,
                },
            }
        )

        assert state.step == "awaiting_compensation_confirmation"
        assert state.pending_compensation is not None
        assert state.pending_compensation.proposal["proposal_id"] == "prop-1"

    def test_from_dict_without_pending_compensation_is_retrocompatible(self):
        state = ConversationState.from_dict({"step": "none"})

        assert state.pending_compensation is None


@pytest.mark.asyncio
async def test_set_check_get_and_clear_pending_compensation():
    phone = "5491100000001"

    await ConversationService.set_pending_compensation(phone, PROPOSAL)

    assert await ConversationService.is_awaiting_compensation_confirmation(phone)
    pending = await ConversationService.get_pending_compensation(phone)
    assert pending is not None
    assert pending.sender_phone == phone
    assert pending.proposal == PROPOSAL

    await ConversationService.clear_state(phone)

    assert not await ConversationService.is_awaiting_compensation_confirmation(phone)
    assert await ConversationService.get_pending_compensation(phone) is None


@pytest.mark.asyncio
async def test_get_pending_compensation_returns_none_when_empty():
    phone = "5491100000002"

    assert await ConversationService.get_pending_compensation(phone) is None
    assert not await ConversationService.is_awaiting_compensation_confirmation(phone)
