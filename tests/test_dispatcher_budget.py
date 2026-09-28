import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.services.budget import (
    BudgetEvaluation,
    BudgetListResult,
    BudgetStatus,
    BudgetStatusResult,
)
from app.services.dispatcher import (
    DispatchResult,
    _crossed_budget_thresholds,
    _handle_budget_query,
    _register_single_with_hint,
    _confirm_pending_category_action,
    process_incoming_message,
    process_incoming_interactive_reply,
)
from app.services.conversation import PendingMovement
from app.api.whatsapp import WhatsAppReplyButton, WhatsAppReplyButtons, WhatsAppText
from app.services.webhook_idempotency import process_text_message_once
from tests.conftest import FakeRedis
from app.services.finance import MovementRegistrationResult


def budget_status(
    *,
    category="Comida",
    limit="1000",
    spent="400",
    remaining="600",
    exceeded="0",
    percentage="40.0",
    state="available",
):
    return BudgetStatus(
        limit_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        category_id=str(uuid.uuid4()),
        category_name=category,
        currency="ARS",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        limit_amount=Decimal(limit),
        spent_amount=Decimal(spent),
        remaining_amount=Decimal(remaining),
        exceeded_amount=Decimal(exceeded),
        percentage=Decimal(percentage),
        state=state,
    )


@pytest.mark.asyncio
async def test_budget_query_for_category_returns_consumed_and_available():
    status = budget_status()
    with (
        patch(
            "app.services.dispatcher._user_id_by_phone",
            return_value=uuid.uuid4(),
        ),
        patch(
            "app.services.dispatcher.BudgetService.get_status",
            return_value=BudgetStatusResult("ok", "found", status),
        ) as get_status,
    ):
        reply = await _handle_budget_query(
            "5491111111111",
            {
                "limit_category": "Comida",
                "limit_month": 9,
                "limit_year": 2026,
                "limit_currency": "ARS",
            },
        )

    assert "Gastaste $400,00 ARS de $1.000,00 ARS" in reply
    assert "Te quedan $600,00 ARS" in reply
    assert "40.0% usado" in reply
    assert get_status.call_args.args[2] == date(2026, 9, 1)


@pytest.mark.asyncio
async def test_budget_query_without_category_lists_all_budgets():
    statuses = [
        budget_status(),
        budget_status(
            category="Transporte",
            spent="1200",
            remaining="0",
            exceeded="200",
            percentage="120.0",
            state="exceeded",
        ),
    ]
    with (
        patch(
            "app.services.dispatcher._user_id_by_phone",
            return_value=uuid.uuid4(),
        ),
        patch(
            "app.services.dispatcher.BudgetService.list_statuses",
            return_value=BudgetListResult("ok", "found", statuses),
        ),
    ):
        reply = await _handle_budget_query(
            "5491111111111",
            {"limit_month": 9, "limit_year": 2026},
        )

    assert "Comida" in reply
    assert "Transporte" in reply
    assert "Superaste el límite en $200,00 ARS" in reply


@pytest.mark.asyncio
async def test_budget_query_reports_missing_limit():
    with (
        patch(
            "app.services.dispatcher._user_id_by_phone",
            return_value=uuid.uuid4(),
        ),
        patch(
            "app.services.dispatcher.BudgetService.get_status",
            return_value=BudgetStatusResult("not_found", "not found"),
        ),
    ):
        reply = await _handle_budget_query(
            "5491111111111",
            {
                "limit_category": "Comida",
                "limit_month": 9,
                "limit_year": 2026,
            },
        )

    assert reply == "No tenés un límite de Comida para Septiembre en ARS."


@pytest.mark.asyncio
async def test_budget_query_with_year_but_no_month_asks_for_month():
    reply = await _handle_budget_query(
        "5491111111111",
        {"limit_year": 2026, "limit_month": None},
    )

    assert reply == "¿Para qué mes querés consultar el presupuesto?"


