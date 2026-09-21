from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_movement_chart import (
    chart_db as _chart_db_fixture,
    _user,
    _category,
    _movement,
)
from app.api.whatsapp import WhatsAppImage
from app.models.database import MovimientoFinanciero
from app.services.conversation import ConversationService
from app.services.dispatcher import process_incoming_message
from app.services.chart_request import ChartRequest, normalize_chart
from app.services.chart_query import ChartQueryResult, ChartValue, query_chart
from app.services.chart_service import generate_chart, prepare_charts
from app.services.finance import CategoryMovementTotal
from app.services.movement_chart import MovementChartService
from app.services.onboarding import OnboardingDecision, OnboardingResult

TODAY = date(2026, 9, 21)
PHONE = "5491100000001"
chart_db = _chart_db_fixture


def query(session, **kwargs):
    request = ChartRequest(**kwargs)
    return query_chart(PHONE, request, request.periods(TODAY), session=session)


def test_aggregation_filters_user_type_period_currency_and_annulment(chart_db):
    user = _user(chart_db, PHONE)
    other = _user(chart_db, "5491100000002")
    category = _category(chart_db, user, "Ocio")
    _movement(chart_db, user, amount="100", category=category)
    _movement(chart_db, user, amount="50", category=category)
    _movement(chart_db, user, amount="999", category=category, annulled=True)
    _movement(chart_db, user, amount="888", movement_date=date(2026, 8, 1))
    _movement(chart_db, user, amount="777", movement_type="ingreso")
    _movement(chart_db, other, amount="666")
    count = chart_db.query(MovimientoFinanciero).count()
    result = query(chart_db)
    assert result.rows == [ChartValue("Ocio", "2026-09", "egreso", Decimal("150"))]
    assert chart_db.query(MovimientoFinanciero).count() == count


def test_category_filter_applies_before_currency_and_denominator(chart_db):
    user = _user(chart_db, PHONE)
    ocio = _category(chart_db, user, "Ocio")
    comida = _category(chart_db, user, "Comida")
    _movement(chart_db, user, amount="100", category=ocio)
    _movement(chart_db, user, amount="200", category=comida, currency="USD")
    result = query(chart_db, chart_categories=["ocio"])
    assert result.currency == "ARS"
    assert len(result.rows) == 1
    unknown = query(chart_db, chart_categories=["inexistente"])
    assert unknown.status == "needs_categories"
    assert "Ocio" in unknown.options


def test_comparison_does_not_mix_currencies_between_types_or_months(chart_db):
    user = _user(chart_db, PHONE)
    _movement(chart_db, user, amount="100")
    _movement(chart_db, user, amount="50", movement_type="ingreso", currency="USD")
    assert query(chart_db, movement_type="both").status == "needs_currency"
    _movement(
        chart_db, user, amount="300", movement_date=date(2026, 8, 1), currency="USD"
    )
    args = dict(chart_mode="month_comparison", chart_months=["2026-08", "2026-09"])
    assert query(chart_db, **args).status == "needs_currency"
    assert query(chart_db, chart_currency="ARS", **args).currency == "ARS"


def test_only_selected_months_not_gap_contribute(chart_db):
    user = _user(chart_db, PHONE)
    _movement(chart_db, user, amount="10", movement_date=date(2026, 1, 1))
    _movement(
        chart_db, user, amount="900", movement_date=date(2026, 2, 1), currency="USD"
    )
    _movement(chart_db, user, amount="20", movement_date=date(2026, 3, 1))
    result = query(
        chart_db, chart_mode="month_comparison", chart_months=["2026-01", "2026-03"]
    )
    assert result.status == "ok"
    assert sum(row.amount for row in result.rows) == 30


def test_category_names_must_resolve_uniquely(chart_db):
    user = _user(chart_db, PHONE)
    _category(chart_db, user, "Ocio")
    _category(chart_db, user, "Ócio")
    assert query(chart_db, chart_categories=["OCIO"]).status == "needs_categories"
    assert query(chart_db, chart_categories=["Ocio"]).status == "ok"


