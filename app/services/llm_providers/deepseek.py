import os
from typing import Any, Dict

import httpx

from .base import LLMProvider


class DeepSeekProvider(LLMProvider):
    """DeepSeek chat completions in JSON mode for Luka's structured replies."""

    API_URL = "https://api.deepseek.com/chat/completions"
    DEFAULT_MODEL = "deepseek-flash"
    LOG_PREFIX = "DeepSeek"

    def _get_config(self) -> tuple[str, str]:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        model = os.getenv("DEEPSEEK_MODEL", self.DEFAULT_MODEL).strip()
        if not api_key:
            raise RuntimeError("DeepSeek no configurado: falta DEEPSEEK_API_KEY.")
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
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_message})

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model_name,
                    "messages": messages,
                    "temperature": temperature,
                    "thinking": {"type": "disabled"},
                    "response_format": {"type": "json_object"},
                    "max_tokens": 4096,
                },
            )
            response.raise_for_status()

        choices = response.json().get("choices", [])
        if not choices:
            raise ValueError("DeepSeek returned no choices")
        raw_text = choices[0].get("message", {}).get("content")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError("DeepSeek returned empty content")
        return self._safe_json_loads(raw_text)
