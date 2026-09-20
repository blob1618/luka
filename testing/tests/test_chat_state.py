"""Tests for chat state management and export logic."""

import json


from testing.components.chat import copy_button_html, export_as_json, export_as_text


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


class TestCopyButtonHtml:
    def test_embeds_escaped_payload_and_label(self):
        markup = copy_button_html('Cerrá </script> & mirá "esto"', "📋 Copiar")

        assert "\\u003c/script\\u003e" in markup
        assert "\\u0026" in markup
        assert "📋 Copiar" in markup
        assert "navigator.clipboard.writeText" in markup
        assert "document.execCommand" in markup
