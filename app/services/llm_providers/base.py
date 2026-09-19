import asyncio
import json
import re
from abc import ABC, abstractmethod
from typing import Any, Dict

import httpx


class LLMProvider(ABC):
    """
    Contrato que todo proveedor LLM debe cumplir.

    La clase base implementa el manejo común de errores: reintentos con
    backoff para statuses transitorios (429/503), fallback entre modelos
    (404 → siguiente candidato, 503 agotado → siguiente candidato) y el
    re-lanzamiento del error real que el proveedor produjo. Los providers
    solo implementan la obtención del modelo (_get_model) y el envío y
    parseo de la request (_post_to_model).
    """

    API_URL: str = ""
    DEFAULT_MODEL: str = ""
    FALLBACK_MODELS: tuple[str, ...] = ()
    LOG_PREFIX: str = "LLM"
    RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 503})
    MAX_RETRIES_PER_MODEL = 2

    # =========================================================================
    # Hooks que cada proveedor implementa
    # =========================================================================

    @abstractmethod
    def _get_model(self) -> str:
        """Devuelve el modelo primario configurado (levanta si no hay API key)."""

    @abstractmethod
    async def _post_to_model(
        self,
        model_name: str,
        system_prompt: str,
        user_message: str,
        temperature: float,
        history: list[dict[str, str]] | None,
    ) -> Dict[str, Any]:
        """Envía la request al modelo indicado y devuelve el dict parseado.

        Debe llamar a response.raise_for_status() y usar _safe_json_loads.
        Errores esperados: httpx.HTTPStatusError, httpx.HTTPError,
        ValueError, json.JSONDecodeError.
        """

    # =========================================================================
    # Driver común: candidatos, reintentos, fallback, errores
    # =========================================================================

    @staticmethod
    def _safe_json_loads(raw_text: str) -> Dict[str, Any]:
        """
        Intenta parsear JSON de raw_text.
        Si falla, busca primer objeto JSON embebido con regex.
        """
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise

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

    async def generate_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        history: list[dict[str, str]] | None = None,
    ) -> Dict[str, Any]:
        """
        Envía el system_prompt, el historial reciente y user_message al proveedor
        en una sola request. Retorna el JSON parseado como dict, con reintentos y
        fallback entre modelos.

        Raises:
            La excepción real del proveedor (httpx.HTTPStatusError, etc.)
            si todos los modelos y reintentos fallan.
        """
        primary_model = self._get_model()
        last_error: Exception | None = None

        for model_name in self._get_model_candidates(primary_model):
            for attempt in range(self.MAX_RETRIES_PER_MODEL):
                try:
                    return await self._post_to_model(
                        model_name,
                        system_prompt,
                        user_message,
                        temperature,
                        history,
                    )
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status_code = exc.response.status_code
                    print(
                        f"[{self.LOG_PREFIX}] model={model_name} "
                        f"status={status_code} body={exc.response.text}"
                    )

                    if status_code == 404:
                        break  # probar siguiente modelo

                    if status_code in self.RETRYABLE_STATUSES and attempt + 1 < self.MAX_RETRIES_PER_MODEL:
                        await asyncio.sleep(self._retry_delay_seconds(exc.response, attempt))
                        continue

                    break

                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    last_error = exc
                    break

            # Un 404 (modelo inexistente) o un 503 (capacidad del modelo, high
            # demand) justifican probar otro candidato. Un 429 agotado aplica
            # al proyecto y recorrer fallbacks multiplica las llamadas. Los
            # errores no HTTP-status (parseo de JSON, respuestas vacías,
            # errores de red) se propagan: no tienen sentido probarlos en
            # otro modelo.
            if isinstance(last_error, httpx.HTTPStatusError):
                if last_error.response.status_code in (404, 503):
                    continue
                break
            break

        if last_error:
            print(
                f"[{self.LOG_PREFIX}] processing failed: "
                f"{type(last_error).__name__}: {last_error}"
            )
            raise last_error

        raise RuntimeError(f"{self.LOG_PREFIX}: todos los modelos fallaron sin error registrado.")
