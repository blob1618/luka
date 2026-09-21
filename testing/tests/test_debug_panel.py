"""Tests for debug panel formatting logic."""


from testing.components.debug_panel import format_debug_for_export


class TestFormatDebugForExport:
    def test_includes_all_fields(self):
        debug_data = {
            "raw_json": {"intent": "expense", "amount": 5000},
            "latency_ms": 342.5,
            "service_log": "FinanceService.register_movement_with_category",
            "redis_state": {"step": "none"},
            "provider": "gemini",
            "prompt_used": "prompt.md",
            "memory": [{"role": "user", "content": "hola"}],
            "memory_ttl_seconds": 3600,
        }
        exported = format_debug_for_export(debug_data)

        assert exported["raw_json"]["intent"] == "expense"
        assert exported["latency_ms"] == 342.5
        assert exported["service_log"] == "FinanceService.register_movement_with_category"
        assert exported["redis_state"]["step"] == "none"
        assert exported["provider"] == "gemini"
        assert exported["memory"] == [{"role": "user", "content": "hola"}]
        assert exported["memory_ttl_seconds"] == 3600

    def test_handles_missing_fields(self):
        debug_data = {"latency_ms": 100.0}
        exported = format_debug_for_export(debug_data)

        assert exported["latency_ms"] == 100.0
        assert exported.get("raw_json") is None
        assert exported.get("redis_state") is None
        assert exported.get("memory") is None
        assert exported.get("memory_ttl_seconds") is None

    def test_handles_empty_dict(self):
        exported = format_debug_for_export({})
        assert isinstance(exported, dict)

    def test_handles_none_values(self):
        debug_data = {
            "raw_json": None,
            "latency_ms": 0.0,
            "service_log": None,
            "redis_state": None,
        }
        exported = format_debug_for_export(debug_data)
        assert exported["raw_json"] is None
