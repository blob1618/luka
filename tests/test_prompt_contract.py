import re
from pathlib import Path

import pytest

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "core_prompt.md"
PROMPTS = (
    Path(__file__).resolve().parent.parent / "prompt.md",
    PROMPT,
)
PROMPT_INTENTS = {
    "dashboard_link",
    "query_movements",
    "movement_chart",
    "out_of_scope",
}


def _prompt_text() -> str:
    return PROMPT.read_text(encoding="utf-8")


_EXAMPLE_BOUNDARY = re.compile(
    r"^(?:#{1,6}\s|(?:[-*]\s*)?(?:\*\*)?(?:usuario|ejemplo|example)\b|"
    r"(?:[-*]\s+|\d+\.\s+)(?:\*\*)?[\"“`])",
    re.IGNORECASE,
)


def _example_blocks(text: str, example: str) -> list[str]:
    lines = text.splitlines()
    starts = [
        index for index, line in enumerate(lines)
        if example.casefold() in line.casefold()
    ]
    blocks = []
    for start in starts:
        block = []
        for line in lines[start:]:
            if block and _EXAMPLE_BOUNDARY.match(line.strip()):
                break
            block.append(line.casefold())
        blocks.append("\n".join(block))
    return blocks


def _assert_prompt_intent_pair(text: str, example: str, intent: str) -> None:
    blocks = _example_blocks(text, example)
    competing_intents = PROMPT_INTENTS - {intent}
    assert blocks, f"Missing prompt example for {example!r}"
    for block in blocks:
        assert intent in block, f"Missing {example!r} → {intent}"
        assert not any(other in block for other in competing_intents), (
            f"Wrong intent paired with {example!r}: {block}"
        )


def _assert_prompt_non_access_pair(text: str, example: str) -> None:
    blocks = _example_blocks(text, example)
    assert blocks, f"Missing prompt example for {example!r}"
    for block in blocks:
        assert "out_of_scope" in block, f"Missing {example!r} → out_of_scope"
        assert "dashboard_link" not in block, (
            f"{example!r} is paired with dashboard_link: {block}"
        )


def test_prompt_has_binary_clarification_rule():
    text = _prompt_text()
    assert "SI Y SOLO SI" in text or "si y solo si" in text.lower()


def test_prompt_has_contrast_examples_for_clarification():
    text = _prompt_text()
    assert "Pagué algo" in text and "Cobré 200 mil" in text


def test_prompt_has_multiop_example():
    text = _prompt_text()
    assert '"movements"' in text
    # dos movimientos dentro del ejemplo multiop
    section = text.split('"movements"', 1)[1][:800]
    assert section.count('"movement_type"') >= 2


def test_prompt_categories_reference_context_list():
    text = _prompt_text().lower()
    assert "lista provista" in text or "categorías provistas" in text
    assert "'servicios' o 'luz'" not in text  # regla difusa eliminada


SUPPORTED_INTENTS = {
    "expense", "dashboard_link", "budget_query", "reminder", "expense_summary",
    "query_movements", "movement_chart", "greeting", "out_of_scope", "financial_education", "create_reminder",
    "list_reminders", "update_reminder", "pause_reminder",
    "activate_reminder", "delete_reminder", "enable_proactive_reminders",
    "disable_proactive_reminders", "confirm_category", "reject_category",
    "delete_category", "list_categories", "update_movement",
    "delete_movement", "create_limit", "change_limit", "list_limits",
    "delete_limit", "confirm_limit", "reject_limit", "reset_context",
    "compensate_budget", "confirm_compensation", "reject_compensation",
}


def test_prompt_covers_all_supported_intents():
    text = _prompt_text()
    for intent in SUPPORTED_INTENTS:
        assert f"`{intent}`" in text or f'"{intent}"' in text, f"Intent missing from prompt: {intent}"


def test_prompt_has_conversation_memory_section():
    text = _prompt_text()
    assert "## Memoria Conversacional" in text
    assert "contexto de referencia" in text


def test_prompt_examples_do_not_invent_categories_without_context():
    import re

    text = _prompt_text()
    examples_section = text.split("## Ejemplos de Referencia", 1)[1]
    matches = re.findall(r'"category"\s*:\s*"([^"]+)"', examples_section)
    assert not matches, f"Ejemplos en el prompt asignan categorías hardcodeadas sin contexto: {matches}"


@pytest.mark.parametrize("prompt_path", PROMPTS, ids=("legacy", "core"))
def test_prompts_route_dashboard_access_naturally_without_slash_shortcuts(prompt_path):
    text = prompt_path.read_text(encoding="utf-8")

    _assert_prompt_intent_pair(
        text, "quiero entrar al panel avanzado", "dashboard_link"
    )
    _assert_prompt_intent_pair(text, "pasame mi dashboard", "dashboard_link")
    _assert_prompt_non_access_pair(text, "¿qué es un dashboard?")
    _assert_prompt_intent_pair(text, "mostrame mis movimientos", "query_movements")
    _assert_prompt_intent_pair(
        text,
        "gráfico de ingresos y egresos de ocio",
        "movement_chart",
    )
    assert not any(command in text for command in ("/link", "/movimientos", "/egresos"))
