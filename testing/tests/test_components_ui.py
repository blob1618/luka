"""Tests for UI components using a mocked streamlit module."""

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from testing.config.settings import TestingConfig, new_session


def make_session(
    label="Sesión 1",
    phone="5491112345678",
    user_name="Test User",
    registered=True,
    messages=None,
):
    session = new_session(label, phone, user_name, registered)
    if messages:
        session.messages.extend(messages)
    return session


def make_config(*sessions, active_index=0, **kwargs):
    config = TestingConfig(**kwargs)
    config.sessions.extend(sessions)
    if sessions:
        config.active_session_id = sessions[active_index].id
    return config


def session_option(session):
    return f"{session.label} — {session.phone}"


@pytest.fixture
def mock_st():
    """Patches the streamlit module with a MagicMock for UI components."""
    class SessionState(dict):
        def __getattr__(self, name):
            return self[name]

        def __setattr__(self, name, value):
            self[name] = value

    with patch("testing.components.sidebar.st") as sidebar_st, \
         patch("testing.components.sidebar.sync_test_user") as sidebar_sync, \
         patch("testing.components.chat.st") as chat_st, \
         patch("testing.components.debug_panel.st") as debug_st, \
         patch("testing.components.chat.components_html") as components_html:
        for st in (sidebar_st, chat_st, debug_st):
            st.session_state = SessionState()
        for container in (sidebar_st.sidebar, chat_st.sidebar):
            container.__enter__ = MagicMock(return_value=None)
            container.__exit__ = MagicMock(return_value=False)
        col = MagicMock()
        col.__enter__ = MagicMock(return_value=None)
        col.__exit__ = MagicMock(return_value=False)
        sidebar_st.columns.return_value = (col, col)
        yield {
            "sidebar": sidebar_st,
            "sync": sidebar_sync,
            "chat": chat_st,
            "debug": debug_st,
            "components_html": components_html,
        }


