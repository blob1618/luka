import os
from datetime import date
from pathlib import Path
from typing import Any, Dict

from app.services.llm_contract import (
    RETRY_FORMAT_INSTRUCTION,
    normalize_llm_response,
    resolve_relative_date,
)
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
    def set_prompt_path(cls, path: str) -> None:
        """Sobreescribe la ruta a prompt.md (útil para tests)."""
        cls._prompt_path = path
        cls._system_prompt = None  # force reload

    @classmethod
    def _load_system_prompt(cls) -> str:
        """
        Carga el system prompt desde prompt.md.
        Se cachea en memoria tras la primera carga.
        """
        if cls._system_prompt is not None:
            return cls._system_prompt

        path = cls._prompt_path or os.getenv(
            "SYSTEM_PROMPT_PATH",
            str(Path(__file__).resolve().parent.parent.parent / "prompt.md"),
        )

        try:
            with open(path, "r", encoding="utf-8") as f:
                cls._system_prompt = f.read()
        except FileNotFoundError:
            print(f"[LLMService] prompt.md not found at {path}, using fallback prompt.")
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
            if movement_type in {"ingreso", "egreso"}:
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
    async def process_message(cls, text: str, *, context: str | None = None) -> Dict[str, Any]:
        """
        Procesa un mensaje de usuario usando el system prompt de prompt.md.
        El LLM debe devolver un JSON estructurado con intent y datos asociados.

        Args:
            text: El mensaje de texto del usuario.
            context: Texto adicional concatenado al system prompt (p.ej. fecha actual).

        Returns:
            Dict con los campos del JSON parseado (intent, amount, etc.)
        """
        system_prompt = cls._load_system_prompt()
        if isinstance(context, str) and context:
            system_prompt = system_prompt + "\n\n" + context

        try:
            provider = cls._get_provider()
            parsed = await provider.generate_json(
                system_prompt=system_prompt,
                user_message=text,
                temperature=0.1,
            )
            try:
                parsed = normalize_llm_response(parsed)
            except ValueError:
                parsed = await provider.generate_json(
                    system_prompt=system_prompt + RETRY_FORMAT_INSTRUCTION,
                    user_message=text,
                    temperature=0.1,
                )
                parsed = normalize_llm_response(parsed)

            # Normalizar y validar la respuesta
            intent = str(parsed.get("intent", "out_of_scope")).strip().lower()
            allowed_intents = {
                "expense", "budget_query", "reminder",
                "expense_summary", "query_movements", "greeting", "out_of_scope",
                "create_reminder",
                "list_reminders", "update_reminder",
                "pause_reminder", "activate_reminder",
                "delete_reminder",
                "confirm_category", "reject_category",
                "delete_category", "list_categories",
                "change_category",
                "create_limit", "change_limit", "list_limits",
                "delete_limit", "confirm_limit", "reject_limit",
            }
            if intent not in allowed_intents:
                intent = "out_of_scope"

            amount = cls._normalize_amount(parsed.get("amount"))

            movement_type = cls._normalize_movement_type(parsed, intent)

            # Normalizar campos de query_movements (STK-149)
            date_from = parsed.get("date_from")
            if date_from is not None:
                date_from = str(date_from).strip() or None

            date_to = parsed.get("date_to")
            if date_to is not None:
                date_to = str(date_to).strip() or None

            query_limit = parsed.get("limit")
            try:
                query_limit = int(query_limit) if query_limit is not None else None
            except (TypeError, ValueError):
                query_limit = None

            # Normalizar campos de create_reminder
            reminder_day = parsed.get("reminder_day")
            try:
                reminder_day = int(reminder_day) if reminder_day is not None else None
            except (TypeError, ValueError):
                reminder_day = None
            if reminder_day is not None and not (1 <= reminder_day <= 31):
                reminder_day = None

            reminder_amount = parsed.get("reminder_amount")
            try:
                reminder_amount = float(reminder_amount) if reminder_amount is not None else None
            except (TypeError, ValueError):
                reminder_amount = None

            reminder_concept = parsed.get("reminder_concept")
            if reminder_concept is not None:
                reminder_concept = str(reminder_concept).strip() or None

            reminder_currency = parsed.get("reminder_currency")
            if reminder_currency is not None:
                reminder_currency = str(reminder_currency).strip().upper() or None
                
            reminder_id = parsed.get("reminder_id")
            if reminder_id is not None:
                reminder_id = str(reminder_id).strip() or None

            limit_category = parsed.get("limit_category")
            if limit_category is not None:
                limit_category = str(limit_category).strip() or None

            limit_amount = parsed.get("limit_amount")
            try:
                limit_amount = float(limit_amount) if limit_amount is not None else None
            except (TypeError, ValueError):
                limit_amount = None

            limit_month = parsed.get("limit_month")
            try:
                limit_month = int(limit_month) if limit_month is not None else None
            except (TypeError, ValueError):
                limit_month = None
            if limit_month is not None and not (1 <= limit_month <= 12):
                limit_month = None

            limit_year = parsed.get("limit_year")
            try:
                limit_year = int(limit_year) if limit_year is not None else None
            except (TypeError, ValueError):
                limit_year = None

            limit_currency = parsed.get("limit_currency")
            if limit_currency is not None:
                limit_currency = str(limit_currency).strip().upper() or None
                if limit_currency is not None and (
                    len(limit_currency) != 3 or not limit_currency.isalpha()
                ):
                    limit_currency = None

            raw_movements = parsed.get("movements")
            if isinstance(raw_movements, list) and raw_movements:
                movements = [cls._normalize_single(m, base=parsed) for m in raw_movements]
            elif intent == "expense":
                single = {k: v for k, v in parsed.items() if k != "movements"}
                movements = [cls._normalize_single(single, base=parsed)]
            else:
                movements = []

            return {
                "intent": intent,
                "expense": parsed.get("expense"),
                "amount": amount,
                "currency": str(parsed.get("currency", "ARS")).upper() if parsed.get("currency") else "ARS",
                "movement_type": movement_type,
                "category": parsed.get("category"),
                "description": parsed.get("description"),
                "date_from": date_from,
                "date_to": date_to,
                "limit": query_limit,
                "reminder_title": parsed.get("reminder_title"),
                "reminder_date": parsed.get("reminder_date"),
                "reminder_concept": reminder_concept,
                "reminder_day": reminder_day,
                "reminder_amount": reminder_amount,
                "reminder_currency": reminder_currency,
                "reminder_id": reminder_id,
                "limit_category": limit_category,
                "limit_amount": limit_amount,
                "limit_month": limit_month,
                "limit_year": limit_year,
                "limit_currency": limit_currency,
                "reply_text": str(parsed.get("reply_text") or ""),
                "movements": movements,
            }

        except Exception as exc:
            print(f"[LLMService] process_message failed: {type(exc).__name__}: {exc}")
            return {
                "intent": "out_of_scope",
                "expense": None,
                "amount": None,
                "currency": "ARS",
                "movement_type": None,
                "category": None,
                "description": None,
                "date_from": None,
                "date_to": None,
                "limit": None,
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
                "reply_text": (
                    "No he podido analizar tu mensaje en este momento. "
                    "¿Podés reformularlo e intentar de nuevo?"
                ),
                "movements": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

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