@pytest.mark.asyncio
async def test_registered_expense_appends_excess_alert():
    movement_id = str(uuid.uuid4())
    exceeded = budget_status(
        spent="1200",
        remaining="0",
        exceeded="200",
        percentage="120.0",
        state="exceeded",
    )
    movement = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 1200,
        "currency": "ARS",
        "description": "supermercado",
        "category": "Comida",
    }
    with (
        patch(
            "app.services.dispatcher.FinanceService.register_movement_with_category",
            return_value=MovementRegistrationResult(
                status="registered",
                message="ok",
                movement_id=movement_id,
                user_id=str(uuid.uuid4()),
            ),
        ),
        patch(
            "app.services.dispatcher.BudgetService.evaluate_movement",
            return_value=BudgetEvaluation(
                status="ok",
                message="evaluated",
                movement_id=movement_id,
                has_limit=True,
                should_alert=True,
                budget=exceeded,
            ),
        ),
        patch(
            "app.services.dispatcher.ConversationService.set_last_movement",
            new_callable=AsyncMock,
        ),
    ):
        reply = await _register_single_with_hint(
            "5491111111111",
            "wamid.1",
            "gasté 1200 en supermercado",
            movement,
            movement,
        )

    assert "Registré tu egreso" in reply
    assert "Superaste el límite en $200,00 ARS" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            budget_status(
                spent="800",
                remaining="200",
                percentage="80.0",
                state="available",
            ),
            "Te quedan $200,00 ARS (80.0% usado)",
        ),
        (
            budget_status(
                spent="1000",
                remaining="0",
                percentage="100.0",
                state="reached",
            ),
            "Te quedan $0,00 ARS (100.0% usado)",
        ),
    ],
)
async def test_registered_expense_always_appends_matching_budget_status(status, expected):
    movement_id = str(uuid.uuid4())
    movement = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 800,
        "currency": "ARS",
        "description": "supermercado",
        "category": "Comida",
    }
    with (
        patch(
            "app.services.dispatcher.FinanceService.register_movement_with_category",
            return_value=MovementRegistrationResult(
                status="registered",
                message="ok",
                movement_id=movement_id,
                user_id=str(uuid.uuid4()),
            ),
        ),
        patch(
            "app.services.dispatcher.BudgetService.evaluate_movement",
            return_value=BudgetEvaluation(
                status="ok",
                message="evaluated",
                movement_id=movement_id,
                has_limit=True,
                should_alert=False,
                budget=status,
            ),
        ),
        patch(
            "app.services.dispatcher.ConversationService.set_last_movement",
            new_callable=AsyncMock,
        ),
    ):
        reply = await _register_single_with_hint(
            "5491111111111",
            "wamid.status",
            "gasté en supermercado",
            movement,
            movement,
        )

    assert expected in reply


@pytest.mark.asyncio
async def test_registered_expense_collects_80_percent_alert_separately():
    movement_id = str(uuid.uuid4())
    movement = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 7000,
        "currency": "ARS",
        "description": "pan francés",
        "category": "pan",
    }
    status = budget_status(
        category="pan", limit="30000", spent="24000", remaining="6000",
        percentage="80.0",
    )
    with (
        patch(
            "app.services.dispatcher.FinanceService.register_movement_with_category",
            return_value=MovementRegistrationResult(
                status="registered", message="ok", movement_id=movement_id,
                user_id=str(uuid.uuid4()),
            ),
        ),
        patch(
            "app.services.dispatcher.BudgetService.evaluate_movement",
            return_value=BudgetEvaluation(
                status="ok", message="evaluated", movement_id=movement_id,
                has_limit=True, budget=status, crossed_80_percent=True,
            ),
        ),
        patch(
            "app.services.dispatcher.ConversationService.set_last_movement",
            new_callable=AsyncMock,
        ),
    ):
        reply = await _register_single_with_hint(
            "5491111111111", "wamid.crossing", "compré pan por 7000",
            movement, movement,
        )

    assert "80.0% usado" in reply
    assert "⚠️" not in reply
    assert movement["_budget_threshold_alerts"] == [status]


def test_batch_of_expenses_warns_once_for_the_same_80_percent_crossing():
    status = budget_status(
        category="pan", limit="1000", spent="800", remaining="200",
        percentage="80.0",
    )
    evaluations = [
        BudgetEvaluation(
            status="ok", message="evaluated", movement_id=str(uuid.uuid4()),
            has_limit=True, budget=status, crossed_80_percent=True,
        )
        for _ in range(2)
    ]

    assert _crossed_budget_thresholds(evaluations) == [status]


