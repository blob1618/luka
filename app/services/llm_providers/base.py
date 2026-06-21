import json
import re
from abc import ABC, abstractmethod
from typing import Any, Dict


class LLMProvider(ABC):
    """
    Contrato que todo proveedor LLM debe cumplir.
    """

    @abstractmethod
    async def generate_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
    ) -> Dict[str, Any]:
        """
        Envía el system_prompt y user_message al LLM y retorna
        el JSON parseado como dict.
        Cada adapter concreto lo traduce al formato nativo del 
        proveedor (HTTP request/response, retry, fallback).

        Raises:
            Exception: Si el proveedor falla después de retries.
        """
        ...

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
