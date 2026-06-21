from typing import Any, Dict

from app.services.llm_providers import LLMProvider, create_provider


class LLMService:
    """
    Fachada pública para procesamiento LLM.

    Delega toda la comunicación con el proveedor a un LLMProvider concreto
    seleccionado por la env var LLM_PROVIDER (default: 'gemini').
    """

    _provider: LLMProvider | None = None

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
        Sends the user message to the configured LLM provider and asks for 
        expense extraction only.
        The model must reply in very formal, literary Spanish and return JSON.
        """
        system_instruction = (
            "Eres un asistente estrictamente limitado al registro de gastos del usuario. "
            "Tu unica tarea es analizar el mensaje y decidir si contiene un gasto y un monto. "
            "Debes responder en espanol muy formal, con tono literario, y solo devolver JSON valido. "
            "No mantengas conversacion general, no des consejos y no inventes datos. "
            "Si detectas un gasto con su monto, marca is_expense como true y redacta un mensaje de exito breve, "
            "muy formal y, si encaja de forma natural, con emojis discretos. "
            "El mensaje debe repetir el gasto y el monto. "
            "Si no detectas un gasto con monto claro, marca is_expense como false y redacta una respuesta formal "
            "indicando que solo registras gastos. "
            "Devuelve exactamente este esquema JSON: "
            '{"is_expense": true, "expense": "cadena o null", "amount": 0.0, "currency": "ARS", "reply_text": "cadena"}'
        )

        try:
            provider = LLMService._get_provider()
            parsed = await provider.generate_json(
                system_prompt=system_instruction,
                user_message=text,
                temperature=0.1,
            )

            amount = parsed.get("amount")
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None

            return {
                "is_expense": bool(parsed.get("is_expense", False)) and amount is not None,
                "expense": parsed.get("expense"),
                "amount": amount,
                "currency": parsed.get("currency", "ARS"),
                "reply_text": parsed.get("reply_text") or "",
            }

        except Exception as exc:
            print(f"[LLMService] process_text_expense failed: {type(exc).__name__}: {exc}")
            return {
                "is_expense": False,
                "amount": None,
                "expense": None,
            "reply_text": "No he podido analizar tu mensaje en este momento. Si deseas, puedes reenviarlo en unos instantes.",
            }

    @staticmethod
    async def process_audio_expense(audio_bytes: bytes) -> Dict[str, Any]:
        """
        Placeholder for voice note processing.
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
        Placeholder for receipt image processing.
        """
        return {
            "is_expense": False,
            "amount": None,
            "expense": None,
            "reply_text": "Aun no proceso imagenes de comprobantes.",
        }
