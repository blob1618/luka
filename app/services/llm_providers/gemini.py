import asyncio
import json
import os
from typing import Any, Dict

import httpx

from .base import LLMProvider


class GeminiProvider(LLMProvider):
    """
    Adapter para Google Gemini.
    Soporta fallback automático entre modelos si el primario falla con 404.
    """

    API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    DEFAULT_MODEL = "gemini-flash-latest"
    FALLBACK_MODELS = ("gemini-2.5-flash-lite",)
    MAX_RETRIES_PER_MODEL = 2

    # =========================================================================
    # Helpers privados
    # =========================================================================

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        normalized = model_name.strip().strip('"').strip("'")
        if normalized.startswith("models/"):
            normalized = normalized[len("models/"):]
        return normalized

    def _get_config(self) -> tuple[str, str] | tuple[None, None]:
        api_key = os.getenv("GEMINI_API_KEY")
        model = self._normalize_model_name(
            os.getenv("GEMINI_MODEL", self.DEFAULT_MODEL)
        )
        if not api_key:
            print("Falta GEMINI_API_KEY. No se puede llamar a Gemini.")
            return None, None
        return api_key, model

    def _get_model_candidates(self, primary_model: str) -> list[str]:
        candidates = [primary_model]
        for fallback in self.FALLBACK_MODELS:
            if fallback not in candidates:
                candidates.append(fallback)
        return candidates

    @staticmethod
    def _retry_delay_seconds(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return min(2.0 * (attempt + 1), 6.0)

    # =========================================================================
    # Implementación del contrato
    # =========================================================================

    async def generate_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
    ) -> Dict[str, Any]:
        api_key, model = self._get_config()
        if not api_key or not model:
            raise RuntimeError("Gemini no configurado: falta GEMINI_API_KEY.")

        request_body = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_message}],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }

        params = {"key": api_key}
        last_error: Exception | None = None

        for model_name in self._get_model_candidates(model):
            url = self.API_URL.format(model=model_name)

            for attempt in range(self.MAX_RETRIES_PER_MODEL):
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        response = await client.post(url, params=params, json=request_body)
                        response.raise_for_status()

                    payload = response.json()
                    candidates = payload.get("candidates", [])
                    if not candidates:
                        raise ValueError("Gemini returned no candidates")

                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])
                    if not parts:
                        raise ValueError("Gemini returned an empty content payload")

                    raw_text = parts[0].get("text", "")
                    return self._safe_json_loads(raw_text)

                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status_code = exc.response.status_code
                    print(f"[Gemini] model={model_name} status={status_code} body={exc.response.text}")

                    if status_code == 404:
                        break  # probar siguiente modelo

                    if status_code == 429 and attempt + 1 < self.MAX_RETRIES_PER_MODEL:
                        await asyncio.sleep(self._retry_delay_seconds(exc.response, attempt))
                        continue

                    break

                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    last_error = exc
                    break

            # Solo un modelo inexistente justifica probar otro candidato. Una cuota
            # agotada aplica al proyecto y recorrer fallbacks multiplica las llamadas.
            if isinstance(last_error, httpx.HTTPStatusError):
                if last_error.response.status_code == 404:
                    continue
                break

        # Loguear el error final y propagarlo
        if last_error:
            if isinstance(last_error, httpx.HTTPStatusError):
                print(
                    "[Gemini] processing failed",
                    f"url={last_error.request.url.path}",
                    f"status={last_error.response.status_code}",
                    f"body={last_error.response.text}",
                )
                raise RuntimeError(
                    f"Gemini request failed with status {last_error.response.status_code}"
                ) from None

            print(f"[Gemini] processing failed: {type(last_error).__name__}")
            raise RuntimeError(
                f"Gemini processing failed: {type(last_error).__name__}"
            ) from None

        raise RuntimeError("Gemini: todos los modelos fallaron sin error registrado.")
