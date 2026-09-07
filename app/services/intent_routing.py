"""Deterministic corrections for high-confidence limit utterances.

The LLM remains the general natural-language parser.  This module only fixes
short or referential phrases whose product meaning is unambiguous and for which
an incorrect intent would trigger the wrong operation.
"""

import re
import unicodedata
from datetime import date

from app.services.conversation import LastCreatedLimit


_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

_LIST_LIMITS = re.compile(
    r"^(?:mostra(?:me)?|muestra(?:me)?|lista(?:me)?|ver)?\s*"
    r"(?:mis\s+|los\s+)?limites(?:\s+de\s+gasto)?$"
)
_BUDGET_TERMS = re.compile(
    r"\b(?:estado|consum(?:o|ido)|disponible|porcentaje|como\s+vengo|"
    r"cuanto\s+(?:me\s+)?(?:queda|falta)|alcanzar|pase|excedi)\b"
)
_LIMIT_TERMS = re.compile(r"\b(?:limite|limites|presupuesto|presupuestos|tope|topes)\b")
_REFERENTIAL_CHANGE = re.compile(
    r"^(?:(?:mejor\s+)?que\s+sea|en\s+vez\s+de)\b"
)
_EXPLICIT_CHANGE = re.compile(r"\b(?:cambia|cambialo|modifica|modificalo)\b")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9%]+", " ", without_accents).strip()


def references_recent_limit(text: str) -> bool:
    normalized = _normalize(text)
    return (
        _REFERENTIAL_CHANGE.search(normalized) is not None
        or (
            _EXPLICIT_CHANGE.search(normalized) is not None
            and re.search(r"\b(?:limite|tope|mes)\b", normalized) is not None
        )
    )


def _apply_relative_period(data: dict, normalized_text: str, today: date) -> None:
    if re.search(r"\b(?:mes\s+actual|este\s+mes)\b", normalized_text):
        data["limit_month"] = today.month
        data["limit_year"] = today.year
        return

    if re.search(r"\b(?:mes\s+proximo|proximo\s+mes)\b", normalized_text):
        if today.month == 12:
            data["limit_month"] = 1
            data["limit_year"] = today.year + 1
        else:
            data["limit_month"] = today.month + 1
            data["limit_year"] = today.year
        return

    if data.get("limit_month") is None:
        for month_name, month in _MONTHS.items():
            if re.search(rf"\b{month_name}\b", normalized_text):
                data["limit_month"] = month
                break


def normalize_limit_intent(
    text: str,
    extracted_data: dict,
    *,
    last_limit: LastCreatedLimit | None = None,
    today: date | None = None,
) -> dict:
    """Return a corrected copy for unambiguous limit-related phrases."""
    data = dict(extracted_data)
    normalized = _normalize(text)
    reference_date = today or date.today()  # noqa: DTZ011

    if _LIST_LIMITS.fullmatch(normalized):
        data["intent"] = "list_limits"
        return data

    if _LIMIT_TERMS.search(normalized) and _BUDGET_TERMS.search(normalized):
        data["intent"] = "budget_query"
        return data

    if last_limit is not None and references_recent_limit(text):
        data["intent"] = "change_limit"
        _apply_relative_period(data, normalized, reference_date)

    return data


_QUERY_MOVEMENTS = re.compile(
    r"^(?:mostra(?:me)?|muestra(?:me)?|ver|lista(?:me)?|consultar|resumen\s+(?:de\s+)?)"
    r"?\s*(?:mis\s+|los\s+)?(?:ultimos\s+)?(?:\d+\s+)?(?:movimientos|gastos|ingresos|transacciones)"
    r"(?:\s+(?:de|del|en)\s+.*)?$"
)


def normalize_movement_query_intent(
    text: str,
    extracted_data: dict,
) -> dict:
    """Return a corrected copy for unambiguous movement query phrases."""
    data = dict(extracted_data)
    normalized = _normalize(text)

    # Don't override if already determined by LLM as expense with amount
    if data.get("intent") == "expense" and data.get("amount") is not None:
        return data

    if _QUERY_MOVEMENTS.fullmatch(normalized):
        data["intent"] = "query_movements"
        if re.search(r"\bgasto(?:s)?\b", normalized):
            data["movement_type"] = "egreso"
        elif re.search(r"\bingreso(?:s)?\b", normalized):
            data["movement_type"] = "ingreso"

        m_count = re.search(r"\b(\d+)\b", normalized)
        if m_count:
            try:
                data["limit"] = min(int(m_count.group(1)), 5)
            except ValueError:
                pass
        return data

    return data
