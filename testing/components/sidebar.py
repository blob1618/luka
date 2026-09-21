"""Sidebar configuration panel for the testing environment."""

from pathlib import Path

import streamlit as st

from app.models.database import SessionLocal
from app.services.llm_providers.factory import _PROVIDERS
from testing.config.settings import (
    ChatSession,
    TestingConfig,
    get_available_models,
    new_session,
    next_session_label,
    phone_in_use,
)
from testing.services.user_simulator import sync_test_user


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


def _render_sessions(config: TestingConfig) -> ChatSession | None:
    """Renderiza el manejo de sesiones y devuelve la sesión activa."""
    st.subheader("Sesiones")

    labels = [f"{session.label} — {session.phone}" for session in config.sessions]
    active_index = 0
    for index, session in enumerate(config.sessions):
        if session.id == config.active_session_id:
            active_index = index
            break

    selected_label = st.selectbox(
        "Sesión activa",
        options=labels,
        index=active_index,
        key="active_session_select",
    )
    selected = config.sessions[labels.index(selected_label)]
    if selected.id != config.active_session_id:
        config.active_session_id = selected.id
        st.session_state.pop("session_registered_check", None)

    active = config.active_session()
    registered = st.checkbox(
        "Vinculado",
        value=active.user_registered,
        key="session_registered_check",
    )
    if registered != active.user_registered:
        active.user_registered = registered
        sync_test_user(
            SessionLocal,
            phone=active.phone,
            name=active.user_name,
            registered=registered,
        )

    with st.expander("Nueva sesión"):
        label = st.text_input(
            "Etiqueta",
            value=next_session_label(config.sessions),
            key="new_session_label",
        )
        phone = st.text_input("Teléfono", key="new_session_phone")
        user_name = st.text_input("Nombre", key="new_session_name")
        new_registered = st.checkbox(
            "Ya registrado",
            value=True,
            key="new_session_registered",
        )
        if st.button("➕ Crear sesión", key="create_session"):
            phone = phone.strip()
            if not phone:
                st.error("Ingresá un teléfono")
            elif phone_in_use(config.sessions, phone):
                st.error("Ese teléfono ya está en uso")
            else:
                created = new_session(
                    label.strip() or next_session_label(config.sessions),
                    phone,
                    user_name.strip() or "Test User",
                    new_registered,
                )
                config.sessions.append(created)
                config.active_session_id = created.id
                sync_test_user(
                    SessionLocal,
                    phone=created.phone,
                    name=created.user_name,
                    registered=new_registered,
                )
                for widget_key in (
                    "active_session_select",
                    "session_registered_check",
                    "new_session_label",
                    "new_session_phone",
                    "new_session_name",
                    "new_session_registered",
                ):
                    st.session_state.pop(widget_key, None)
                st.rerun()

    if len(config.sessions) > 1 and st.button("🗑 Eliminar sesión", key="delete_session"):
        config.sessions.remove(active)
        config.active_session_id = config.sessions[0].id
        st.session_state.pop("active_session_select", None)
        st.session_state.pop("session_registered_check", None)
        st.rerun()

    return active


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

        _render_sessions(config)

        st.subheader("Acciones")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑 Limpiar chat", key="clear_chat"):
                active = config.active_session()
                if active is not None:
                    active.messages = []
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
