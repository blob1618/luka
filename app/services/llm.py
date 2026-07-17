import os
from pathlib import Path
from typing import Any, Dict

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


    @classmethod
    async def process_message(cls, text: str) -> Dict[str, Any]:
        """
        Procesa un mensaje de usuario usando el system prompt de prompt.md.
        El LLM debe devolver un JSON estructurado con intent y datos asociados.

        Args:
            text: El mensaje de texto del usuario.

        Returns:
            Dict con los campos del JSON parseado (intent, amount, etc.)
        """
        system_prompt = cls._load_system_prompt()

        try:
            provider = cls._get_provider()
            parsed = await provider.generate_json(
                system_prompt=system_prompt,
                user_message=text,
                temperature=0.1,
            )

            # Normalizar y validar la respuesta
            intent = str(parsed.get("intent", "out_of_scope")).strip().lower()
            allowed_intents = {
                "expense", "budget_query", "reminder",
                "expense_summary", "greeting", "out_of_scope",
                "create_reminder",
                "list_reminders", "update_reminder",
                "pause_reminder", "activate_reminder",
                "delete_reminder",
                "confirm_category", "reject_category",
                "delete_category", "list_categories",
                "change_category",
            }
            if intent not in allowed_intents:
                intent = "out_of_scope"

            amount = parsed.get("amount")
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None

            movement_type = cls._normalize_movement_type(parsed, intent)

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

            return {
                "intent": intent,
                "expense": parsed.get("expense"),
                "amount": amount,
                "currency": str(parsed.get("currency", "ARS")).upper() if parsed.get("currency") else "ARS",
                "movement_type": movement_type,
                "category": parsed.get("category"),
                "description": parsed.get("description"),
                "reminder_title": parsed.get("reminder_title"),
                "reminder_date": parsed.get("reminder_date"),
                "reminder_concept": reminder_concept,
                "reminder_day": reminder_day,
                "reminder_amount": reminder_amount,
                "reminder_currency": reminder_currency,
                "reminder_id": reminder_id,
                "reply_text": str(parsed.get("reply_text", "")),
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
                "reminder_title": None,
                "reminder_date": None,
                "reminder_concept": None,
                "reminder_day": None,
                "reminder_amount": None,
                "reminder_currency": None,
                "reminder_id": None,
                "reply_text": (
                    "No he podido analizar tu mensaje en este momento. "
                    "Si deseas, puedes reenviarlo en unos instantes."
                ),
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
