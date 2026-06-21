import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.llm import LLMService
from app.services.llm_providers import GeminiProvider, MistralProvider, create_provider


# =============================================================================
# Tests de LLMService (fachada)
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
