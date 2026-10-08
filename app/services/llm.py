import os
from datetime import date
from pathlib import Path
from typing import Any, Dict

from app.services.llm_contract import (
    RETRY_FORMAT_INSTRUCTION,
    normalize_llm_response,
    resolve_relative_date,
)
from app.services.financial_education import FinancialEducationService
from app.services.llm_providers import LLMProvider, create_provider


class LLMService:
    """
    Fachada pública para procesamiento LLM.

    Carga el system prompt desde prompt.md y delega la comunicación
    con el proveedor a un LLMProvider concreto seleccionado por la
    env var LLM_PROVIDER (default: 'gemini').
    """

    _provider: LLMProvider | None = None
    _system_prompt: str | None = None
    _prompt_path: str | None = None

    @classmethod
    def set_prompt_path(cls, path: str | None) -> None:
        """Sobreescribe la ruta al prompt (útil para tests)."""
        cls._prompt_path = path
        cls._system_prompt = None  # force reload

    @classmethod
    def _load_system_prompt(cls) -> str:
        """
        Carga el system prompt.
        Si se definió una ruta explícita (set_prompt_path o SYSTEM_PROMPT_PATH),
        se intenta cargar desde allí. En la ruta predeterminada, busca primero
        prompts/core_prompt.md y luego prompt.md. Si ninguna existe, usa
        un prompt de fallback interno. Se cachea en memoria tras la primera carga.
        """
        if cls._system_prompt is not None:
            return cls._system_prompt

        explicit_path = cls._prompt_path or os.getenv("SYSTEM_PROMPT_PATH")
        if explicit_path:
            candidate_paths = [Path(explicit_path)]
        else:
            root = Path(__file__).resolve().parent.parent.parent
            candidate_paths = [
                root / "prompts" / "core_prompt.md",
                root / "prompt.md",
            ]

        for path in candidate_paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cls._system_prompt = f.read()
                    return cls._system_prompt
            except FileNotFoundError:
                continue

        print(
            f"[LLMService] No prompt file found at {[str(p) for p in candidate_paths]}, "
            "using fallback prompt."
        )
        cls._system_prompt = (
            "Eres LUKA, un asistente financiero personal que opera por WhatsApp. "
            "Ayudas a los usuarios a registrar y gestionar sus gastos personales. "
            "Responde siempre en español, de forma amable y concisa. "
            "No des consejos financieros profesionales ni temas no relacionados."
        )

        return cls._system_prompt

    @classmethod
    def _get_provider(cls) -> LLMProvider:
        """Singleton: instancia el provider la primera vez que se necesita."""
        if cls._provider is None:
            cls._provider = create_provider()
        return cls._provider

    @classmethod
    def reset_provider(cls) -> None:
        """
        Singleton: Re-creación del provider en el próximo uso.
        Para usar distintos providers sin estado compartido (tests).
        """
        cls._provider = None

    @staticmethod
    def _normalize_movement_type(parsed: Dict[str, Any], intent: str) -> str | None:
        explicit_type = (
            "movement_type" in parsed
            or "transaction_type" in parsed
        )

        for field_name in ("movement_type", "transaction_type"):
            raw_value = parsed.get(field_name)
            if raw_value is None:
                continue

            movement_type = str(raw_value).strip().lower()
            if movement_type in {"ingreso", "egreso"} or (intent == "movement_chart" and movement_type == "both"):
                return movement_type

        if explicit_type:
            return None

        if intent == "expense":
            return "egreso"

        return None

    @staticmethod
    def _normalize_amount(raw: Any) -> float | None:
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _normalize_single(cls, m: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
        intent = str(m.get("intent") or base.get("intent") or "expense").strip().lower()
        return {
            "intent": intent,
            "movement_type": cls._normalize_movement_type(m, intent),
            "amount": cls._normalize_amount(m.get("amount")),
            "currency": str(m.get("currency") or "ARS").upper(),
            "category": m.get("category"),
            "description": m.get("description") or m.get("expense"),
            "reply_text": str(m.get("reply_text") or ""),
            "fecha": resolve_relative_date(m.get("fecha"), date.today()).isoformat(),  # noqa: DTZ011
        }


    @classmethod
    async def process_message(
        cls,
        text: str,
        *,
        context: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> Dict[str, Any]:
        """
        Procesa un mensaje de usuario usando el system prompt de prompt.md.
        El LLM debe devolver un JSON estructurado con intent y datos asociados.

        Args:
            text: El mensaje de texto del usuario.
            context: Texto adicional concatenado al system prompt (p.ej. fecha actual).
            history: Hasta cuatro turnos previos (8 mensajes) con roles user/assistant.

        Returns:
            Dict con los campos del JSON parseado (intent, amount, etc.)
        """
        return await _process_message(cls, text, context, history)

    @staticmethod
    async def process_audio_expense(audio_bytes: bytes) -> Dict[str, Any]:
        """
        Placeholder para el procesamiento de notas de voz.
        """
        return {
            "amount": None,
            "expense": None,
            "reply_text": "Aun no proceso notas de voz.",
        }

    @staticmethod
    async def process_image_receipt(image_bytes: bytes) -> Dict[str, Any]:
        """
        Placeholder para el procesamiento de imágenes de comprobantes.
        """
        return {
            "amount": None,
            "expense": None,
            "reply_text": "Aun no proceso imagenes de comprobantes.",
        }


_ALLOWED_INTENTS = {
    "expense", "dashboard_link", "budget_query", "reminder", "expense_summary",
    "query_movements", "greeting", "out_of_scope", "movement_chart",
    "create_reminder", "list_reminders", "update_reminder", "pause_reminder",
    "activate_reminder", "delete_reminder", "enable_proactive_reminders",
    "disable_proactive_reminders", "confirm_category", "reject_category",
    "delete_category", "list_categories", "update_movement", "delete_movement",
    "create_limit", "change_limit", "list_limits", "delete_limit", "confirm_limit",
    "reject_limit", "compensate_budget", "confirm_compensation", "reject_compensation",
    "reset_context", "financial_education",
}


def _strip_optional(value: Any) -> str | None:
    return str(value).strip() or None if value is not None else None


def _upper_optional(value: Any) -> str | None:
    return str(value).strip().upper() or None if value is not None else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _currency_or_none(value: Any) -> str | None:
    currency = _upper_optional(value)
    if currency is not None and (len(currency) != 3 or not currency.isalpha()):
        return None
    return currency


def _normalize_chart_fields(parsed: Dict[str, Any]) -> dict:
    chart_type = str(parsed.get("chart_type") or "").strip().lower()
    if chart_type not in {"bar", "pie"}:
        chart_type = None
    chart_ranking = str(parsed.get("chart_ranking") or "").strip().lower()
    if chart_ranking not in {"highest", "lowest"}:
        chart_ranking = None
    return {
        "chart_type": chart_type,
        "chart_ranking": chart_ranking,
        "chart_currency": _currency_or_none(parsed.get("chart_currency")),
        **{
            key: parsed.get(key)
            for key in (
                "chart_mode", "chart_categories", "chart_limit", "chart_percentages",
                "chart_start_month", "chart_end_month", "chart_months",
            )
        },
    }


def _normalize_query_fields(parsed: Dict[str, Any]) -> dict:
    query_limit = _int_or_none(parsed.get("limit"))
    return {
        "date_from": _strip_optional(parsed.get("date_from")),
        "date_to": _strip_optional(parsed.get("date_to")),
        "limit": query_limit,
        **_normalize_chart_fields(parsed),
    }


def _normalize_reminder_fields(parsed: Dict[str, Any]) -> dict:
    reminder_day = _int_or_none(parsed.get("reminder_day"))
    if reminder_day is not None and not 1 <= reminder_day <= 31:
        reminder_day = None
    return {
        "reminder_concept": _strip_optional(parsed.get("reminder_concept")),
        "reminder_day": reminder_day,
        "reminder_amount": _float_or_none(parsed.get("reminder_amount")),
        "reminder_currency": _upper_optional(parsed.get("reminder_currency")),
        "reminder_id": _strip_optional(parsed.get("reminder_id")),
    }


def _normalize_limit_fields(parsed: Dict[str, Any]) -> dict:
    limit_month = _int_or_none(parsed.get("limit_month"))
    if limit_month is not None and not 1 <= limit_month <= 12:
        limit_month = None
    return {
        "limit_category": _strip_optional(parsed.get("limit_category")),
        "limit_amount": _float_or_none(parsed.get("limit_amount")),
        "limit_month": limit_month,
        "limit_year": _int_or_none(parsed.get("limit_year")),
        "limit_currency": _currency_or_none(parsed.get("limit_currency")),
    }


def _normalize_compensation_fields(parsed: Dict[str, Any]) -> dict:
    return {
        "compensation_target": _strip_optional(parsed.get("compensation_target")),
        "compensation_source": _strip_optional(parsed.get("compensation_source")),
        "compensation_amount": _float_or_none(parsed.get("compensation_amount")),
    }


def _normalize_intent(parsed: Dict[str, Any]) -> str:
    intent = str(parsed.get("intent", "out_of_scope")).strip().lower()
    return intent if intent in _ALLOWED_INTENTS else "out_of_scope"


def _normalize_movements(cls: type, parsed: Dict[str, Any], intent: str) -> list[dict]:
    raw_movements = parsed.get("movements")
    if isinstance(raw_movements, list) and raw_movements:
        return [cls._normalize_single(item, base=parsed) for item in raw_movements]
    if intent == "expense":
        single = {key: value for key, value in parsed.items() if key != "movements"}
        return [cls._normalize_single(single, base=parsed)]
    return []


def _normalize_message_response(cls: type, parsed: Dict[str, Any]) -> Dict[str, Any]:
    intent = _normalize_intent(parsed)
    return {
        "intent": intent,
        "reference": parsed.get("reference"),
        "selection": parsed.get("selection"),
        "changes": parsed.get("changes") if isinstance(parsed.get("changes"), dict) else {},
        "expense": parsed.get("expense"),
        "amount": cls._normalize_amount(parsed.get("amount")),
        "currency": str(parsed.get("currency", "ARS")).upper() if parsed.get("currency") else "ARS",
        "movement_type": cls._normalize_movement_type(parsed, intent),
        "category": parsed.get("category"),
        "description": parsed.get("description"),
        **_normalize_query_fields(parsed),
        "reminder_title": parsed.get("reminder_title"),
        "reminder_date": parsed.get("reminder_date"),
        **_normalize_reminder_fields(parsed),
        **_normalize_limit_fields(parsed),
        **_normalize_compensation_fields(parsed),
        "education_term": _strip_optional(parsed.get("education_term")),
        "reply_text": str(parsed.get("reply_text") or ""),
        "movements": _normalize_movements(cls, parsed, intent),
    }


def _fallback_response(exc: Exception) -> Dict[str, Any]:
    return {
        "intent": "out_of_scope",
        "reference": None,
        "selection": None,
        "changes": {},
        "expense": None,
        "amount": None,
        "currency": "ARS",
        "movement_type": None,
        "category": None,
        "description": None,
        "date_from": None,
        "date_to": None,
        "limit": None,
        "chart_type": None,
        "chart_ranking": None,
        "chart_currency": None,
        "reminder_title": None,
        "reminder_date": None,
        "reminder_concept": None,
        "reminder_day": None,
        "reminder_amount": None,
        "reminder_currency": None,
        "reminder_id": None,
        "limit_category": None,
        "limit_amount": None,
        "limit_month": None,
        "limit_year": None,
        "limit_currency": None,
        "compensation_target": None,
        "compensation_source": None,
        "compensation_amount": None,
        "education_term": None,
        "reply_text": (
            "No he podido analizar tu mensaje en este momento. "
            "¿Podés reformularlo e intentar de nuevo?"
        ),
        "movements": [],
        "error": f"{type(exc).__name__}: {exc}",
    }


async def _generate_parsed_response(
    cls: type,
    text: str,
    system_prompt: str,
    history: list[dict[str, str]] | None,
) -> Dict[str, Any]:
    provider = cls._get_provider()
    parsed = await provider.generate_json(
        system_prompt=system_prompt,
        user_message=text,
        temperature=0.1,
        history=history,
    )
    try:
        return normalize_llm_response(parsed)
    except ValueError:
        parsed = await provider.generate_json(
            system_prompt=system_prompt + RETRY_FORMAT_INSTRUCTION,
            user_message=text,
            temperature=0.1,
            history=history,
        )
        return normalize_llm_response(parsed)


async def _process_message(
    cls: type,
    text: str,
    context: str | None,
    history: list[dict[str, str]] | None,
) -> Dict[str, Any]:
    # Static glossary remains separate from user-specific context.
    system_prompt = (
        cls._load_system_prompt()
        + "\n\n"
        + FinancialEducationService.static_prompt_context()
    )
    if isinstance(context, str) and context:
        system_prompt += "\n\n" + context
    try:
        parsed = await _generate_parsed_response(cls, text, system_prompt, history)
        return _normalize_message_response(cls, parsed)
    except Exception as exc:
        print(f"[LLMService] process_message failed: {type(exc).__name__}: {exc}")
        return _fallback_response(exc)
