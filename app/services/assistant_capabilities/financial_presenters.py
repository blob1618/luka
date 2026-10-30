"""Backend-only registered-balance presentation; no language routing."""
from datetime import timedelta
from decimal import Decimal
from app.services.monthly_balance import MonthlyBalanceResult

DISCLAIMER = (
    "Estas cifras surgen únicamente de los movimientos registrados: pueden "
    "faltar movimientos no cargados o pagos futuros, y no demuestran que el "
    "dinero esté disponible."
)

POSITIVE_OFFER = (
    "Si querés, puedo ampliar con orientación general sobre ahorro e inversión "
    "(por ejemplo, reserva para imprevistos, plazo fijo o FCI)."
)

def _format_amount(value: Decimal) -> str:
    quantized = Decimal(value).quantize(Decimal("0.01"))
    if quantized == 0:
        quantized = abs(quantized)
    formatted = f"{quantized:,.2f}"
    integer, _, decimals = formatted.partition(".")
    return f"{integer.replace(',', '.')},{decimals}"

def _format_period(result: MonthlyBalanceResult) -> str:
    inclusive_end = result.period_end - timedelta(days=1)
    return (
        f"{result.period_start.strftime('%d/%m/%Y')} al "
        f"{inclusive_end.strftime('%d/%m/%Y')}"
    )

def _has_positive_difference(result: MonthlyBalanceResult) -> bool:
    return any(balance.difference > 0 for balance in result.currencies)

def render_registered_balance(result: MonthlyBalanceResult) -> str:
    period = _format_period(result)
    if result.status == "user_not_found":
        return (
            f"No encuentro una cuenta vinculada a este número, así que no puedo "
            f"calcular el sobrante del período {period}."
        )
    if result.status == "no_data":
        return (
            f"No hay movimientos registrados en el período {period}. Sin registros "
            "no puedo calcular un sobrante y no lo reemplazo por cero."
        )
    if result.status == "error":
        return (
            f"No pude calcular el período {period} en este momento. Intentá "
            "nuevamente en unos minutos."
        )

    lines = [f"Movimientos registrados del mes cerrado ({period}):"]
    if _has_positive_difference(result):
        lines.append(
            "Hay un sobrante positivo registrado en al menos una moneda "
            "(sin convertir entre monedas)."
        )
    else:
        lines.append("No hay un sobrante positivo registrado en el período.")
    lines.append("")
    for balance in result.currencies:
        verdict = (
            "sobrante positivo registrado"
            if balance.difference > 0
            else "sin sobrante positivo registrado"
        )
        lines.append(
            f"- {balance.currency}: ingresos {_format_amount(balance.income)}, "
            f"egresos {_format_amount(balance.expense)}, "
            f"diferencia {_format_amount(balance.difference)} ({verdict})."
        )
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)
