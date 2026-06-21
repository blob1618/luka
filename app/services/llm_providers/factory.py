import os

from .base import LLMProvider
from .gemini import GeminiProvider
from .mistral import MistralProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "gemini": GeminiProvider,
    "mistral": MistralProvider, # para agregar proveedores
}


def create_provider(provider_name: str | None = None) -> LLMProvider:
    """
    Factory function. Crea e instancia el provider LLM solicitado.

    Args:
        provider_name: Nombre del provider (case-insensitive).
                       Si es None, lee de la env var LLM_PROVIDER.

    Raises:
        ValueError: Si el provider solicitado no está registrado.
    """
    name = (provider_name or os.getenv("LLM_PROVIDER", "gemini")).lower().strip()
    provider_cls = _PROVIDERS.get(name)

    if provider_cls is None:
        available = ", ".join(_PROVIDERS.keys())
        raise ValueError(
            f"LLM provider '{name}' no soportado. "
            f"Disponibles: {available}. "
            f"Verificá la env var LLM_PROVIDER."
        )

    return provider_cls()
