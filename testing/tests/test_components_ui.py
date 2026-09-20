"""Tests for UI components using a mocked streamlit module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from testing.config.settings import TestingConfig


@pytest.fixture
def mock_st():
    """Patches the streamlit module with a MagicMock for UI components."""
    class SessionState(dict):
        def __getattr__(self, name):
            return self[name]

        def __setattr__(self, name, value):
            self[name] = value

    with patch("testing.components.sidebar.st") as sidebar_st, \
         patch("testing.components.chat.st") as chat_st, \
         patch("testing.components.debug_panel.st") as debug_st, \
         patch("testing.components.chat.components_html") as components_html:
        sidebar_st.session_state = SessionState()
        chat_st.session_state = SessionState()
        yield {
            "sidebar": sidebar_st,
            "chat": chat_st,
            "debug": debug_st,
            "components_html": components_html,
        }


class TestRenderSidebar:
    def test_render_sidebar_updates_config(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        st.sidebar.__enter__ = MagicMock(return_value=None)
        st.sidebar.__exit__ = MagicMock(return_value=False)

        col = MagicMock()
        col.__enter__ = MagicMock(return_value=None)
        col.__exit__ = MagicMock(return_value=False)
        st.columns.return_value = (col, col)

        st.selectbox.return_value = "gemini"
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True
        st.button.return_value = False

        config = render_sidebar()

        assert config.provider == "gemini"
        assert st.session_state["config"] is config

    def test_render_sidebar_clear_chat_button(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        st.sidebar.__enter__ = MagicMock(return_value=None)
        st.sidebar.__exit__ = MagicMock(return_value=False)

        col = MagicMock()
        col.__enter__ = MagicMock(return_value=None)
        col.__exit__ = MagicMock(return_value=False)
        st.columns.return_value = (col, col)

        st.selectbox.return_value = "gemini"
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True

        # First button (clear chat) returns True, reset returns False
        st.button.side_effect = [True, False]
        st.session_state.messages = [{"role": "user", "content": "hola"}]

        render_sidebar()

        assert st.session_state.messages == []

    def test_render_sidebar_reset_db_button(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        st.sidebar.__enter__ = MagicMock(return_value=None)
        st.sidebar.__exit__ = MagicMock(return_value=False)

        col = MagicMock()
        col.__enter__ = MagicMock(return_value=None)
        col.__exit__ = MagicMock(return_value=False)
        st.columns.return_value = (col, col)

        st.selectbox.return_value = "gemini"
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True

        st.button.side_effect = [False, True]

        render_sidebar()

        assert st.session_state["reset_db_requested"] is True

    def test_render_sidebar_configura_el_modelo(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        st.sidebar.__enter__ = MagicMock(return_value=None)
        st.sidebar.__exit__ = MagicMock(return_value=False)
        col = MagicMock()
        col.__enter__ = MagicMock(return_value=None)
        col.__exit__ = MagicMock(return_value=False)
        st.columns.return_value = (col, col)

        # orden de llamadas: Provider, Prompt, Modelo
        st.selectbox.side_effect = ["gemini", "prompt.md", "gemini-3.5-flash"]
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True
        st.button.return_value = False

        config = render_sidebar()

        assert config.model == "gemini-3.5-flash"

    def test_modelo_fuera_de_lista_resetea_a_primero(self, mock_st):
        from testing.components.sidebar import render_sidebar
        from testing.config.settings import TestingConfig

        st = mock_st["sidebar"]
        st.session_state["config"] = TestingConfig(provider="gemini", model="mistral-small-latest")
        st.sidebar.__enter__ = MagicMock(return_value=None)
        st.sidebar.__exit__ = MagicMock(return_value=False)
        col = MagicMock()
        col.__enter__ = MagicMock(return_value=None)
        col.__exit__ = MagicMock(return_value=False)
        st.columns.return_value = (col, col)

        st.selectbox.return_value = "gemini-3.6-flash"
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True
        st.button.return_value = False

        render_sidebar()

        # el selectbox de modelo debe arrancar en índice 0 (primer item de gemini)
        model_call = st.selectbox.call_args_list[2]
        assert model_call.kwargs["index"] == 0


class TestSinModoDirecto:
    def test_config_no_tiene_campo_modo(self):
        from testing.config.settings import TestingConfig

        assert not hasattr(TestingConfig(), "mode")

    def test_sidebar_no_renderiza_radio_de_modo(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        st.sidebar.__enter__ = MagicMock(return_value=None)
        st.sidebar.__exit__ = MagicMock(return_value=False)
        col = MagicMock()
        col.__enter__ = MagicMock(return_value=None)
        col.__exit__ = MagicMock(return_value=False)
        st.columns.return_value = (col, col)

        st.selectbox.return_value = "gemini"
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True
        st.button.return_value = False

        render_sidebar()

        st.radio.assert_not_called()


class TestRenderDebug:
    def test_no_flags_returns_without_rendering(self, mock_st):
        from testing.components.debug_panel import render_debug

        st = mock_st["debug"]
        render_debug({"latency_ms": 10}, {"json": False, "latency": False, "redis": False, "logs": False})

        st.expander.assert_not_called()

    def test_no_data_returns_without_rendering(self, mock_st):
        from testing.components.debug_panel import render_debug

        st = mock_st["debug"]
        render_debug({}, {"json": True, "latency": False, "redis": False, "logs": False})

        st.expander.assert_not_called()

    def test_renders_json_section(self, mock_st):
        from testing.components.debug_panel import render_debug

        st = mock_st["debug"]
        st.expander.return_value.__enter__ = MagicMock(return_value=None)
        st.expander.return_value.__exit__ = MagicMock(return_value=False)

        render_debug(
            {"raw_json": {"intent": "expense"}, "latency_ms": 42.5, "service_log": "x", "redis_state": {"step": "none"}},
            {"json": True, "latency": True, "redis": True, "logs": True},
        )

        st.json.assert_called()
        st.metric.assert_called_once()
        st.code.assert_called_once()


class TestChatLogic:
    def test_get_prompt_path_default(self):
        from testing.components.chat import _get_prompt_path

        config = TestingConfig(prompt_path="prompt.md")
        assert _get_prompt_path(config) == "prompt.md"

    def test_get_prompt_path_custom(self):
        from testing.components.chat import _get_prompt_path

        config = TestingConfig(prompt_path="prompt_v2.md")
        assert _get_prompt_path(config) == "testing/prompts/prompt_v2.md"

    @pytest.mark.asyncio
    async def test_process_message_webhook_mode(self, mock_st):
        from testing.components.chat import _process_message

        config = TestingConfig()

        with (
            patch(
                "testing.components.chat.WebhookModeService.send_message",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_send.return_value = MagicMock(
                reply_text="✅ registrado",
                raw_llm_response={"intent": "expense"},
                service_invoked="finance",
                intent="expense",
                latency_ms=5.0,
                provider="gemini",
                prompt_path="prompt.md",
                redis_state={"step": "none"},
            )
            reply, debug = await _process_message("Gasté 5000", config)

        assert reply == "✅ registrado"
        assert debug["service_log"] == "finance"
        assert debug["redis_state"]["step"] == "none"

    @pytest.mark.asyncio
    async def test_process_message_webhook_unknown_service(self, mock_st):
        from testing.components.chat import _process_message

        config = TestingConfig()

        with (
            patch(
                "testing.components.chat.WebhookModeService.send_message",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_send.return_value = MagicMock(
                reply_text="ok",
                raw_llm_response=None,
                service_invoked=None,
                intent=None,
                latency_ms=1.0,
                provider="gemini",
                prompt_path="prompt.md",
                redis_state=None,
            )
            reply, debug = await _process_message("test", config)

        assert debug["service_log"] == "unknown"

    def test_bot_avatar_returns_bytes(self):
        from testing.components.chat import bot_avatar

        avatar = bot_avatar()
        assert isinstance(avatar, bytes)
        assert len(avatar) > 1000

    def test_bot_avatar_missing_file_returns_none(self):
        from testing.components.chat import bot_avatar

        with patch(
            "testing.components.chat._public_asset",
            return_value=MagicMock(exists=MagicMock(return_value=False)),
        ):
            assert bot_avatar() is None

    def test_render_chat_existing_assistant_message_with_debug(self, mock_st):
        """Existing assistant messages render with debug expander."""
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        chat_st.session_state.messages = [
            {"role": "user", "content": "hola", "debug": {}},
            {
                "role": "assistant",
                "content": "respuesta",
                "debug": {"raw_json": {"intent": "greeting"}, "latency_ms": 5.0},
            },
        ]
        chat_st.chat_input.return_value = None

        chat_msg = MagicMock()
        chat_msg.__enter__ = MagicMock(return_value=None)
        chat_msg.__exit__ = MagicMock(return_value=False)
        chat_st.chat_message.return_value = chat_msg

        render_chat(TestingConfig(debug_json=True))

        assert chat_st.chat_message.call_count == 2


class TestRenderChat:
    def test_render_chat_displays_and_handles_input(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        chat_st.session_state.messages = []
        chat_st.chat_input.return_value = None

        render_chat(TestingConfig())

        chat_st.chat_message.assert_not_called()

    def test_render_chat_with_prompt(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        chat_st.session_state.messages = []
        chat_st.chat_input.return_value = "hola"
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.spinner.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.spinner.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.sidebar.__enter__ = MagicMock(return_value=None)
        chat_st.sidebar.__exit__ = MagicMock(return_value=False)
        chat_st.download_button.return_value = None

        # Real async processing would hit the LLM; patch it
        with (
            patch(
                "testing.components.chat._process_message",
                new_callable=AsyncMock,
            ) as mock_process,
        ):
            mock_process.return_value = ("respuesta", {"latency_ms": 1.0})

            render_chat(TestingConfig())

        assert len(chat_st.session_state["messages"]) == 2
        assert chat_st.session_state["messages"][0]["role"] == "user"
        assert chat_st.session_state["messages"][1]["content"] == "respuesta"

    def test_render_chat_empty_response(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        chat_st.session_state.messages = []
        chat_st.chat_input.return_value = "hola"
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.spinner.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.spinner.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.sidebar.__enter__ = MagicMock(return_value=None)
        chat_st.sidebar.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "testing.components.chat._process_message",
                new_callable=AsyncMock,
                return_value=("", {"latency_ms": 1.0}),
            ),
        ):
            render_chat(TestingConfig())

        assert chat_st.session_state["messages"][1]["content"] == "Sin respuesta"

    def test_muestra_error_del_llm_con_st_error(self, mock_st):
        from testing.components.chat import render_chat

        st = mock_st["chat"]
        st.chat_input.return_value = "Gasté 5000 en supermercado"
        st.session_state.messages = []
        st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        st.spinner.return_value.__enter__ = MagicMock(return_value=None)
        st.spinner.return_value.__exit__ = MagicMock(return_value=False)
        st.sidebar.__enter__ = MagicMock(return_value=None)
        st.sidebar.__exit__ = MagicMock(return_value=False)
        st.download_button.return_value = None

        with (
            patch(
                "testing.components.chat._process_message",
                new_callable=AsyncMock,
                return_value=(
                    "No he podido analizar tu mensaje en este momento.",
                    {"raw_json": {"intent": "out_of_scope", "error": "RuntimeError: API timeout"}},
                ),
            ),
        ):
            render_chat(TestingConfig())

        st.error.assert_called_once()

    def test_render_chat_assistant_history_uses_markdown_renderer(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        chat_st.session_state.messages = [
            {"role": "assistant", "content": "l1\nl2", "debug": {}},
        ]
        chat_st.chat_input.return_value = None

        chat_msg = MagicMock()
        chat_msg.__enter__ = MagicMock(return_value=None)
        chat_msg.__exit__ = MagicMock(return_value=False)
        chat_st.chat_message.return_value = chat_msg

        render_chat(TestingConfig())

        chat_st.markdown.assert_called_once_with("l1  \nl2")
        chat_st.write.assert_not_called()

    def test_render_chat_new_assistant_reply_uses_markdown_renderer(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        chat_st.session_state.messages = []
        chat_st.chat_input.return_value = "hola"
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.spinner.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.spinner.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.sidebar.__enter__ = MagicMock(return_value=None)
        chat_st.sidebar.__exit__ = MagicMock(return_value=False)
        chat_st.download_button.return_value = None

        with (
            patch(
                "testing.components.chat._process_message",
                new_callable=AsyncMock,
                return_value=("l1\nl2", {"latency_ms": 1.0}),
            ),
        ):
            render_chat(TestingConfig())

        chat_st.markdown.assert_called_once_with("l1  \nl2")

    def test_render_chat_user_message_still_uses_write(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        chat_st.session_state.messages = [
            {"role": "user", "content": "hola", "debug": {}},
        ]
        chat_st.chat_input.return_value = None

        chat_msg = MagicMock()
        chat_msg.__enter__ = MagicMock(return_value=None)
        chat_msg.__exit__ = MagicMock(return_value=False)
        chat_st.chat_message.return_value = chat_msg

        render_chat(TestingConfig())

        chat_st.write.assert_called_once_with("hola")
        chat_st.markdown.assert_not_called()


class TestRenderChatCopyButtons:
    def _render_with_messages(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        chat_st.session_state.messages = [
            {"role": "user", "content": "hola", "debug": {}},
            {
                "role": "assistant",
                "content": "ok",
                "debug": {"latency_ms": 42.0, "raw_json": {"intent": "greeting"}},
            },
        ]
        chat_st.chat_input.return_value = None
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.sidebar.__enter__ = MagicMock(return_value=None)
        chat_st.sidebar.__exit__ = MagicMock(return_value=False)

        render_chat(TestingConfig())

    def test_renders_two_copy_buttons_with_and_without_debug(self, mock_st):
        self._render_with_messages(mock_st)

        calls = mock_st["components_html"].call_args_list
        assert len(calls) == 2
        plain_markup = calls[0].args[0]
        debug_markup = calls[1].args[0]
        assert "📋 Copiar conversación" in plain_markup
        assert "latency_ms" not in plain_markup
        assert "🐞 Copiar con debug" in debug_markup
        assert "latency_ms" in debug_markup

    def test_skips_copy_buttons_without_messages(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        chat_st.session_state.messages = []
        chat_st.chat_input.return_value = None

        render_chat(TestingConfig())

        mock_st["components_html"].assert_not_called()
