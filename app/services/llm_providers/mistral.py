import asyncio
import json
import os
from typing import Any, Dict

import httpx

from .base import LLMProvider


class MistralProvider(LLMProvider):
    """
    Adapter para Mistral AI.
    Usa la API OpenAI-compatible de Mistral con JSON mode nativo.
    Documentación: https://docs.mistral.ai/api/
    """

    API_URL = "https://api.mistral.ai/v1/chat/completions"
    DEFAULT_MODEL = "mistral-small-latest"
    FALLBACK_MODELS = ("mistral-small-latest", "ministral-8b-latest")
    MAX_RETRIES = 2

    # =========================================================================
    # Helpers privados
    # =========================================================================

    def _get_config(self) -> tuple[str, str] | tuple[None, None]:
        api_key = os.getenv("MISTRAL_API_KEY")
        model = os.getenv("MISTRAL_MODEL", self.DEFAULT_MODEL).strip()
        if not api_key:
            print("Falta MISTRAL_API_KEY. No se puede llamar a Mistral.")
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
            raise RuntimeError("Mistral no configurado: falta MISTRAL_API_KEY.")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None

        for model_name in self._get_model_candidates(model):
            request_body = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }

            for attempt in range(self.MAX_RETRIES):
                try:
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

                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status_code = exc.response.status_code
                    print(f"[Mistral] model={model_name} status={status_code} body={exc.response.text}")

                    if status_code == 404:
                        break  # probar siguiente modelo

                    if status_code == 429 and attempt + 1 < self.MAX_RETRIES:
                        await asyncio.sleep(self._retry_delay_seconds(exc.response, attempt))
                        continue

                    break

                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    last_error = exc
                    break

            # 404 → siguiente modelo candidato
            if isinstance(last_error, httpx.HTTPStatusError) and last_error.response.status_code == 404:
                continue

        if last_error:
            if isinstance(last_error, httpx.HTTPStatusError):
                print(
                    "[Mistral] processing failed",
                    f"url={last_error.request.url.path}",
                    f"status={last_error.response.status_code}",
                    f"body={last_error.response.text}",
                )
            else:
                print(f"[Mistral] processing failed: {type(last_error).__name__}: {last_error}")
            raise last_error

        raise RuntimeError("Mistral: todos los modelos fallaron sin error registrado.")
