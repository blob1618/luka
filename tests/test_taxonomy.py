from app.services.categories_taxonomy import (
    category_explicitly_named_in_text,
    resolve_category,
    resolve_category_for_user,
)


def test_explicit_category_uses_unique_text_evidence():
    assert category_explicitly_named_in_text("Gasté 10000 en Uber") == "Transporte"
    assert category_explicitly_named_in_text("Gasté 12000 en supermercado") == "Comida"
    assert category_explicitly_named_in_text("Gasté en Uber y supermercado") is None


def test_exact_user_category_wins_over_taxonomy_alias():
    assert resolve_category_for_user("Supermercado", {"Comida", "Supermercado"}) == "Supermercado"


def test_resolve_synonym_to_canonical():
    assert resolve_category("luz") == "Servicios"
    assert resolve_category("ALIMENTACIÓN") == "Comida"
    assert resolve_category("  nafta ") == "Transporte"


def test_resolve_canonical_itself():
    assert resolve_category("servicios") == "Servicios"


def test_unknown_returns_none():
    assert resolve_category("cryptomoneda") is None
    assert resolve_category(None) is None


def test_user_custom_category_wins_over_none():
    assert resolve_category("Viáticos", user_category_names={"viáticos"}) == "Viáticos"


def test_taxonomy_beats_custom_on_collision():
    # sinónimo de taxonomía aunque el usuario tenga custom parecido distinto
    assert resolve_category("comida", user_category_names={"Alimentos"}) == "Comida"


def test_accent_and_case_insensitivity():
    assert resolve_category("ALQUILER") == "Vivienda"
    assert resolve_category("Expensas") == "Vivienda"
    assert resolve_category("ócio") == "Ocio"
    assert resolve_category("  SUELDO ") == "Ingresos"


def test_resolve_for_user_prefers_user_synonym_over_canonical():
    # LLM manda "luz" -> canónico "Servicios"; el usuario tiene "Luz" (sinónimo del mismo canónico)
    assert resolve_category_for_user("luz", {"Luz", "Comida"}) == "Luz"
    assert resolve_category_for_user("LUZ", {"luz"}) == "luz"


def test_resolve_for_user_returns_canonical_when_no_user_synonym():
    assert resolve_category_for_user("luz", set()) == "Servicios"
    assert resolve_category_for_user("luz", {"Otra"}) == "Servicios"


def test_resolve_for_user_exact_custom_match_not_in_taxonomy():
    assert resolve_category_for_user("Viáticos", {"viáticos"}) == "viáticos"


def test_resolve_for_user_unknown_returns_none():
    assert resolve_category_for_user("cryptomoneda", set()) is None
    assert resolve_category_for_user("cryptomoneda", {"Comida"}) is None
    assert resolve_category_for_user(None, {"Comida"}) is None

