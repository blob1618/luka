"""Category regressions reported in STK-245."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.dispatcher import (
    _DispatchContext,
    _handle_finance_intent,
    _multiop_registration_reply,
    _normalize_extracted_message,
    _register_and_reply_with_hint,
)
from app.services.finance import CategoryResult, MovementRegistrationResult
from app.services.intent_routing import normalize_category_management_intent


@pytest.mark.asyncio
async def test_uber_evidence_overrides_unrelated_llm_category_before_persistence():
    data = {
        "intent": "expense", "movement_type": "egreso", "amount": 10000,
        "currency": "ARS", "description": "Uber", "category": "Supermercado",
    }
    with (
        patch(
            "app.services.dispatcher.FinanceService.register_movement_with_category",
            return_value=MovementRegistrationResult(
                status="needs_category_confirmation", message="confirm",
                category_name="Transporte",
            ),
        ) as register,
        patch(
            "app.services.dispatcher.ConversationService.set_pending_movement",
            new_callable=AsyncMock,
        ) as save_pending,
    ):
        reply = await _register_and_reply_with_hint(
            "5491111111111", "wamid.uber", "Gasté 10000 ARS en Uber", data,
        )

    assert register.call_args.kwargs["category_name"] == "Transporte"
    assert "Transporte" in reply
    assert save_pending.call_args.args[1].inferred_category == "Transporte"


def test_uncategorized_registration_discloses_actual_state():
    reply = _multiop_registration_reply(
        [MovementRegistrationResult(status="registered", message="ok", category_name=None)],
        {"movement_type": "egreso", "amount": 12000, "currency": "ARS",
         "description": "regalo sorpresa"},
    )
    assert "sin categoría" in reply
    assert "crear una" in reply


@pytest.mark.asyncio
async def test_explicit_create_and_rename_category_intents_use_owned_finance_service():
    user_id = uuid4()
    ctx = _DispatchContext(
        sender_phone="5491111111111", text_body="Creá la categoría Jardinería",
        whatsapp_message_id="wamid.create", conversation_history=None,
    )
    with (
        patch("app.services.dispatcher._user_id_by_phone", return_value=user_id),
        patch(
            "app.services.dispatcher.FinanceService.create_category",
            return_value=CategoryResult(status="created", message="ok", category_name="Jardinería"),
        ) as create,
        patch(
            "app.services.dispatcher.FinanceService.rename_category",
            return_value=CategoryResult(status="renamed", message="ok", category_name="Plantas"),
        ) as rename,
    ):
        created = await _handle_finance_intent(
            ctx, {"intent": "create_category", "category": "Jardinería"},
        )
        renamed = await _handle_finance_intent(
            ctx, {"intent": "rename_category", "category": "Jardinería",
                  "changes": {"category": "Plantas"}},
        )

    assert created.intent == "create_category"
    assert "Jardinería" in created.reply_text
    assert renamed.intent == "rename_category"
    assert "Plantas" in renamed.reply_text
    create.assert_called_once_with(user_id, "Jardinería")
    rename.assert_called_once_with(user_id, "Jardinería", "Plantas")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_intent", "expected_category", "expected_target"),
    [
        ("Creá la categoría Jardinería", "create_category", "Jardinería", None),
        ("Renombrá la categoría Jardinería a Plantas", "rename_category", "Jardinería", "Plantas"),
    ],
)
async def test_chat_category_commands_override_false_confirmation(
    message, expected_intent, expected_category, expected_target,
):
    ctx = _DispatchContext(
        sender_phone="5491111111111", text_body=message,
        whatsapp_message_id="wamid.category", conversation_history=None,
    )
    data = await _normalize_extracted_message(
        ctx, {"intent": "confirm_category", "category": None},
    )

    assert data["intent"] == expected_intent
    assert data["category"] == expected_category
    if expected_target is not None:
        assert data["changes"]["category"] == expected_target


def test_category_management_route_does_not_replace_movement_reclassification():
    movement = {"intent": "update_movement", "changes": {"category": "Transporte"}}

    assert normalize_category_management_intent(
        "Cambiá la categoría del último gasto a Transporte", movement,
    ) is movement
