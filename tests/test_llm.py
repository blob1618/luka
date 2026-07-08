import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.llm import LLMService
from app.services.llm_providers import GeminiProvider, MistralProvider, create_provider


# =============================================================================
# Tests de LLMService (fachada) - process_text_expense (legacy)
# =============================================================================

@pytest.mark.asyncio
async def test_process_text_expense_valid():
    # Mocking the LLM response
    mock_response = {
        "is_expense": True,
        "amount": 2500.50,
        "expense": "Supermercado",
        "currency": "ARS",
        "reply_text": "✅✨ Gasto registrado con éxito: Supermercado por 2500.50 ARS."
    }

    with patch("app.services.llm.LLMService.process_text_expense", return_value=mock_response):
        result = await LLMService.process_text_expense("Gasté 2500.5 en el supermercado")

        assert result.get("is_expense") is True
        assert result.get("amount") == 2500.50
        assert result.get("expense") == "Supermercado"

@pytest.mark.asyncio
async def test_process_text_expense_not_expense():
    # Mocking the LLM response for a non-expense message
    mock_response = {
        "is_expense": False,
        "reply_text": "📌 Este bot solo registra gastos."
    }

    with patch("app.services.llm.LLMService.process_text_expense", return_value=mock_response):
        result = await LLMService.process_text_expense("Hola, ¿cómo estás?")

        assert result.get("is_expense") is False
        assert "bot solo registra gastos" in result.get("reply_text")


# =============================================================================
# Tests de LLMService - process_message (nuevo multi-intent)
# =============================================================================

