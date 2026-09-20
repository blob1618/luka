"""Tests for chat state management and export logic."""

import json


from testing.components.chat import (
    copy_button_html,
    export_as_json,
    export_as_text,
    export_sessions_as_json,
    export_sessions_as_text,
)
from testing.config.settings import new_session


class TestExportAsJson:
    def test_exports_valid_json(self):
        messages = [
            {"role": "user", "content": "hola", "debug": {}},
            {
                "role": "assistant",
                "content": "¡Hola!",
                "debug": {
                    "raw_json": {"intent": "greeting"},
                    "latency_ms": 42.0,
                },
            },
        ]
        result = export_as_json(messages)
        parsed = json.loads(result)

        assert len(parsed) == 2
        assert parsed[0]["role"] == "user"
        assert parsed[1]["debug"]["raw_json"]["intent"] == "greeting"

    def test_exports_empty_list(self):
        result = export_as_json([])
        assert json.loads(result) == []

    def test_handles_special_characters(self):
        messages = [
            {"role": "user", "content": 'Gasté $5.000 en "super"', "debug": {}},
        ]
        result = export_as_json(messages)
        parsed = json.loads(result)
        assert "$5.000" in parsed[0]["content"]


class TestExportAsText:
    def test_formats_readable_conversation(self):
        messages = [
            {"role": "user", "content": "Gasté 5000 en super", "debug": {}},
            {"role": "assistant", "content": "✅ Registrado.", "debug": {}},
        ]
        result = export_as_text(messages)

        assert "Usuario: Gasté 5000 en super" in result
        assert "Luka: ✅ Registrado." in result

    def test_exports_empty_list(self):
        result = export_as_text([])
        assert result == ""

    def test_excludes_debug_data(self):
        messages = [
            {
                "role": "assistant",
                "content": "ok",
                "debug": {"raw_json": {"intent": "greeting"}, "latency_ms": 42.0},
            },
        ]
        result = export_as_text(messages)
        assert "intent" not in result
        assert "latency" not in result


class TestExportAsTextWithDebug:
    def test_includes_debug_data_when_requested(self):
        messages = [
            {"role": "user", "content": "hola", "debug": {}},
            {
                "role": "assistant",
                "content": "ok",
                "debug": {"raw_json": {"intent": "greeting"}, "latency_ms": 42.0},
            },
        ]
        result = export_as_text(messages, include_debug=True)

        assert "Usuario: hola" in result
        assert "Luka: ok" in result
        assert '"intent": "greeting"' in result
        assert '"latency_ms": 42.0' in result

    def test_skips_messages_without_debug(self):
        messages = [{"role": "assistant", "content": "ok", "debug": {}}]
        assert export_as_text(messages, include_debug=True) == "Luka: ok"

    def test_empty_list_returns_empty_string(self):
        assert export_as_text([], include_debug=True) == ""


class TestExportSessionsAsJson:
    def _sessions(self):
        first = new_session("Sesión 1", "5491112345678", "Test User", True)
        first.messages.append({"role": "user", "content": "Gasté 5000", "debug": {}})
        second = new_session("Sesión 2", "5491187654321", "María", False)
        return [first, second]

    def test_incluye_label_phone_registro_y_mensajes(self):
        parsed = json.loads(export_sessions_as_json(self._sessions()))

        assert [s["label"] for s in parsed] == ["Sesión 1", "Sesión 2"]
        assert [s["phone"] for s in parsed] == ["5491112345678", "5491187654321"]
        assert [s["user_registered"] for s in parsed] == [True, False]
        assert parsed[0]["messages"][0]["content"] == "Gasté 5000"
        assert parsed[1]["messages"] == []

    def test_preserva_unicode_e_indentacion(self):
        first = new_session("Sesión 1", "5491112345678", "Test User", True)
        first.messages.append({"role": "user", "content": "café", "debug": {}})

        result = export_sessions_as_json([first])

        assert "café" in result
        assert "\\u" not in result
        assert '\n    "phone"' in result


class TestExportSessionsAsText:
    def test_encabezados_y_separacion_entre_sesiones(self):
        first = new_session("Sesión 1", "5491112345678", "Test User", True)
        first.messages.append({"role": "user", "content": "hola", "debug": {}})
        second = new_session("Sesión 2", "5491187654321", "María", False)
        second.messages.append({"role": "assistant", "content": "respuesta", "debug": {}})

        result = export_sessions_as_text([first, second])

        assert "=== Sesión 1 (5491112345678) ===" in result
        assert "Usuario: hola" in result
        assert "\n\n=== Sesión 2 (5491187654321) ===" in result
        assert "Luka: respuesta" in result

    def test_sesion_sin_mensajes_muestra_sin_mensajes(self):
        session = new_session("Sesión 1", "5491112345678", "Test User", True)

        assert export_sessions_as_text([session]) == (
            "=== Sesión 1 (5491112345678) ===\n(sin mensajes)"
        )

    def test_debug_excluido_por_defecto_e_incluido_si_se_pide(self):
        session = new_session("Sesión 1", "5491112345678", "Test User", True)
        session.messages.append(
            {
                "role": "assistant",
                "content": "ok",
                "debug": {"raw_json": {"intent": "greeting"}, "latency_ms": 42.0},
            }
        )

        plain = export_sessions_as_text([session])
        with_debug = export_sessions_as_text([session], include_debug=True)

        assert "intent" not in plain
        assert '"intent": "greeting"' in with_debug
        assert '"latency_ms": 42.0' in with_debug


class TestCopyButtonHtml:
    def test_embeds_escaped_payload_and_label(self):
        markup = copy_button_html('Cerrá </script> & mirá "esto"', "📋 Copiar")

        assert "\\u003c/script\\u003e" in markup
        assert "\\u0026" in markup
        assert "📋 Copiar" in markup
        assert "navigator.clipboard.writeText" in markup
        assert "document.execCommand" in markup
