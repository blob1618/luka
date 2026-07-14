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
    async def process_text_expense(text: str) -> Dict[str, Any]:
        """
        Wrapper legacy. Llama a process_message y extrae los campos de gasto.
        Se mantiene por compatibilidad hacia atrás.
        """
        result = await LLMService.process_message(text)
        return {
            "is_expense": result.get("intent") == "expense" and result.get("amount") is not None,
            "expense": result.get("expense"),
            "amount": result.get("amount"),
            "currency": result.get("currency") or "ARS",
            "reply_text": result.get("reply_text") or "",
        }

    @classmethod
    async def process_message(cls, text: str) -> Dict[str, Any]:
        """
        Procesa un mensaje de usuario usando el system prompt de prompt.md.
        El LLM debe devolver un JSON estructurado con intent y datos asociados.

        Args:
            text: El mensaje de texto del usuario.

        Returns:
            Dict con los campos del JSON parseado (intent, is_expense, amount, etc.)
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
                "expense", "set_budget", "budget_query", "reminder",
                "expense_summary", "greeting", "out_of_scope",
            }
            if intent not in allowed_intents:
                intent = "out_of_scope"

            amount = parsed.get("amount")
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None

            # Normalizar movement_type
            movement_type = str(parsed.get("movement_type", "")).strip().lower() if parsed.get("movement_type") else ""
            if movement_type not in ("ingreso", "egreso"):
                # Compatibilidad hacia atrás: si intent es "expense" y no hay movement_type, asumir "egreso"
                movement_type = "egreso" if intent == "expense" else None

            return {
                "intent": intent,
                "movement_type": movement_type,
                "is_expense": intent == "expense" and amount is not None,
                "expense": parsed.get("expense"),
                "amount": amount,
                "currency": str(parsed.get("currency", "ARS")).upper() if parsed.get("currency") else "ARS",
                "category": parsed.get("category"),
                "description": parsed.get("description"),
                "month": parsed.get("month"),
                "reminder_title": parsed.get("reminder_title"),
                "reminder_date": parsed.get("reminder_date"),
                "reply_text": str(parsed.get("reply_text", "")),
            }

        except Exception as exc:
            print(f"[LLMService] process_message failed: {type(exc).__name__}: {exc}")
            return {
                "intent": "out_of_scope",
                "movement_type": None,
                "is_expense": False,
                "expense": None,
                "amount": None,
                "currency": "ARS",
                "category": None,
                "description": None,
                "month": None,
                "reminder_title": None,
                "reminder_date": None,
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
            "is_expense": False,
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
            "is_expense": False,
            "amount": None,
            "expense": None,
            "reply_text": "Aun no proceso imagenes de comprobantes.",
        }