@pytest.mark.asyncio
async def test_process_message_valid_expense():
    """Prueba de éxito: El bot extrae correctamente los datos de un gasto válido."""
    mock_response = {
        "intent": "expense",
        "is_expense": True,
        "expense": "nafta",
        "amount": 3500.0,
        "currency": "ARS",
        "category": "transporte",
        "description": "carga de nafta",
        "reminder_title": None,
        "reminder_date": None,
        "reply_text": "✅ Gasto registrado: nafta por $3,500.00 ARS."
    }

    with patch.object(LLMService, "_get_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate_json.return_value = mock_response
        mock_get_provider.return_value = mock_provider

        result = await LLMService.process_message("Gasté 3500 en nafta")

        assert result["intent"] == "expense"
        assert result["is_expense"] is True
        assert result["expense"] == "nafta"
        assert result["amount"] == 3500.0
        assert result["currency"] == "ARS"
        assert result["category"] == "transporte"
        assert "Gasto registrado" in result["reply_text"]


@pytest.mark.asyncio
async def test_process_message_out_of_scope_recipe():
    """Prueba de rechazo: El bot se niega a procesar una solicitud de receta de cocina."""
    mock_response = {
        "intent": "out_of_scope",
        "is_expense": False,
        "expense": None,
        "amount": None,
        "currency": None,
        "category": None,
        "description": None,
        "reminder_title": None,
        "reminder_date": None,
        "reply_text": (
            "Solo puedo ayudarte con la gestión de tus finanzas personales. "
            "No tengo capacidad para darte recetas de cocina."
        )
    }

    with patch.object(LLMService, "_get_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate_json.return_value = mock_response
        mock_get_provider.return_value = mock_provider

        result = await LLMService.process_message("Dame una receta de cocina")

        assert result["intent"] == "out_of_scope"
        assert result["is_expense"] is False
        assert result["amount"] is None
        assert "finanzas personales" in result["reply_text"]


@pytest.mark.asyncio
async def test_process_message_out_of_scope_investment_advice():
    """Prueba de rechazo: El bot se niega a dar asesoría financiera profesional."""
    mock_response = {
        "intent": "out_of_scope",
        "is_expense": False,
        "expense": None,
        "amount": None,
        "currency": None,
        "category": None,
        "description": None,
        "reminder_title": None,
        "reminder_date": None,
        "reply_text": (
            "Entiendo tu interés en invertir, pero soy un asistente de registro financiero, "
            "no un asesor de inversiones. No puedo recomendarte acciones ni instrumentos financieros."
        )
    }

    with patch.object(LLMService, "_get_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate_json.return_value = mock_response
        mock_get_provider.return_value = mock_provider

        result = await LLMService.process_message("Recomiéndame en qué acciones invertir")

        assert result["intent"] == "out_of_scope"
        assert result["is_expense"] is False
        assert "asesor de inversiones" in result["reply_text"]


@pytest.mark.asyncio
async def test_process_message_greeting():
    """Prueba: El bot responde adecuadamente a un saludo."""
    mock_response = {
        "intent": "greeting",
        "is_expense": False,
        "expense": None,
        "amount": None,
        "currency": None,
        "category": None,
        "description": None,
        "reminder_title": None,
        "reminder_date": None,
        "reply_text": (
            "¡Hola! Soy LUKA, tu asistente financiero personal. "
            "Puedo ayudarte a registrar gastos, consultar presupuestos, "
            "programar recordatorios y más. ¿En qué puedo ayudarte?"
        )
    }

    with patch.object(LLMService, "_get_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate_json.return_value = mock_response
        mock_get_provider.return_value = mock_provider

        result = await LLMService.process_message("Hola")

        assert result["intent"] == "greeting"
        assert "LUKA" in result["reply_text"]


@pytest.mark.asyncio
async def test_process_message_budget_query():
    """Prueba: El bot reconoce una consulta de presupuesto."""
    mock_response = {
        "intent": "budget_query",
        "is_expense": False,
        "expense": None,
        "amount": None,
        "currency": None,
        "category": "comida",
        "description": None,
        "reminder_title": None,
        "reminder_date": None,
        "reply_text": "Estoy consultando tu presupuesto de comida. Un momento por favor..."
    }

    with patch.object(LLMService, "_get_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate_json.return_value = mock_response
        mock_get_provider.return_value = mock_provider

        result = await LLMService.process_message("¿Cuánto me queda de presupuesto para comida?")

        assert result["intent"] == "budget_query"
        assert result["category"] == "comida"


@pytest.mark.asyncio
async def test_process_message_reminder():
    """Prueba: El bot reconoce una solicitud de recordatorio."""
    mock_response = {
        "intent": "reminder",
        "is_expense": False,
        "expense": None,
        "amount": None,
        "currency": None,
        "category": None,
        "description": None,
        "reminder_title": "pagar la tarjeta",
        "reminder_date": "2026-07-15",
        "reply_text": "✅ Recordatorio creado: pagar la tarjeta para el 15 de julio de 2026."
    }

    with patch.object(LLMService, "_get_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate_json.return_value = mock_response
        mock_get_provider.return_value = mock_provider

        result = await LLMService.process_message("Recordame pagar la tarjeta el 15 de julio")

        assert result["intent"] == "reminder"
        assert result["reminder_title"] == "pagar la tarjeta"
        assert result["reminder_date"] == "2026-07-15"


@pytest.mark.asyncio
async def test_process_message_expense_summary():
    """Prueba: El bot reconoce una solicitud de resumen de gastos."""
    mock_response = {
        "intent": "expense_summary",
        "is_expense": False,
        "expense": None,
        "amount": None,
        "currency": None,
        "category": None,
        "description": None,
        "reminder_title": None,
        "reminder_date": None,
        "reply_text": "Estoy consultando el resumen de tus gastos. Un momento por favor..."
    }

    with patch.object(LLMService, "_get_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate_json.return_value = mock_response
        mock_get_provider.return_value = mock_provider

        result = await LLMService.process_message("¿Cuánto gasté este mes?")

        assert result["intent"] == "expense_summary"


@pytest.mark.asyncio
async def test_process_message_fallback_on_exception():
    """Prueba: Cuando el LLM falla, el servicio devuelve un mensaje de error controlado."""
    with patch.object(LLMService, "_get_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate_json.side_effect = RuntimeError("API timeout")
        mock_get_provider.return_value = mock_provider

        result = await LLMService.process_message("Gasté 500 en comida")

        assert result["intent"] == "out_of_scope"
        assert result["is_expense"] is False
        assert "No he podido analizar" in result["reply_text"]


@pytest.mark.asyncio
async def test_process_message_invalid_intent_normalized():
    """Prueba: Un intent no reconocido se normaliza a out_of_scope."""
    mock_response = {
        "intent": "hazme_un_cafe",
        "is_expense": False,
        "expense": None,
        "amount": None,
        "currency": None,
        "category": None,
        "description": None,
        "reminder_title": None,
        "reminder_date": None,
        "reply_text": "No puedo ayudarte con eso."
    }

    with patch.object(LLMService, "_get_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate_json.return_value = mock_response
        mock_get_provider.return_value = mock_provider

        result = await LLMService.process_message("Haceme un café")

        assert result["intent"] == "out_of_scope"
        assert result["is_expense"] is False


# =============================================================================
# Tests de carga de prompt.md
# =============================================================================

@pytest.mark.asyncio
async def test_system_prompt_loaded_from_file(tmp_path):
    """Prueba: El system prompt se carga correctamente desde prompt.md."""
    prompt_content = "# Test Prompt\nEres LUKA, un asistente de prueba."
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(prompt_content, encoding="utf-8")

    # Reset cached prompt
    LLMService._system_prompt = None
    LLMService.set_prompt_path(str(prompt_file))

    loaded = LLMService._load_system_prompt()
    assert "Eres LUKA" in loaded
    assert "asistente de prueba" in loaded


@pytest.mark.asyncio
async def test_system_prompt_fallback_on_missing_file():
    """Prueba: Si no existe prompt.md, se usa un fallback."""
    LLMService._system_prompt = None
    LLMService.set_prompt_path("/ruta/inexistente/prompt.md")

    loaded = LLMService._load_system_prompt()
    assert "Eres LUKA" in loaded
    assert "asistente financiero" in loaded


# =============================================================================
# Tests del Factory
# =============================================================================

def test_factory_returns_gemini_provider():
    provider = create_provider("gemini")
    assert isinstance(provider, GeminiProvider)


def test_factory_returns_mistral_provider():
    provider = create_provider("mistral")
    assert isinstance(provider, MistralProvider)


def test_factory_case_insensitive():
    assert isinstance(create_provider("Gemini"), GeminiProvider)
    assert isinstance(create_provider("MISTRAL"), MistralProvider)


def test_factory_invalid_provider_raises():
    with pytest.raises(ValueError, match="no soportado"):
        create_provider("openai")


def test_factory_reads_env_var(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mistral")
    provider = create_provider()  # sin argumento → lee env var
    assert isinstance(provider, MistralProvider)


def test_factory_default_is_gemini(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    provider = create_provider()
    assert isinstance(provider, GeminiProvider)


# =============================================================================
# Tests de GeminiProvider
# =============================================================================

@pytest.mark.asyncio
async def test_gemini_provider_generate_json_success(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-flash")

    fake_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps({
                                "is_expense": True,
                                "expense": "Taxi",
                                "amount": 1500.0,
                                "currency": "ARS",
                                "reply_text": "Gasto registrado.",
                            })
                        }
                    ]
                }
            }
        ]
    }

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = fake_payload
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.llm_providers.gemini.httpx.AsyncClient", return_value=mock_client):
        provider = GeminiProvider()
        result = await provider.generate_json(
            system_prompt="Eres un asistente de gastos.",
            user_message="Pagué 1500 de taxi.",
        )

    assert result["is_expense"] is True
    assert result["expense"] == "Taxi"
    assert result["amount"] == 1500.0


@pytest.mark.asyncio
async def test_gemini_provider_raises_on_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiProvider()
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        await provider.generate_json("sys", "msg")


# =============================================================================
# Tests de MistralProvider
# =============================================================================

@pytest.mark.asyncio
async def test_mistral_provider_generate_json_success(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")
    monkeypatch.setenv("MISTRAL_MODEL", "mistral-small-latest")

    fake_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "is_expense": True,
                        "expense": "Almuerzo",
                        "amount": 3200.0,
                        "currency": "ARS",
                        "reply_text": "Gasto de almuerzo registrado exitosamente.",
                    })
                }
            }
        ]
    }

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = fake_payload
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.llm_providers.mistral.httpx.AsyncClient", return_value=mock_client):
        provider = MistralProvider()
        result = await provider.generate_json(
            system_prompt="Eres un asistente de gastos.",
            user_message="Almorcé por 3200 pesos.",
        )

    assert result["is_expense"] is True
    assert result["expense"] == "Almuerzo"
    assert result["amount"] == 3200.0


@pytest.mark.asyncio
async def test_mistral_provider_raises_on_missing_key(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    provider = MistralProvider()
    with pytest.raises(RuntimeError, match="MISTRAL_API_KEY"):
        await provider.generate_json("sys", "msg")


# =============================================================================
# Tests de LLMProvider
# =============================================================================

def test_safe_json_loads_valid():
    provider = GeminiProvider()
    result = provider._safe_json_loads('{"key": "value"}')
    assert result == {"key": "value"}


def test_safe_json_loads_embedded_json():
    provider = GeminiProvider()
    raw = 'Texto previo {"is_expense": false, "amount": null} texto final'
    result = provider._safe_json_loads(raw)
    assert result["is_expense"] is False


def test_safe_json_loads_invalid_raises():
    provider = GeminiProvider()
    with pytest.raises(json.JSONDecodeError):
        provider._safe_json_loads("esto no es json en absoluto!!!")