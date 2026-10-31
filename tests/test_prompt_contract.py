"""The brief global prompt delegates domain schemas to the registry."""
from pathlib import Path
from app.services.assistant_catalog import build_tool_registry
from app.services.assistant_contracts import RegisterMovement


def test_prompt_has_bounded_capability_and_confirmation_rules():
    root=Path(__file__).resolve().parent.parent
    for path in [root/'prompt.md',root/'prompts/core_prompt.md']:
        text=path.read_text(encoding='utf-8')
        assert len(text)<2000 and 'capacidades declaradas' in text and 'mensaje posterior' in text
        assert 'No inventes commit ni confirm' in text
        assert '"intent"' not in text and 'taxonomía' not in text


def test_prompt_domains_live_in_typed_catalog_not_global_examples():
    registry=build_tool_registry()
    assert len(registry.tools)==43 and 'commit_command' not in registry.tools
    schema=RegisterMovement.model_json_schema()
    assert 'economic_effect' in schema['properties'] and 'cash_flow' in str(schema)
