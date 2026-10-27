from datetime import date, timedelta
from typing import Any, Dict

_WEEKDAY_ES = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
    "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6,
}


def resolve_relative_date(raw_fecha: str | None, today: date) -> date | None:
    """Resuelve una fecha relativa o ISO; una fecha inválida no se reemplaza."""
    if raw_fecha is None:
        return today
    if not isinstance(raw_fecha, str) or not raw_fecha.strip():
        return None

    token = raw_fecha.strip().lower()
    if token == "hoy":
        return today
    if token == "ayer":
        return today - timedelta(days=1)
    if token in {"anteayer", "antes de ayer"}:
        return today - timedelta(days=2)
    if token in {"mañana", "manana"}:
        return today + timedelta(days=1)
    if token in {"pasado mañana", "pasado manana"}:
        return today + timedelta(days=2)

    if token in _WEEKDAY_ES:
        delta = (today.weekday() - _WEEKDAY_ES[token]) % 7
        delta = 7 if delta == 0 else delta
        return today - timedelta(days=delta)

    try:
        parsed = date.fromisoformat(token)
    except ValueError:
        return None

    return parsed


RETRY_FORMAT_INSTRUCTION = (
    "\n\nIMPORTANTE: Respondé ÚNICAMENTE con un objeto JSON válido, "
    "no con una lista ni texto adicional."
)


def normalize_llm_response(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(
            f"LLM output non-conformant: expected object, got {type(raw).__name__}"
        )
    return raw
