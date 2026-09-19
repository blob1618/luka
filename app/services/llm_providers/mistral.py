import os
from typing import Any, Dict

import httpx

from .base import LLMProvider


class MistralProvider(LLMProvider):
    """
    Adapter para Mistral AI.
    Usa la API OpenAI-compatible de Mistral con JSON mode nativo.
    Documentación: https://docs.mistral.ai/api/
    El manejo de errores (reintentos, fallback, 503) vive en LLMProvider.
    """

    API_URL = "https://api.mistral.ai/v1/chat/completions"
    DEFAULT_MODEL = "mistral-small-latest"
    FALLBACK_MODELS = ("mistral-small-latest", "ministral-8b-latest")
    LOG_PREFIX = "Mistral"

    def _get_config(self) -> tuple[str, str]:
        api_key = os.getenv("MISTRAL_API_KEY")
        model = os.getenv("MISTRAL_MODEL", self.DEFAULT_MODEL).strip()
        if not api_key:
            print("Falta MISTRAL_API_KEY. No se puede llamar a Mistral.")
            raise RuntimeError("Mistral no configurado: falta MISTRAL_API_KEY.")
        return api_key, model

    def _get_model(self) -> str:
        return self._get_config()[1]

    async def _post_to_model(
        self,
        model_name: str,
        system_prompt: str,
        user_message: str,
        temperature: float,
        history: list[dict[str, str]] | None,
    ) -> Dict[str, Any]:
        api_key, _ = self._get_config()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_message})
        request_body = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.API_URL, headers=headers, json=request_body
            )
            response.raise_for_status()

        payload = response.json()
        choices = payload.get("choices", [])
        if not choices:
            raise ValueError("Mistral returned no choices")

        raw_text = choices[0].get("message", {}).get("content", "")
        return self._safe_json_loads(raw_text)
