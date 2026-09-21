from unittest.mock import AsyncMock, patch

import pytest

from app.services.financial_education import FinancialEducationService, load_glossary
from app.services.llm import LLMService


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


def test_conceptual_detection_accepts_an_amount_inside_an_example():
    assert FinancialEducationService.is_conceptual_query(
        "Si ahorro 1000 por mes, ¿cómo funciona el interés compuesto?"
    )
    assert not FinancialEducationService.is_conceptual_query("Ahorré 1000 pesos hoy")


def test_budget_command_is_not_intercepted_as_education():
    assert not FinancialEducationService.is_conceptual_query("Compensá mi presupuesto")


def test_glossary_is_versioned_and_safe_for_a_shared_static_prompt():
    glossary = load_glossary()
    static_context = FinancialEducationService.static_prompt_context()

    assert glossary.version == "1.0.0"
    assert "GLOSARIO FINANCIERO VERSIONADO v1.0.0" in static_context
    assert "historial" not in static_context.lower()
    assert "datos de usuarios" not in static_context.lower()


@pytest.mark.asyncio
async def test_llm_service_accepts_education_intent_and_includes_static_glossary():
    provider = AsyncMock()
    provider.generate_json.return_value = {
        "intent": "financial_education",
        "education_term": "interés compuesto",
        "movement_type": None,
        "amount": None,
        "expense": None,
        "reply_text": "",
    }

    with patch.object(LLMService, "_get_provider", return_value=provider):
        result = await LLMService.process_message("¿Qué es el interés compuesto?")

    assert result["intent"] == "financial_education"
    assert result["education_term"] == "interés compuesto"
    assert result["movements"] == []
    sent_prompt = provider.generate_json.await_args.kwargs["system_prompt"]
    assert "GLOSARIO FINANCIERO VERSIONADO v1.0.0" in sent_prompt