@pytest.mark.asyncio
@pytest.mark.parametrize("crossed,configured_alert", [(True, False), (True, True), (False, False)])
async def test_budget_alert_is_sent_after_confirmation_once(crossed, configured_alert):
    budget = budget_status(
        category="pan", limit="30000", spent="24000", remaining="6000", percentage="80.0",
    )
    primary = WhatsAppReplyButtons(
        "Registrado desde el flujo personalizado.",
        (WhatsAppReplyButton("change", "Cambiar categoría"),),
    )
    alert = WhatsAppText("Aviso personalizado: pan llegó al 80 %.")
    result = DispatchResult(
        "Confirmación original.", event_key="movement.registered",
        event_variables={"movement_id": str(uuid.uuid4())},
        budget_threshold_alerts=[budget] if crossed else [],
    )

    async def render_event(**kwargs):
        if kwargs["event_key"] == "movement.registered":
            return primary
        assert kwargs["event_key"] == "budget.threshold_crossed"
        assert kwargs["variables"]["spent_amount"] == "24.000,00"
        return alert if configured_alert else None

    send = AsyncMock(return_value=True)
    kwargs = dict(
        redis_client=FakeRedis(), sender_phone="5491111111111",
        text_body="compré 7000 de pan", whatsapp_message_id="wamid.budget-alert",
        process_message=process_incoming_message, send_message=send,
    )
    with (
        patch("app.services.dispatcher._dispatch_incoming_message", AsyncMock(return_value=result)),
        patch("app.services.dispatcher.ConversationFlowRuntime.abandon", AsyncMock()),
        patch("app.services.dispatcher.ConversationFlowRuntime.render_event", AsyncMock(side_effect=render_event)),
    ):
        assert await process_text_message_once(**kwargs) == "completed"
        assert await process_text_message_once(**kwargs) == "duplicate"

    messages = [call.args[1] for call in send.await_args_list]
    assert messages[0] == primary
    assert len(messages) == (2 if crossed else 1)
    if crossed:
        if configured_alert:
            assert messages[1] == alert
        else:
            assert isinstance(messages[1], WhatsAppText)
            assert "Alerta de presupuesto" in messages[1].body
            assert "80 % o más" in messages[1].body


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["registered", "duplicate", "persistence_error"])
async def test_category_confirmation_alert_requires_successful_new_registration(status):
    pending = PendingMovement(
        "5491111111111", "wamid.category", "compré pan por 7000",
        "egreso", Decimal("7000"), "ARS", "pan francés", "pan",
    )
    budget = budget_status(spent="800", remaining="200", percentage="80.0")
    evaluation = BudgetEvaluation(
        "ok", "evaluated", has_limit=True, budget=budget, crossed_80_percent=True,
    )
    with (
        patch("app.services.dispatcher.ConversationService.get_pending_movement", AsyncMock(return_value=pending)),
        patch("app.services.dispatcher.FinanceService.register_movement_with_category", return_value=MovementRegistrationResult(
            status, "ok", movement_id=str(uuid.uuid4()),
        )),
        patch("app.services.dispatcher.BudgetService.evaluate_movement", return_value=evaluation) as evaluate,
    ):
        result = await _confirm_pending_category_action(pending.sender_phone)

    if status != "registered":
        evaluate.assert_not_called()
        assert result.budget_threshold_alerts == []
        return
    assert result.budget_threshold_alerts == [budget]
    with (
        patch("app.services.dispatcher.ConversationFlowRuntime.handle_reply", AsyncMock(return_value=result)),
        patch("app.services.dispatcher.ConversationFlowRuntime.render_event", AsyncMock(return_value=None)),
    ):
        result = await process_incoming_interactive_reply(
            sender_phone=pending.sender_phone, option_id="flow.category.confirm", reply_type="button_reply",
        )
    assert "Registré tu egreso" in result.reply_text
    assert len(result.followup_messages) == 1
    assert "Alerta de presupuesto" in result.followup_messages[0].body
