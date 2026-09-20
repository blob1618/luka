"""Tests del entrypoint de Streamlit (testing/streamlit_app.py).

El módulo ejecuta su lógica al importarse; por eso cada test lo re-importa
con un `streamlit` mockeado y distinto estado de sesión para ejercitar
las ramas de bootstrapping, reset de DB y simulación de usuario.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import app.models.database as database_module
import testing.config.settings as settings_mod


class SessionState(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


def _import_app(session_state=None, config=None):
    """Importa testing/streamlit_app.py con streamlit y dependencias mockeadas."""
    fake_st = MagicMock()
    fake_st.session_state = SessionState()
    if config is None:
        config = settings_mod.TestingConfig()
    fake_st.session_state["config"] = config
    if session_state:
        fake_st.session_state.update(session_state)

    mock_user_sim = MagicMock()

    original_db_url = os.environ.get("DATABASE_URL")
    sys.modules.pop("testing.streamlit_app", None)
    try:
        with (
            patch.dict(sys.modules, {"streamlit": fake_st}),
            patch("testing.components.sidebar.render_sidebar", return_value=config),
            patch("testing.components.chat.render_chat") as mock_chat,
            patch(
                "testing.services.user_simulator.UserSimulator",
                return_value=mock_user_sim,
            ),
            patch("dotenv.load_dotenv"),
            patch("testing.services.schema.ensure_testing_schema"),
        ):
            module = importlib.import_module("testing.streamlit_app")
    finally:
        if original_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_db_url

    return fake_st, config, mock_chat, mock_user_sim, module


class TestStreamlitAppEntrypoint:
    def test_bootstrap_crea_sesion_inicial_y_sincroniza_usuario(self):
        fake_st, config, mock_chat, mock_user_sim, module = _import_app()

        assert fake_st.set_page_config.called
        assert fake_st.markdown.called
        assert len(config.sessions) == 1
        session = config.sessions[0]
        assert session.label == "Sesión 1"
        assert session.phone == "5491112345678"
        assert session.user_name == "Test User"
        assert session.user_registered is True
        assert config.active_session_id == session.id
        module.ensure_testing_schema.assert_called_once_with(database_module.engine)
        mock_user_sim.create_test_user.assert_called_once_with("5491112345678", "Test User")
        mock_chat.assert_called_once_with(config)

    def test_bootstrap_no_usa_estado_global_de_mensajes(self):
        fake_st, _, _, _, _ = _import_app()

        assert "messages" not in fake_st.session_state
        assert "user_simulator_initialized" not in fake_st.session_state

    def test_no_rebootstrap_si_ya_hay_sesiones(self):
        existing = settings_mod.new_session("Sesión 1", "5491112345678", "Test User", True)
        config = settings_mod.TestingConfig(sessions=[existing], active_session_id=existing.id)

        _, _, mock_chat, mock_user_sim, _ = _import_app(config=config)

        assert config.sessions == [existing]
        mock_user_sim.create_test_user.assert_not_called()
        mock_user_sim.delete_test_user.assert_not_called()
        mock_chat.assert_called_once_with(config)

    def test_reset_db_usa_el_telefono_de_la_sesion_activa(self):
        first = settings_mod.new_session("Sesión 1", "5491112345678", "Test User", True)
        second = settings_mod.new_session("Sesión 2", "5491187654321", "María", False)
        config = settings_mod.TestingConfig(sessions=[first, second], active_session_id=second.id)

        fake_st, _, _, mock_user_sim, _ = _import_app(
            session_state={"reset_db_requested": True},
            config=config,
        )

        mock_user_sim.reset_user_data.assert_called_once_with("5491187654321")
        assert "reset_db_requested" not in fake_st.session_state
        assert fake_st.toast.called

    def test_reset_db_sin_sesion_activa_no_resetea(self):
        existing = settings_mod.new_session("Sesión 1", "5491112345678", "Test User", True)
        config = settings_mod.TestingConfig(sessions=[existing], active_session_id="otro")

        fake_st, _, _, mock_user_sim, _ = _import_app(
            session_state={"reset_db_requested": True},
            config=config,
        )

        mock_user_sim.reset_user_data.assert_not_called()
        assert "reset_db_requested" not in fake_st.session_state
        assert fake_st.toast.called

    def test_import_without_database_url_restores_nothing(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        _, _, mock_chat, mock_user_sim, _ = _import_app()

        assert "DATABASE_URL" not in os.environ
        mock_user_sim.create_test_user.assert_called_once()
        mock_chat.assert_called_once()

    def test_import_restores_existing_database_url(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://prod/db")
        _, _, mock_chat, mock_user_sim, _ = _import_app()

        assert os.environ["DATABASE_URL"] == "postgresql://prod/db"
        mock_user_sim.create_test_user.assert_called_once()
