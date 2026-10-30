from datetime import date
from decimal import Decimal

import pytest

from tests.test_movement_chart import (
    chart_db as _chart_db_fixture,
    _user,
    _category,
    _movement,
)
from app.models.database import MovimientoFinanciero
from app.services.chart_request import ChartRequest
from app.services.chart_query import ChartQueryResult, ChartValue, query_chart
from app.services.chart_service import prepare_charts
from app.services.finance import CategoryMovementTotal
from app.services.movement_chart import MovementChartService

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