class TestRenderSidebar:
    def test_render_sidebar_updates_config(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        session = make_session()
        config = make_config(session)
        st.session_state["config"] = config

        st.selectbox.side_effect = [
            "gemini",
            "prompt.md",
            "gemini-3.6-flash",
            session_option(session),
        ]
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True
        st.button.return_value = False

        rendered = render_sidebar()

        assert rendered is config
        assert rendered.provider == "gemini"
        assert st.session_state["config"] is config

    def test_render_sidebar_clear_chat_button(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        active = make_session(messages=[{"role": "user", "content": "hola", "debug": {}}])
        other = make_session(
            "Sesión 2",
            "5491187654321",
            messages=[{"role": "user", "content": "otra", "debug": {}}],
        )
        config = make_config(active, other)
        st.session_state["config"] = config

        st.selectbox.side_effect = [
            "gemini",
            "prompt.md",
            "gemini-3.6-flash",
            session_option(active),
        ]
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True
        st.button.side_effect = [False, False, True, False]

        render_sidebar()

        assert active.messages == []
        assert other.messages == [{"role": "user", "content": "otra", "debug": {}}]

    def test_render_sidebar_reset_db_button(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        session = make_session()
        config = make_config(session)
        st.session_state["config"] = config

        st.selectbox.side_effect = [
            "gemini",
            "prompt.md",
            "gemini-3.6-flash",
            session_option(session),
        ]
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True
        st.button.side_effect = [False, False, True]

        render_sidebar()

        assert st.session_state["reset_db_requested"] is True

    def test_render_sidebar_configura_el_modelo(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        session = make_session()
        config = make_config(session)
        st.session_state["config"] = config

        st.selectbox.side_effect = [
            "gemini",
            "prompt.md",
            "gemini-3.5-flash",
            session_option(session),
        ]
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True
        st.button.return_value = False

        rendered = render_sidebar()

        assert rendered.model == "gemini-3.5-flash"

    def test_modelo_fuera_de_lista_resetea_a_primero(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        session = make_session()
        st.session_state["config"] = make_config(
            session, provider="gemini", model="mistral-small-latest"
        )

        st.selectbox.side_effect = [
            "gemini",
            "prompt.md",
            "gemini-3.6-flash",
            session_option(session),
        ]
        st.text_input.return_value = "5491112345678"
        st.checkbox.return_value = True
        st.button.return_value = False

        render_sidebar()

        model_call = st.selectbox.call_args_list[2]
        assert model_call.kwargs["index"] == 0


class TestSidebarSessions:
    def test_crear_sesion_la_activa_y_sincroniza(self, mock_st):
        from testing.components.sidebar import _render_sessions

        st = mock_st["sidebar"]
        first = make_session()
        config = make_config(first)
        st.selectbox.return_value = session_option(first)
        st.text_input.side_effect = ["Sesión 2", "5491187654321", "María"]
        st.checkbox.side_effect = [True, True]
        st.button.side_effect = [True, False]

        _render_sessions(config)

        assert len(config.sessions) == 2
        created = config.sessions[1]
        assert created.phone == "5491187654321"
        assert created.user_name == "María"
        assert created.user_registered is True
        assert config.active_session() is created
        mock_st["sync"].assert_called_once_with(
            ANY, phone="5491187654321", name="María", registered=True
        )
        st.error.assert_not_called()

    def test_telefono_vacio_no_crea_y_avisa(self, mock_st):
        from testing.components.sidebar import _render_sessions

        st = mock_st["sidebar"]
        first = make_session()
        config = make_config(first)
        st.selectbox.return_value = session_option(first)
        st.text_input.side_effect = ["Sesión 2", "   ", "María"]
        st.checkbox.return_value = True
        st.button.side_effect = [True]

        _render_sessions(config)

        assert config.sessions == [first]
        st.error.assert_called_once_with("Ingresá un teléfono")
        mock_st["sync"].assert_not_called()

    def test_telefono_repetido_no_crea_y_avisa(self, mock_st):
        from testing.components.sidebar import _render_sessions

        st = mock_st["sidebar"]
        first = make_session()
        config = make_config(first)
        st.selectbox.return_value = session_option(first)
        st.text_input.side_effect = ["Sesión 2", first.phone, "María"]
        st.checkbox.return_value = True
        st.button.side_effect = [True]

        _render_sessions(config)

        assert config.sessions == [first]
        st.error.assert_called_once_with("Ese teléfono ya está en uso")
        mock_st["sync"].assert_not_called()

    def test_cambiar_de_sesion_no_toca_los_mensajes(self, mock_st):
        from testing.components.sidebar import _render_sessions

        st = mock_st["sidebar"]
        first = make_session(messages=[{"role": "user", "content": "uno", "debug": {}}])
        second = make_session(
            "Sesión 2",
            "5491187654321",
            messages=[{"role": "user", "content": "dos", "debug": {}}],
        )
        config = make_config(first, second)
        st.selectbox.return_value = session_option(second)
        st.checkbox.return_value = True
        st.button.return_value = False

        active = _render_sessions(config)

        assert active is second
        assert config.active_session_id == second.id
        assert first.messages == [{"role": "user", "content": "uno", "debug": {}}]
        assert second.messages == [{"role": "user", "content": "dos", "debug": {}}]
        mock_st["sync"].assert_not_called()

    def test_toggle_vinculado_actualiza_y_sincroniza(self, mock_st):
        from testing.components.sidebar import _render_sessions

        st = mock_st["sidebar"]
        first = make_session(registered=True)
        config = make_config(first)
        st.selectbox.return_value = session_option(first)
        st.checkbox.side_effect = [False, True]
        st.button.return_value = False

        _render_sessions(config)

        assert first.user_registered is False
        mock_st["sync"].assert_called_once_with(
            ANY, phone=first.phone, name=first.user_name, registered=False
        )

    def test_eliminar_solo_con_mas_de_una_sesion(self, mock_st):
        from testing.components.sidebar import _render_sessions

        st = mock_st["sidebar"]
        first = make_session()
        second = make_session("Sesión 2", "5491187654321")
        config = make_config(first, second, active_index=1)
        st.selectbox.return_value = session_option(second)
        st.text_input.side_effect = ["Sesión 3", "", ""]
        st.checkbox.return_value = True
        st.button.side_effect = [False, True]

        _render_sessions(config)

        assert config.sessions == [first]
        assert config.active_session_id == first.id
        mock_st["sync"].assert_not_called()

    def test_no_hay_boton_eliminar_con_una_sola_sesion(self, mock_st):
        from testing.components.sidebar import _render_sessions

        st = mock_st["sidebar"]
        first = make_session()
        config = make_config(first)
        st.selectbox.return_value = session_option(first)
        st.checkbox.return_value = True
        st.button.return_value = False

        _render_sessions(config)

        assert config.sessions == [first]
        assert [call.kwargs["key"] for call in st.button.call_args_list] == ["create_session"]

    def test_limpiar_chat_vacia_solo_la_activa(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        active = make_session(messages=[{"role": "user", "content": "uno", "debug": {}}])
        other = make_session(
            "Sesión 2",
            "5491187654321",
            messages=[{"role": "user", "content": "dos", "debug": {}}],
        )
        config = make_config(active, other)
        st.session_state["config"] = config

        st.selectbox.side_effect = [
            "gemini",
            "prompt.md",
            "gemini-3.6-flash",
            session_option(active),
        ]
        st.text_input.side_effect = ["Sesión 3", "", ""]
        st.checkbox.return_value = True
        st.button.side_effect = [False, False, True, False]

        render_sidebar()

        assert active.messages == []
        assert other.messages == [{"role": "user", "content": "dos", "debug": {}}]


class TestSinModoDirecto:
    def test_config_no_tiene_campo_modo(self):
        from testing.config.settings import TestingConfig

        assert not hasattr(TestingConfig(), "mode")

    def test_sidebar_no_renderiza_radio_de_modo(self, mock_st):
        from testing.components.sidebar import render_sidebar

        st = mock_st["sidebar"]
        session = make_session()
        st.session_state["config"] = make_config(session)

        st.selectbox.side_effect = [
            "gemini",
            "prompt.md",
            "gemini-3.6-flash",
            session_option(session),
        ]
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

    def test_renders_memory_when_present(self, mock_st):
        from testing.components.debug_panel import render_debug

        st = mock_st["debug"]
        st.expander.return_value.__enter__ = MagicMock(return_value=None)
        st.expander.return_value.__exit__ = MagicMock(return_value=False)

        memory = [{"role": "user", "content": "hola"}]
        render_debug(
            {"memory": memory, "memory_ttl_seconds": 3600},
            {"json": False, "latency": False, "redis": True, "logs": False},
        )

        st.caption.assert_called_once()
        st.json.assert_called_once_with(memory)

    def test_omits_memory_when_absent(self, mock_st):
        from testing.components.debug_panel import render_debug

        st = mock_st["debug"]
        st.expander.return_value.__enter__ = MagicMock(return_value=None)
        st.expander.return_value.__exit__ = MagicMock(return_value=False)

        render_debug(
            {"redis_state": {"step": "none"}},
            {"json": False, "latency": False, "redis": True, "logs": False},
        )

        st.caption.assert_not_called()


class TestChatLogic:
    def test_export_omits_preview_bytes_and_marks_chart(self):
        from testing.components.chat import export_as_json

        exported = export_as_json([
            {
                "role": "assistant",
                "content": "Gastos por categoría",
                "debug": {},
                "image_pngs": [b"\x89PNG\r\n\x1a\nchart"],
            }
        ])

        assert "image_pngs" not in exported
        assert '"has_image_preview": true' in exported

    def test_get_prompt_path_default(self):
        from testing.components.chat import _get_prompt_path

        config = TestingConfig(prompt_path="prompt.md")
        assert _get_prompt_path(config) == "prompt.md"

    def test_get_prompt_path_custom(self):
        from testing.components.chat import _get_prompt_path

        config = TestingConfig(prompt_path="prompt_v2.md")
        assert _get_prompt_path(config) == "testing/prompts/prompt_v2.md"

    def test_get_prompt_path_repo_relative(self):
        from testing.components.chat import _get_prompt_path

        config = TestingConfig(prompt_path="prompts/core_prompt.md")
        assert _get_prompt_path(config) == "prompts/core_prompt.md"

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
            reply, debug, image_pngs = await _process_message(
                "Gasté 5000", config, "5491187654321"
            )

        assert reply == "✅ registrado"
        assert debug["service_log"] == "finance"
        assert debug["redis_state"]["step"] == "none"
        assert image_pngs == []
        assert mock_send.await_args.kwargs["phone"] == "5491187654321"

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
            reply, debug, image_pngs = await _process_message(
                "test", config, "5491112345678"
            )

        assert debug["service_log"] == "unknown"
        assert image_pngs == []

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
        config = make_config(
            make_session(
                messages=[
                    {"role": "user", "content": "hola", "debug": {}},
                    {
                        "role": "assistant",
                        "content": "respuesta",
                        "debug": {"raw_json": {"intent": "greeting"}, "latency_ms": 5.0},
                    },
                ]
            ),
            debug_json=True,
        )
        chat_st.chat_input.return_value = None

        chat_msg = MagicMock()
        chat_msg.__enter__ = MagicMock(return_value=None)
        chat_msg.__exit__ = MagicMock(return_value=False)
        chat_st.chat_message.return_value = chat_msg

        render_chat(config)

        assert chat_st.chat_message.call_count == 2

    def test_render_chat_existing_chart_renders_preview(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        config = make_config(
            make_session(messages=[
                {
                    "role": "assistant",
                    "content": "Gastos por categoría",
                    "debug": {},
                    "image_pngs": [b"\x89PNG\r\n\x1a\nchart"],
                }
            ])
        )
        chat_st.chat_input.return_value = None
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)

        render_chat(config)

        chat_st.image.assert_called_once()


class TestRenderChat:
    def test_render_chat_sin_sesion_activa_avisa(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]

        render_chat(TestingConfig())

        chat_st.info.assert_called_once()
        chat_st.chat_message.assert_not_called()

    def test_render_chat_displays_and_handles_input(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        config = make_config(make_session())
        chat_st.chat_input.return_value = None

        render_chat(config)

        chat_st.chat_message.assert_not_called()
        assert config.active_session().messages == []

    def test_render_chat_appendea_en_la_sesion_activa(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        first = make_session()
        second = make_session("Sesión 2", "5491187654321")
        config = make_config(first, second, active_index=1)
        chat_st.chat_input.return_value = "hola"
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.spinner.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.spinner.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "testing.components.chat._process_message",
                new_callable=AsyncMock,
                return_value=("respuesta", {"latency_ms": 1.0}, None),
            ),
        ):
            render_chat(config)

        assert second.messages[0]["role"] == "user"
        assert second.messages[1]["content"] == "respuesta"
        assert first.messages == []

    def test_pipeline_recibe_el_telefono_de_la_sesion_activa(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        first = make_session()
        second = make_session("Sesión 2", "5491187654321")
        config = make_config(first, second, active_index=1)
        chat_st.chat_input.return_value = "hola"
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.spinner.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.spinner.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "testing.components.chat._process_message",
                new_callable=AsyncMock,
                return_value=("respuesta", {"latency_ms": 1.0}, None),
            ) as mock_process,
        ):
            render_chat(config)

        assert mock_process.await_count == 1
        assert mock_process.call_args.args[2] == second.phone

    def test_render_chat_no_renderiza_mensajes_de_otra_sesion(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        first = make_session(messages=[{"role": "user", "content": "uno", "debug": {}}])
        second = make_session(
            "Sesión 2",
            "5491187654321",
            messages=[
                {"role": "user", "content": "dos", "debug": {}},
                {"role": "assistant", "content": "tres", "debug": {}},
            ],
        )
        config = make_config(first, second)
        chat_st.chat_input.return_value = None
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)

        render_chat(config)

        assert chat_st.chat_message.call_count == 1
        chat_st.write.assert_called_once_with("uno")

    def test_render_chat_empty_response(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        config = make_config(make_session())
        chat_st.chat_input.return_value = "hola"
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.spinner.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.spinner.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "testing.components.chat._process_message",
                new_callable=AsyncMock,
                return_value=("", {"latency_ms": 1.0}, None),
            ),
        ):
            render_chat(config)

        assert config.active_session().messages[1]["content"] == "Sin respuesta"

    def test_muestra_error_del_llm_con_st_error(self, mock_st):
        from testing.components.chat import render_chat

        st = mock_st["chat"]
        config = make_config(make_session())
        st.chat_input.return_value = "Gasté 5000 en supermercado"
        st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        st.spinner.return_value.__enter__ = MagicMock(return_value=None)
        st.spinner.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "testing.components.chat._process_message",
                new_callable=AsyncMock,
                return_value=(
                    "No he podido analizar tu mensaje en este momento.",
                    {"raw_json": {"intent": "out_of_scope", "error": "RuntimeError: API timeout"}},
                    None,
                ),
            ),
        ):
            render_chat(config)

        st.error.assert_called_once()

    def test_render_chat_assistant_history_uses_markdown_renderer(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        config = make_config(
            make_session(messages=[{"role": "assistant", "content": "l1\nl2", "debug": {}}])
        )
        chat_st.chat_input.return_value = None

        chat_msg = MagicMock()
        chat_msg.__enter__ = MagicMock(return_value=None)
        chat_msg.__exit__ = MagicMock(return_value=False)
        chat_st.chat_message.return_value = chat_msg

        render_chat(config)

        chat_st.markdown.assert_called_once_with("l1  \nl2")
        chat_st.write.assert_not_called()

    def test_render_chat_new_assistant_reply_uses_markdown_renderer(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        config = make_config(make_session())
        chat_st.chat_input.return_value = "hola"
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)
        chat_st.spinner.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.spinner.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "testing.components.chat._process_message",
                new_callable=AsyncMock,
                return_value=("l1\nl2", {"latency_ms": 1.0}, None),
            ),
        ):
            render_chat(config)

        chat_st.markdown.assert_called_once_with("l1  \nl2")

    def test_render_chat_user_message_still_uses_write(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        config = make_config(
            make_session(messages=[{"role": "user", "content": "hola", "debug": {}}])
        )
        chat_st.chat_input.return_value = None

        chat_msg = MagicMock()
        chat_msg.__enter__ = MagicMock(return_value=None)
        chat_msg.__exit__ = MagicMock(return_value=False)
        chat_st.chat_message.return_value = chat_msg

        render_chat(config)

        chat_st.write.assert_called_once_with("hola")
        chat_st.markdown.assert_not_called()


class TestRenderChatCopyButtons:
    def _render_with_messages(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        config = make_config(
            make_session(
                messages=[
                    {"role": "user", "content": "hola", "debug": {}},
                    {
                        "role": "assistant",
                        "content": "ok",
                        "debug": {"latency_ms": 42.0, "raw_json": {"intent": "greeting"}},
                    },
                ]
            )
        )
        chat_st.chat_input.return_value = None
        chat_st.chat_message.return_value.__enter__ = MagicMock(return_value=None)
        chat_st.chat_message.return_value.__exit__ = MagicMock(return_value=False)

        render_chat(config)

    def test_renders_two_copy_buttons_with_and_without_debug(self, mock_st):
        self._render_with_messages(mock_st)

        calls = mock_st["components_html"].call_args_list
        plain = [call for call in calls if "📋 Copiar conversación" in call.args[0]]
        debug = [call for call in calls if "🐞 Copiar con debug" in call.args[0]]
        assert len(plain) == 1
        assert len(debug) == 1
        assert "latency_ms" not in plain[0].args[0]
        assert "latency_ms" in debug[0].args[0]

    def test_skips_copy_buttons_without_messages(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        config = make_config(make_session())
        chat_st.chat_input.return_value = None

        render_chat(config)

        mock_st["components_html"].assert_not_called()

    def test_exporta_todas_las_sesiones_si_solo_la_inactiva_tiene_mensajes(self, mock_st):
        from testing.components.chat import render_chat

        chat_st = mock_st["chat"]
        empty = make_session()
        other = make_session(
            "Sesión 2",
            "5491187654321",
            messages=[{"role": "user", "content": "dos", "debug": {}}],
        )
        config = make_config(empty, other)
        chat_st.chat_input.return_value = None

        render_chat(config)

        assert [call.kwargs["key"] for call in chat_st.download_button.call_args_list] == [
            "export_all_json",
            "export_all_text",
        ]
        calls = mock_st["components_html"].call_args_list
        assert len(calls) == 1
        assert "🐞 Copiar todas con debug" in calls[0].args[0]