def test_monthly_periods_include_missing_months_and_year_rollover():
    request = ChartRequest(
        chart_mode="monthly", chart_start_month="2025-11", chart_end_month="2026-02"
    )
    periods = request.periods(TODAY)
    assert [p[0] for p in periods] == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert periods[-1][2] == date(2026, 2, 28)
    request.chart_end_month = None
    assert request.periods(TODAY)[-1][2] == TODAY


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chart_mode": "monthly"},
        {"chart_mode": "monthly", "chart_start_month": "2026-13"},
        {"chart_mode": "monthly", "chart_start_month": "2026-10"},
        {"chart_mode": "monthly", "chart_start_month": "2020-01"},
        {"chart_mode": "month_comparison", "chart_months": ["2026-09", "2026-09"]},
        {"chart_mode": "month_comparison", "chart_months": ["2026-09"]},
        {"date_from": "2026-09-01"},
    ],
)
def test_ambiguous_periods_require_clarification(kwargs):
    with pytest.raises(ValueError):
        ChartRequest(**kwargs).periods(TODAY)


@pytest.mark.parametrize("ranking", ["highest", "lowest"])
def test_ranking_keeps_requested_n_plus_others_and_exact_total(ranking):
    spec = MovementChartService.prepare(
        [CategoryMovementTotal(f"C{i}", Decimal(i)) for i in range(1, 8)],
        movement_type="egreso",
        currency="ARS",
        start_date=TODAY,
        end_date=TODAY,
        ranking=ranking,
        limit=3,
    )
    assert len(spec.categories) == 4
    assert spec.categories[0].category_name == ("C7" if ranking == "highest" else "C1")
    assert spec.categories[-1].category_name == "Otros"
    assert sum(c.amount for c in spec.categories) == spec.total == 28


def test_monthly_pages_share_scale_and_include_zero_months():
    request = ChartRequest(chart_mode="monthly", chart_start_month="2026-01")
    result = ChartQueryResult(
        "ok", [ChartValue("Ocio", "2026-09", "egreso", Decimal(120))], "ARS"
    )
    specs = prepare_charts(request, result, request.periods(TODAY), TODAY)
    assert [len(s.categories) for s in specs] == [6, 3]
    assert all(s.scale_max == 120 for s in specs)
    assert specs[0].categories[0].amount == 0
    assert specs[1].categories[-1].amount == 120
    assert "parcial" in specs[-1].note
    assert MovementChartService.render_png(specs[0]).startswith(b"\x89PNG")


def test_comparison_shows_missing_income_as_zero_and_balance():
    request = ChartRequest(chart_mode="movement_comparison", movement_type="both")
    result = ChartQueryResult(
        "ok", [ChartValue("Ocio", "2026-09", "egreso", Decimal(100))], "ARS"
    )
    spec = prepare_charts(request, result, request.periods(TODAY), TODAY)[0]
    assert [c.amount for c in spec.categories] == [0, 100]
    assert spec.summary == "Saldo  -100 ARS"
    assert "sin movimientos registrados" in spec.note


def test_month_comparison_reports_delta_without_division_by_zero():
    request = ChartRequest(
        chart_mode="month_comparison", chart_months=["2026-08", "2026-09"]
    )
    result = ChartQueryResult(
        "ok", [ChartValue("Ocio", "2026-09", "egreso", Decimal(100))], "ARS"
    )
    spec = prepare_charts(request, result, request.periods(TODAY), TODAY)[0]
    assert "base cero" in spec.note
    assert "100 ARS" in spec.note


