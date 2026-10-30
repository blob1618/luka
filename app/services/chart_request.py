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
FULL_MONTH_NAMES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
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
                start, end = today.replace(day=1), today
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
