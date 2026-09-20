"""Sidebar configuration panel for the testing environment."""

from pathlib import Path

import streamlit as st

from app.services.llm_providers.factory import _PROVIDERS
from testing.config.settings import TestingConfig, get_available_models


def _public_asset(filename: str) -> Path:
    """Resolve a file inside the testing/public/ directory."""
    return Path(__file__).resolve().parent.parent / "public" / filename


def get_available_providers() -> list[str]:
    """Return list of registered LLM provider names from the factory."""
    return list(_PROVIDERS.keys())


def get_available_prompts(testing_dir: str = "testing") -> list[str]:
    """
    Detect available prompt files.

    Always includes 'prompt.md' (project default).
    Scans testing/prompts/ (bare names) and prompts/ (repo-relative paths).
    """
    prompts = ["prompt.md"]
    testing_prompts_dir = Path(testing_dir) / "prompts"
    if testing_prompts_dir.exists():
        for p in sorted(testing_prompts_dir.glob("*.md")):
            if p.name not in prompts:
                prompts.append(p.name)
    repo_prompts_dir = Path(testing_dir).parent / "prompts"
    if repo_prompts_dir.exists():
        for p in sorted(repo_prompts_dir.glob("*.md")):
            rel = f"prompts/{p.name}"
            if rel not in prompts:
                prompts.append(rel)
    return prompts


def render_sidebar() -> TestingConfig:
    """
    Render the sidebar and return the current configuration.

    Reads from st.session_state and Streamlit widgets.
    Returns an updated TestingConfig.
    """
    config = st.session_state.get("config", TestingConfig())

    with st.sidebar:
        logo = _public_asset("logo-luka-texto.png")
        if logo.exists():
            st.image(str(logo), use_container_width=True)
        st.caption("Entorno de testing")

        st.subheader("Modelo LLM")
        providers = get_available_providers()
        provider_index = providers.index(config.provider) if config.provider in providers else 0
        provider = st.selectbox(
            "Provider",
            options=providers,
            index=provider_index,
            key="provider_select",
        )
        config.provider = provider

        st.subheader("Prompt")
        prompts = get_available_prompts()
        prompt_index = prompts.index(config.prompt_path) if config.prompt_path in prompts else 0
        prompt = st.selectbox(
            "Archivo de prompt",
            options=prompts,
            index=prompt_index,
            key="prompt_select",
        )
        config.prompt_path = prompt

        st.subheader("Modelo")
        models = get_available_models(config.provider)
        model_index = models.index(config.model) if config.model in models else 0
        config.model = st.selectbox(
            "Modelo",
            options=models,
            index=model_index,
            key="model_select",
        )

        st.subheader("Usuario simulado")
        config.user_registered = st.checkbox(
            "Registrado",
            value=config.user_registered,
            key="user_registered_check",
        )
        config.phone = st.text_input(
            "Teléfono",
            value=config.phone,
            key="phone_input",
        )
        config.user_name = st.text_input(
            "Nombre",
            value=config.user_name,
            key="name_input",
        )

        st.subheader("Acciones")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑 Limpiar chat", key="clear_chat"):
                st.session_state.messages = []
                st.rerun()
        with col2:
            if st.button("🔄 Reset DB", key="reset_db"):
                st.session_state["reset_db_requested"] = True

        st.subheader("Debug")
        config.debug_json = st.checkbox("JSON crudo LLM", value=config.debug_json, key="debug_json")
        config.debug_latency = st.checkbox("Latencia", value=config.debug_latency, key="debug_latency")
        config.debug_redis = st.checkbox("Estado Redis", value=config.debug_redis, key="debug_redis")
        config.debug_logs = st.checkbox("Logs dispatcher", value=config.debug_logs, key="debug_logs")

    st.session_state.config = config
    return config
