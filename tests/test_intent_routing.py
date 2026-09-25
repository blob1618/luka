from datetime import date
from decimal import Decimal

import pytest

from app.services.conversation import LastCreatedLimit
from app.services.intent_routing import (
    normalize_limit_intent,
    normalize_movement_action,
    normalize_movement_chart_intent,
    normalize_movement_query_intent,
    references_recent_limit,
)


def test_colloquial_amount_corrections_override_new_expense_classification():
    for text, amount in (("Era por 13.000 en realidad", 13000),
                         ("Me equivoqué, fueron 13 lucas", 13000),
                         ("Fue por 13,5", 13.5)):
        result = normalize_movement_action(text, {"intent": "expense", "amount": 10000})
        assert result["intent"] == "update_movement"
        assert result["reference"] == "last_registered"
        assert result["changes"]["amount"] == amount


def recent_limit() -> LastCreatedLimit:
    return LastCreatedLimit(
        limit_id="limit-1",
        sender_phone="5491111111111",
        category_name="Comida",
        amount=Decimal("40000"),
        month=10,
        year=2026,
        currency="ARS",
    )


def test_plain_limits_is_always_list_limits():
    result = normalize_limit_intent("límites", {"intent": "out_of_scope"})
    assert result["intent"] == "list_limits"


def test_limit_status_is_budget_query_not_list():
    result = normalize_limit_intent(
        "muéstrame el estado de mis límites",
        {"intent": "list_limits"},
    )
    assert result["intent"] == "budget_query"


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("compensá mi presupuesto porque me pasé", "compensate_budget"),
        (
            "confirmá la compensación del presupuesto porque me excedí",
            "confirm_compensation",
        ),
        (
            "rechazá la compensación del presupuesto porque me excedí",
            "reject_compensation",
        ),
    ],
)
def test_compensation_intents_are_not_rerouted_to_budget_query(text, intent):
    result = normalize_limit_intent(text, {"intent": intent})
    assert result["intent"] == intent


def test_pure_budget_query_still_wins_over_limit_terms():
    result = normalize_limit_intent(
        "¿cuánto me queda del presupuesto de comida?",
        {"intent": "out_of_scope"},
    )
    assert result["intent"] == "budget_query"


def test_pure_limit_creation_is_untouched():
    result = normalize_limit_intent(
        "creá un límite de 50000 para comida",
        {"intent": "create_limit"},
    )
    assert result["intent"] == "create_limit"


def test_current_month_references_last_limit():
    result = normalize_limit_intent(
        "que sea para el mes actual",
        {"intent": "create_limit", "limit_month": None, "limit_year": None},
        last_limit=recent_limit(),
        today=date(2026, 9, 6),
    )
    assert result["intent"] == "change_limit"
    assert result["limit_month"] == 9
    assert result["limit_year"] == 2026


def test_unrelated_correction_is_not_forced_to_change_limit():
    assert references_recent_limit("en realidad gasté 5000 en comida") is False
    result = normalize_limit_intent(
        "en realidad gasté 5000 en comida",
        {"intent": "expense"},
        last_limit=recent_limit(),
    )
    assert result["intent"] == "expense"


def test_normalize_movement_query_intent_general():
    result = normalize_movement_query_intent(
        "mis últimos movimientos",
        {"intent": "out_of_scope", "reply_text": "¿Qué querés consultar?"},
    )
    assert result["intent"] == "query_movements"
    assert result.get("movement_type") is None
    assert result["reply_text"] == "Consultando tus movimientos."


def test_normalize_movement_query_intent_expenses_with_count():
    result = normalize_movement_query_intent("ultimos 3 gastos", {"intent": "out_of_scope"})
    assert result["intent"] == "query_movements"
    assert result["movement_type"] == "egreso"
    assert result["limit"] == 3


def test_normalize_movement_query_intent_incomes():
    result = normalize_movement_query_intent("mostrame mis ingresos", {"intent": "out_of_scope"})
    assert result["intent"] == "query_movements"
    assert result["movement_type"] == "ingreso"


def test_normalize_movement_query_intent_explicit_month_year_without_llm_dates():
    result = normalize_movement_query_intent(
        "gastos de enero de 2000",
        {"intent": "query_movements", "date_from": None, "date_to": None},
    )
    assert result["date_from"] == "2000-01-01"
    assert result["date_to"] == "2000-01-31"
    assert result.get("limit") is None


def test_normalize_movement_query_intent_does_not_override_expense_with_amount():
    result = normalize_movement_query_intent(
        "gasté 5000 en comida",
        {"intent": "expense", "amount": 5000.0},
    )
    assert result["intent"] == "expense"


@pytest.mark.parametrize(
    ("text", "chart_type", "ranking", "movement_type"),
    [
        ("grafico de gastos por categoria", "bar", "highest", "egreso"),
        ("torta de las categorias con menor gasto", "pie", "lowest", "egreso"),
        ("diagrama de barras de ingresos", "bar", "highest", "ingreso"),
    ],
)
def test_explicit_chart_requests_are_normalized(
    text,
    chart_type,
    ranking,
    movement_type,
):
    result = normalize_movement_chart_intent(text, {"intent": "out_of_scope"})
    assert result["intent"] == "movement_chart"
    assert result["chart_explicit"] is True
    assert result["chart_type"] == chart_type
    assert result["chart_ranking"] == ranking
    assert result["movement_type"] == movement_type


def test_non_explicit_statistics_request_is_not_a_chart():
    result = normalize_movement_chart_intent(
        "mostrame un resumen de mis gastos",
        {"intent": "expense_summary"},
    )
    assert result["intent"] == "expense_summary"