@pytest.mark.asyncio
async def test_pending_pie_comparison_offers_concrete_choices():
    reply = await generate_chart(
        PHONE,
        {
            "chart_mode": "movement_comparison",
            "movement_type": "both",
            "chart_type": "pie",
        },
        today=TODAY,
    )
    assert not reply.images
    assert "1. Comparar en barras" in reply.text
    assert "2. Pastel de gastos" in reply.text
    assert "3. Pastel de ingresos" in reply.text
    assert (
        await ConversationService.get_pending_movement_chart(PHONE)
    ).reason == "distribution"


@pytest.mark.asyncio
async def test_temporal_pie_choices_have_month_and_distribution():
    reply = await generate_chart(
        PHONE,
        {
            "chart_mode": "month_comparison",
            "chart_months": ["2026-08", "2026-09"],
            "chart_type": "pie",
        },
        today=TODAY,
    )
    assert "Barras por mes" in reply.text
    assert "Pastel de Ago 2026" in reply.text
    assert "Pastel de Sep 2026" in reply.text


@pytest.mark.asyncio
async def test_no_data_does_not_render(chart_db):
    _user(chart_db, PHONE)
    with patch("app.services.chart_service.MovementChartService.render_png") as render:
        reply = await generate_chart(PHONE, {}, today=TODAY)
    assert "No encontré gastos" in reply.text
    render.assert_not_called()


@pytest.mark.asyncio
async def test_multi_page_response_and_last_spec_are_isolated_by_user(chart_db):
    user = _user(chart_db, PHONE)
    _movement(chart_db, user, amount="100")
    reply = await generate_chart(
        PHONE, {"chart_mode": "monthly", "chart_start_month": "2026-01"}, today=TODAY
    )
    assert len(reply.images) == 2
    assert all(
        isinstance(image, WhatsAppImage) and len(image.content) < 5 * 1024 * 1024
        for image in reply.images
    )
    saved = await ConversationService.get_last_chart(PHONE)
    assert saved["chart_currency"] == "ARS"
    assert await ConversationService.get_last_chart("other") is None


@pytest.mark.asyncio
async def test_dispatcher_followup_preserves_filters_and_options_skip_llm(chart_db):
    user = _user(chart_db, PHONE)
    category = _category(chart_db, user, "Ocio")
    _movement(chart_db, user, amount="100", category=category)
    await generate_chart(
        PHONE,
        {
            "chart_categories": ["Ocio"],
            "date_from": "2026-09-01",
            "date_to": "2026-09-30",
        },
        today=TODAY,
    )
    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=OnboardingResult(OnboardingDecision.KNOWN_USER),
        ),
        patch(
            "app.services.dispatcher.build_user_context",
            return_value="FECHA ACTUAL: 2026-09-21",
        ),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value={"intent": "movement_chart", "chart_type": "pie"},
        ) as llm,
    ):
        result = await process_incoming_message(PHONE, "Que sea un gráfico pastel")
        assert isinstance(result.reply_message, WhatsAppImage)
        saved = await ConversationService.get_last_chart(PHONE)
        assert saved["chart_categories"] == ["Ocio"] and saved["chart_type"] == "pie"
        await generate_chart(
            PHONE,
            {**saved, "movement_type": "both", "chart_mode": "movement_comparison"},
            today=TODAY,
        )
        llm.reset_mock()
        chosen = await process_incoming_message(PHONE, "1")
        assert isinstance(chosen.reply_message, WhatsAppImage)
        llm.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "Gasté 500 en comida",
        "Ahora anotá 500 de gasto",
        "Hola",
        "Ahora recordame pagar luz",
    ],
)
def test_ordinary_messages_do_not_trigger_chart_followups(text):
    result = normalize_chart(text, {"intent": "expense"}, has_context=True)
    assert not result.get("chart_explicit")


def test_limit_change_cannot_be_hijacked_by_chart_context():
    result = normalize_chart(
        "Que sea 3000",
        {"intent": "change_limit", "limit_amount": 3000},
        has_context=True,
    )
    assert result["intent"] == "change_limit"


def test_expired_context_does_not_invent_a_new_chart():
    result = normalize_chart(
        "Que sea pastel", {"intent": "movement_chart", "chart_type": "pie"}
    )
    assert result["chart_missing_context"]


