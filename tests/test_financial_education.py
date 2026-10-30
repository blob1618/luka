
import pytest

from app.services.financial_education import FinancialEducationService, load_glossary


@pytest.mark.parametrize(
    ("query", "term"),
    [
        ("¿Qué es un presupuesto?", "presupuesto"),
        ("Explicame gasto fijo", "gasto fijo"),
        ("¿Qué significa gasto variable?", "gasto variable"),
        ("¿Qué es ahorrar?", "ahorro"),
        ("Definí interés simple", "interés simple"),
        ("¿Qué es interés compuesto?", "interés compuesto"),
        ("¿Qué es la inflación?", "inflación"),
        ("Explicame qué es una deuda", "deuda"),
        ("¿Qué significa CFT?", "costo financiero total"),
    ],
)
def test_supported_terms_have_a_brief_illustrative_answer(query, term):
    reply = FinancialEducationService.answer(query)

    assert reply.status == "supported"
    assert reply.term == term
    assert "Ejemplo ilustrativo:" in reply.text


def test_ambiguous_interest_requests_clarification():
    reply = FinancialEducationService.answer("¿Qué es el interés?")

    assert reply.status == "ambiguous"
    assert "simple o interés compuesto" in reply.text


@pytest.mark.parametrize(
    "query",
    ["¿Qué es la TIR?", "¿Cuál es la inflación actual?"],
)
def test_unknown_or_current_information_is_never_invented(query):
    reply = FinancialEducationService.answer(query)

    assert reply.status in {"unsupported", "current_information_unavailable"}
    assert "No tengo una definición verificada" in reply.text or "No puedo confirmar" in reply.text


def test_glossary_is_versioned_and_safe_for_a_shared_static_prompt():
    glossary = load_glossary()
    static_context = FinancialEducationService.static_prompt_context()

    assert glossary.version == "1.0.0"
    assert "GLOSARIO FINANCIERO VERSIONADO v1.0.0" in static_context
    assert "historial" not in static_context.lower()
    assert "datos de usuarios" not in static_context.lower()
