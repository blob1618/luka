from pathlib import Path

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "core_prompt.md"


def _prompt_text() -> str:
    return PROMPT.read_text(encoding="utf-8")


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
    "expense", "budget_query", "reminder", "expense_summary",
    "query_movements", "greeting", "out_of_scope", "create_reminder",
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
