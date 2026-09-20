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
    if session_state:
        fake_st.session_state.update(session_state)
    if config is None:
        config = settings_mod.TestingConfig()

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
    def test_initializes_session_and_simulates_user(self):
        fake_st, config, mock_chat, mock_user_sim, module = _import_app()

        assert fake_st.set_page_config.called
        assert fake_st.markdown.called
        assert fake_st.session_state["messages"] == []
        assert isinstance(fake_st.session_state["config"], settings_mod.TestingConfig)
        assert fake_st.session_state["user_simulator_initialized"] is True
        module.ensure_testing_schema.assert_called_once_with(database_module.engine)
        mock_user_sim.create_test_user.assert_called_once_with(
            config.phone, config.user_name
        )
        mock_chat.assert_called_once_with(config)

    def test_skips_user_simulation_when_already_initialized(self):
        fake_st, config, mock_chat, mock_user_sim, _ = _import_app(
            session_state={"user_simulator_initialized": True}
        )

        assert fake_st.session_state["user_simulator_initialized"] is True
        mock_user_sim.create_test_user.assert_not_called()
        mock_chat.assert_called_once_with(config)

    def test_unregistered_user_skips_simulation(self):
        config = settings_mod.TestingConfig(user_registered=False)
        _, _, mock_chat, mock_user_sim, _ = _import_app(config=config)

        mock_user_sim.create_test_user.assert_not_called()
        mock_chat.assert_called_once_with(config)

    def test_unregistered_user_deletes_simulated_user(self):
        config = settings_mod.TestingConfig(user_registered=False)
        _, _, mock_chat, mock_user_sim, _ = _import_app(config=config)

        mock_user_sim.delete_test_user.assert_called_once_with(config.phone)
        mock_chat.assert_called_once_with(config)

    def test_reset_db_request_clears_flag_and_toasts(self):
        fake_st, config, mock_chat, mock_user_sim, _ = _import_app(
            session_state={"reset_db_requested": True}
        )

        mock_user_sim.reset_user_data.assert_called_once_with(config.phone)
        assert "reset_db_requested" not in fake_st.session_state
        assert fake_st.toast.called
        assert fake_st.session_state["user_simulator_initialized"] is True
        mock_chat.assert_called_once_with(config)

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
