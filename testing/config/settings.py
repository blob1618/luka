"""Configuration for the Streamlit testing environment."""

import os
import re
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class ChatSession:
    """An independent simulated user session: own phone and own chat."""
    id: str
    label: str
    phone: str
    user_name: str
    user_registered: bool
    messages: list[dict] = field(default_factory=list)


@dataclass
class TestingConfig:
    """Holds all sidebar configuration state."""
    provider: str = "gemini"                  # LLM provider name
    prompt_path: str = "prompts/core_prompt.md"  # path to prompt file
    model: str = ""                           # modelo seleccionado ("" = primer item de la lista)
    debug_json: bool = True                   # show raw LLM JSON
    debug_latency: bool = True                # show latency metrics
    debug_redis: bool = True                  # show Redis state
    debug_logs: bool = True                   # show dispatcher logs
    sessions: list[ChatSession] = field(default_factory=list)
    active_session_id: str = ""

    def active_session(self) -> ChatSession | None:
        for session in self.sessions:
            if session.id == self.active_session_id:
                return session
        return None


def next_session_label(sessions: list[ChatSession]) -> str:
    """Label por defecto para la próxima sesión."""
    numbers = []
    for session in sessions:
        match = re.fullmatch(r"Sesión (\d+)", session.label)
        if match:
            numbers.append(int(match.group(1)))
    return f"Sesión {max(numbers, default=0) + 1}"


def phone_in_use(sessions: list[ChatSession], phone: str) -> bool:
    """True si alguna sesión ya usa ese teléfono."""
    return any(session.phone == phone for session in sessions)


def new_session(label: str, phone: str, user_name: str, user_registered: bool) -> ChatSession:
    """Crea una ChatSession con id único."""
    return ChatSession(
        id=uuid4().hex,
        label=label,
        phone=phone,
        user_name=user_name,
        user_registered=user_registered,
    )


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
