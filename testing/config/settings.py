"""Configuration for the Streamlit testing environment."""

import os
from dataclasses import dataclass, field


@dataclass
class TestingConfig:
    """Holds all sidebar configuration state."""
    provider: str = "gemini"                  # LLM provider name
    prompt_path: str = "prompts/core_prompt.md"  # path to prompt file
    model: str = ""                           # modelo seleccionado ("" = primer item de la lista)
    user_registered: bool = True              # simulate registered user
    phone: str = "5491112345678"              # simulated phone number
    user_name: str = "Test User"              # simulated user name
    debug_json: bool = True                   # show raw LLM JSON
    debug_latency: bool = True                # show latency metrics
    debug_redis: bool = True                  # show Redis state
    debug_logs: bool = True                   # show dispatcher logs


MODEL_ENV_BY_PROVIDER = {
    "gemini": "GEMINI_MODEL",
    "mistral": "MISTRAL_MODEL",
}

# Hardcodeado desde los comentarios de .env.example (solo modelos disponibles;
# flash primero y pro al final). El primer item es el default del selector.
AVAILABLE_MODELS = {
    "gemini": [
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.1-flash-lite-preview",
        "gemini-flash-latest",
        "gemini-3-flash-preview",
        "gemini-3.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemma-4-26b-a4b-it",
        "gemma-4-31b-it",
        "gemini-pro-latest",
        "gemini-3.1-pro-preview",
        "gemini-3.1-pro-preview-customtools",
    ],
    "mistral": [
        "mistral-small-latest",
        "ministral-3b-latest",
        "ministral-8b-latest",
        "ministral-14b-latest",
        "mistral-medium-latest",
        "mistral-large-latest",
        "zai-glm-5-2",
        "voxtral-small-latest",
    ],
}


def get_available_models(provider: str) -> list[str]:
    """Lista hardcodeada de modelos para el proveedor (default: gemini)."""
    return list(AVAILABLE_MODELS.get(provider, AVAILABLE_MODELS["gemini"]))


def set_model_env(provider: str, model: str) -> None:
    """Setea la env var del modelo según el proveedor (solo si hay modelo)."""
    env_name = MODEL_ENV_BY_PROVIDER.get(provider)
    if env_name and model:
        os.environ[env_name] = model


@dataclass
class ChatMessage:
    """A single message in the chat history."""
    role: str                                 # "user" | "assistant"
    content: str                              # visible text
    debug: dict = field(default_factory=dict) # debug metadata (assistant only)
