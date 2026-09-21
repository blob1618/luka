"""Validated chart requests and explicit, conservative conversational routing."""

import calendar
import re
import unicodedata
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


MONTH_NAMES = (
    "Ene",
    "Feb",
    "Mar",
    "Abr",
    "May",
    "Jun",
    "Jul",
    "Ago",
    "Sep",
    "Oct",
    "Nov",
    "Dic",
)


def normalized(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    ).strip()


def month_start(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError(
            "Indicá los meses con mes y año, por ejemplo septiembre de 2026."
        )
    try:
        return date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise ValueError(
            "Ese mes o año no es válido. Indicá, por ejemplo, septiembre de 2026."
        ) from exc


def month_end(value: date) -> date:
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def month_label(value: str) -> str:
    day = month_start(value)
    return f"{MONTH_NAMES[day.month - 1]} {day.year}"


class ChartRequest(BaseModel):
    chart_mode: Literal[
        "category", "movement_comparison", "monthly", "month_comparison"
    ] = "category"
    movement_type: Literal["egreso", "ingreso", "both"] = "egreso"
    chart_type: Literal["bar", "pie"] = "bar"
    chart_ranking: Literal["highest", "lowest"] = "highest"
    chart_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    chart_categories: list[str] = Field(default_factory=list, max_length=6)
    chart_limit: int = Field(default=5, ge=1, le=5, strict=True)
    chart_percentages: bool = True
    date_from: date | None = None
    date_to: date | None = None
    chart_start_month: str | None = None
    chart_end_month: str | None = None
    chart_months: list[str] = Field(default_factory=list, max_length=2)

    def periods(self, today: date) -> list[tuple[str, date, date]]:
        if self.chart_mode == "monthly":
            if not self.chart_start_month:
                raise ValueError("¿Desde qué mes y año querés la evolución mensual?")
            start = month_start(self.chart_start_month)
            end = (
                month_start(self.chart_end_month)
                if self.chart_end_month
                else today.replace(day=1)
            )
            count = (end.year - start.year) * 12 + end.month - start.month + 1
            if count < 1 or end > today:
                raise ValueError(
                    "Elegí un mes inicial anterior al final y meses que no estén en el futuro."
                )
            if count > 24:
                raise ValueError(
                    "Puedo mostrar hasta 24 meses por pedido. Indicá un mes inicial más reciente."
                )
            months = []
            for index in range(count):
                offset = start.year * 12 + start.month - 1 + index
                months.append(date(offset // 12, offset % 12 + 1, 1))
        elif self.chart_mode == "month_comparison":
            if len(set(self.chart_months)) != 2:
                raise ValueError(
                    "¿Qué dos meses querés comparar? Indicá mes y año de cada uno."
                )
            months = sorted(month_start(value) for value in self.chart_months)
            if months[-1] > today:
                raise ValueError("Elegí dos meses que no estén en el futuro.")
        else:
            start, end = self.date_from, self.date_to
            if start is None and end is None:
                start, end = today.replace(day=1), month_end(today)
            if start is None or end is None or start > end:
                raise ValueError(
                    "No pude determinar el período completo. Indicá desde qué fecha y hasta qué fecha."
                )
            self.date_from, self.date_to = start, end
            return [("", start, end)]
        return [
            (value.strftime("%Y-%m"), value, min(month_end(value), today))
            for value in months
        ]


CHART_FIELDS = tuple(ChartRequest.model_fields)


def chart_patch(data: dict) -> dict:
    """Null means unspecified, while [] explicitly clears a category filter."""
    return {key: data[key] for key in CHART_FIELDS if data.get(key) is not None}


def is_chart_followup(text: str) -> bool:
    value = normalized(text)
    if re.search(
        r"\b(gaste|pague|cobre|anul\w*|elimin\w*|record\w*|registr\w*|anot\w*|limite|presupuesto)\b",
        value,
    ):
        return False
    return bool(
        re.search(
            r"^(ahora|mejor|que sea|cambialo|cambia el grafico|solo|solamente|con porcentajes|sin porcentajes|compar[aá]|y (?:en|del|de|con))\b",
            value,
        )
    )


def normalize_chart(text: str, data: dict, *, has_context: bool = False) -> dict:
    result = dict(data)
    value = normalized(text)
    chart_word = bool(re.search(r"\b(grafico\w*|diagrama\w*)\b", value))
    pie_word = bool(re.search(r"\b(torta|pastel)\b", value))
    finance = bool(
        re.search(r"\b(gastos?|egresos?|ingresos?|categorias?|movimientos?)\b", value)
    )
    explicit = (chart_word or pie_word) and (
        finance
        or data.get("intent") == "movement_chart"
        or (has_context and is_chart_followup(text))
    )
    if (
        data.get("intent") == "expense"
        and data.get("amount") is not None
        and not chart_word
    ):
        return result
    if re.search(r"\b(dashboard|panel|web|link|enlace)\b", value):
        return result
    continuation = (
        has_context
        and is_chart_followup(text)
        and (explicit or data.get("intent") == "movement_chart")
    )
    missing_context = (
        not has_context
        and is_chart_followup(text)
        and bool(
            re.search(
                r"^(que sea|cambialo|ahora|mejor|solo|solamente|con porcentajes|sin porcentajes)\b",
                value,
            )
        )
        and data.get("intent") == "movement_chart"
    )
    if missing_context:
        return {
            **result,
            "intent": "movement_chart",
            "chart_explicit": True,
            "chart_missing_context": True,
        }
    if not explicit and not continuation:
        return result
    # A chart word alone is enough: expenses/current month remain the defaults.
    result.update(
        intent="movement_chart", chart_explicit=True, chart_followup=continuation
    )
    if "compar" in value and re.search(r"\b(dias?|semanas?)\b", value):
        result["chart_mode"] = "unsupported"
    if re.search(r"\b(torta|pastel|circular)\b", value):
        result["chart_type"] = "pie"
    elif "barra" in value:
        result["chart_type"] = "bar"
    if "ingreso" in value and re.search(r"gast|egres", value):
        result["movement_type"] = "both"
        if not result.get("chart_mode") or result["chart_mode"] == "category":
            result["chart_mode"] = "movement_comparison"
    elif "ingreso" in value:
        result["movement_type"] = "ingreso"
    elif re.search(r"gast|egres", value):
        result["movement_type"] = "egreso"
    if re.search(r"mes a mes|evolucion|mensual", value) and "compar" not in value:
        result["chart_mode"] = "monthly"
    if re.search(r"\b(menor\w*|menos|mas bajo\w*)\b", value):
        result["chart_ranking"] = "lowest"
    elif re.search(r"\b(mayor\w*|mas alto\w*)\b", value):
        result["chart_ranking"] = "highest"
    for currency, pattern in (
        ("ARS", r"\b(ars|pesos?)\b"),
        ("USD", r"\b(usd|dolares?)\b"),
        ("EUR", r"\b(eur|euros?)\b"),
    ):
        if re.search(pattern, value):
            result["chart_currency"] = currency
    if "sin porcentajes" in value:
        result["chart_percentages"] = False
    elif "porcentaje" in value:
        result["chart_percentages"] = True
    if not continuation:
        for key, default in (
            ("movement_type", "egreso"),
            ("chart_type", "bar"),
            ("chart_ranking", "highest"),
        ):
            if result.get(key) is None:
                result[key] = default
    return result
