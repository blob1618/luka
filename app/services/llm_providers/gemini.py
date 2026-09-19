import os
from typing import Any, Dict

import httpx

from .base import LLMProvider


class GeminiProvider(LLMProvider):
    """
    Adapter para Google Gemini.
    El manejo de errores (reintentos, fallback, 503) vive en LLMProvider.
    """

    API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    DEFAULT_MODEL = "gemini-3.6-flash"
    FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-3-flash-preview")
    LOG_PREFIX = "Gemini"

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        normalized = model_name.strip().strip('"').strip("'")
        if normalized.startswith("models/"):
            normalized = normalized[len("models/"):]
        return normalized

    def _get_config(self) -> tuple[str, str]:
        api_key = os.getenv("GEMINI_API_KEY")
        model = self._normalize_model_name(
            os.getenv("GEMINI_MODEL", self.DEFAULT_MODEL)
        )
        if not api_key:
            print("Falta GEMINI_API_KEY. No se puede llamar a Gemini.")
            raise RuntimeError("Gemini no configurado: falta GEMINI_API_KEY.")
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

        contents = [
            {
                "role": "model" if message["role"] == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            }
            for message in (history or [])
        ]
        contents.append(
            {
                "role": "user",
                "parts": [{"text": user_message}],
            }
        )
        request_body = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.API_URL.format(model=model_name),
                headers={"x-goog-api-key": api_key},
                json=request_body,
            )
            response.raise_for_status()

        payload = response.json()
        candidates = payload.get("candidates", [])
        if not candidates:
            raise ValueError("Gemini returned no candidates")

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise ValueError("Gemini returned an empty content payload")

        raw_text = parts[0].get("text", "")
        return self._safe_json_loads(raw_text)
