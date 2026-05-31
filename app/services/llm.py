import json
import os
import re
import asyncio
from typing import Any, Dict

import httpx


class LLMService:
    GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    DEFAULT_MODEL = "gemini-2.0-flash"
    FALLBACK_MODELS = ("gemini-2.0-flash", "gemini-1.5-flash")
    MAX_RETRIES_PER_MODEL = 2

    @staticmethod
    def _get_gemini_config() -> tuple[str, str] | tuple[None, None]:
        api_key = os.getenv("GEMINI_API_KEY")
        model = os.getenv("GEMINI_MODEL", LLMService.DEFAULT_MODEL)

        if not api_key:
            print("Missing GEMINI_API_KEY. Cannot call Gemini.")
            return None, None

        return api_key, model

    @staticmethod
    def _get_model_candidates(primary_model: str) -> list[str]:
        model_candidates = [primary_model]
        for fallback_model in LLMService.FALLBACK_MODELS:
            if fallback_model not in model_candidates:
                model_candidates.append(fallback_model)
        return model_candidates

    @staticmethod
    def _safe_json_loads(raw_text: str) -> Dict[str, Any]:
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise

    @staticmethod
    def _retry_delay_seconds(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass

        return min(2.0 * (attempt + 1), 6.0)

    @staticmethod
    async def process_text_expense(text: str) -> Dict[str, Any]:
        """
        Sends the user message to Gemini and asks for expense extraction only.
        The model must reply in very formal, literary Spanish and return JSON.
        """
        api_key, model = LLMService._get_gemini_config()
        if not api_key or not model:
            return {
                "is_expense": False,
                "amount": None,
                "expense": None,
                "reply_text": "No puedo procesar tu mensaje en este momento porque falta configurar Gemini.",
            }

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

        request_body = {
            "systemInstruction": {
                "parts": [{"text": system_instruction}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": text}],
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }

        params = {"key": api_key}

        last_error: Exception | None = None
        for model_name in LLMService._get_model_candidates(model):
            url = LLMService.GEMINI_API_URL.format(model=model_name)
            for attempt in range(LLMService.MAX_RETRIES_PER_MODEL):
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
                    parsed = LLMService._safe_json_loads(raw_text)

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
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status_code = exc.response.status_code

                    if status_code == 404:
                        break

                    if status_code == 429 and attempt + 1 < LLMService.MAX_RETRIES_PER_MODEL:
                        await asyncio.sleep(LLMService._retry_delay_seconds(exc.response, attempt))
                        continue

                    break
                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    last_error = exc
                    break

            if isinstance(last_error, httpx.HTTPStatusError) and last_error.response.status_code == 404:
                continue

        if last_error:
            if isinstance(last_error, httpx.HTTPStatusError):
                print(f"Gemini processing failed with HTTP {last_error.response.status_code}")
            else:
                print(f"Gemini processing failed: {type(last_error).__name__}")

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