def test_purchase_of_a_cake_is_not_a_pie_chart():
    result = normalize_chart(
        "Gasté 500 en torta", {"intent": "expense", "amount": 500}, has_context=True
    )
    assert result["intent"] == "expense"
    assert not result.get("chart_explicit")


def test_non_financial_graph_is_not_replaced_by_expenses():
    result = normalize_chart(
        "Quiero un gráfico de temperaturas", {"intent": "out_of_scope"}
    )
    assert not result.get("chart_explicit")


@pytest.mark.asyncio
async def test_currency_choice_preserves_months_and_category(chart_db):
    user = _user(chart_db, PHONE)
    ocio = _category(chart_db, user, "Ocio")
    _movement(chart_db, user, category=ocio, amount="100")
    _movement(chart_db, user, category=ocio, amount="30", currency="USD")
    initial = {
        "chart_mode": "month_comparison",
        "chart_months": ["2026-08", "2026-09"],
        "chart_categories": ["Ocio"],
    }
    reply = await generate_chart(PHONE, initial, today=TODAY)
    assert "1. ARS" in reply.text and "2. USD" in reply.text
    with patch(
        "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
        return_value=OnboardingResult(OnboardingDecision.KNOWN_USER),
    ):
        result = await process_incoming_message(PHONE, "USD")
    assert isinstance(result.reply_message, WhatsAppImage)
    last = await ConversationService.get_last_chart(PHONE)
    assert last["chart_months"] == initial["chart_months"]
    assert last["chart_categories"] == ["Ocio"] and last["chart_currency"] == "USD"


@pytest.mark.asyncio
async def test_reset_context_removes_chart_specification():
    from app.services.dispatcher import _handle_reset_context

    await ConversationService.set_last_chart(PHONE, {"chart_type": "pie"})
    await _handle_reset_context(PHONE)
    assert await ConversationService.get_last_chart(PHONE) is None


@pytest.mark.asyncio
async def test_render_failure_does_not_save_or_send_a_partial_chart(chart_db):
    user = _user(chart_db, PHONE)
    _movement(chart_db, user, amount="100")
    with patch(
        "app.services.chart_service.MovementChartService.render_png",
        side_effect=[b"png", RuntimeError("render failed")],
    ):
        reply = await generate_chart(
            PHONE,
            {"chart_mode": "monthly", "chart_start_month": "2026-01"},
            today=TODAY,
        )
    assert not reply.images and "No pude generar" in reply.text
    assert await ConversationService.get_last_chart(PHONE) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "fields"),
    [
        (
            "Mostrame un gráfico de gastos mes a mes desde enero de 2026",
            {
                "chart_mode": "monthly",
                "chart_start_month": "2026-01",
                "chart_end_month": "2026-09",
            },
        ),
        (
            "Gráfico de gastos comparando agosto y septiembre de 2026",
            {"chart_mode": "month_comparison", "chart_months": ["2026-08", "2026-09"]},
        ),
        (
            "Gráfico de egresos comparados con ingresos para ocio",
            {"chart_categories": ["Ocio"]},
        ),
    ],
)
async def test_natural_language_contract_reaches_chart_service(chart_db, text, fields):
    user = _user(chart_db, PHONE)
    ocio = _category(chart_db, user, "Ocio")
    _movement(chart_db, user, category=ocio, amount="100")
    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=OnboardingResult(OnboardingDecision.KNOWN_USER),
        ),
        patch(
            "app.services.dispatcher.build_user_context",
            return_value="FECHA ACTUAL: 2026-09-21",
        ),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value={"intent": "movement_chart", **fields},
        ),
    ):
        result = await process_incoming_message(PHONE, text)
    assert isinstance(result.reply_message, WhatsAppImage)
    if fields.get("chart_mode") == "monthly":
        assert len(result.followup_messages) == 1
