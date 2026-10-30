"""Registered-balance presenter: real backend values without a classifier."""
from datetime import date
from decimal import Decimal
import app.services.monthly_balance as monthly_balance_module
from app.services.monthly_balance import MonthlyBalanceResult
from app.services.assistant_capabilities.financial_presenters import render_registered_balance

def _balance_result(status, *, currencies=(), start=date(2026, 9, 1), end=date(2026, 10, 1)):
    return MonthlyBalanceResult(
        status=status,
        period_start=start,
        period_end=end,
        currencies=list(currencies),
    )


def _currency(currency, income, expense, difference):
    return monthly_balance_module.CurrencyBalance(
        currency=currency,
        income=Decimal(income),
        expense=Decimal(expense),
        difference=Decimal(difference),
        movement_count=1,
    )


def test_render_balance_uses_only_the_monthly_balance_result():
    result = _balance_result("ok", currencies=[_currency("ARS", "1000.00", "400.00", "600.00")])

    text = render_registered_balance(result)

    assert "01/09/2026 al 30/09/2026" in text
    assert "ARS" in text
    assert "diferencia 600,00 (sobrante positivo registrado)" in text


def test_zero_difference_is_not_a_positive_surplus():
    result = _balance_result("zero_difference", currencies=[_currency("ARS", "500.00", "500.00", "0")])

    text = render_registered_balance(result)

    assert "No hay un sobrante positivo registrado" in text
    assert "diferencia 0,00 (sin sobrante positivo registrado)" in text
    assert "Hay un sobrante positivo" not in text


def test_negative_difference_is_not_a_positive_surplus():
    result = _balance_result("ok", currencies=[_currency("ARS", "1000.00", "1500.00", "-500.00")])

    text = render_registered_balance(result)

    assert "diferencia -500,00 (sin sobrante positivo registrado)" in text
    assert "Hay un sobrante positivo" not in text


def test_statuses_never_become_zero_or_guessed_values():
    for status, expected in [
        ("no_data", "No hay movimientos registrados"),
        ("user_not_found", "No encuentro una cuenta vinculada"),
        ("error", "No pude calcular"),
    ]:
        result = _balance_result(status)
        text = render_registered_balance(result)

        assert expected in text
        assert "0,00" not in text
        assert "sobrante positivo registrado" not in text
