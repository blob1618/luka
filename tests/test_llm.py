import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.llm import LLMService
from app.services.llm_providers import GeminiProvider, MistralProvider, create_provider






# =============================================================================
# Tests de LLMService - process_message (nuevo multi-intent)
# =============================================================================



































































def test_get_provider_creates_and_caches_singleton():
    LLMService._provider = None
    try:
        with patch("app.services.llm.create_provider") as mock_create:
            mock_provider = MagicMock()
            mock_create.return_value = mock_provider

            provider = LLMService._get_provider()

            assert provider is mock_provider
            assert LLMService._provider is mock_provider
            assert mock_create.call_count == 1

            # La segunda llamada usa el singleton cacheado
            assert LLMService._get_provider() is mock_provider
            assert mock_create.call_count == 1
    finally:
        LLMService._provider = None


def test_reset_provider_clears_cached_instance():
    LLMService._provider = MagicMock()
    try:
        LLMService.reset_provider()
        assert LLMService._provider is None
    finally:
        LLMService._provider = None


# =============================================================================
# Tests de carga de prompt
# =============================================================================

@pytest.mark.asyncio
async def test_system_prompt_loaded_from_explicit_path(tmp_path):
    """Prueba: El system prompt se carga correctamente desde una ruta explícita."""
    prompt_content = "# Test Prompt\nEres LUKA, un asistente de prueba."
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(prompt_content, encoding="utf-8")

    # Reset cached prompt
    LLMService._system_prompt = None
    LLMService.set_prompt_path(str(prompt_file))

    loaded = LLMService._load_system_prompt()
    assert "Eres LUKA" in loaded
    assert "asistente de prueba" in loaded
    LLMService.set_prompt_path(None)
    LLMService._system_prompt = None


@pytest.mark.asyncio
async def test_system_prompt_fallback_on_missing_explicit_file():
    """Prueba: Si no existe la ruta explícita, se usa el fallback interno."""
    LLMService._system_prompt = None
    LLMService.set_prompt_path("/ruta/inexistente/prompt.md")

    loaded = LLMService._load_system_prompt()
    assert "Eres LUKA" in loaded
    assert "asistente financiero" in loaded
    LLMService.set_prompt_path(None)
    LLMService._system_prompt = None


def test_system_prompt_default_loads_core_prompt(monkeypatch):
    """Prueba: Por defecto, busca prompts/core_prompt.md antes de prompt.md."""
    LLMService._system_prompt = None
    LLMService.set_prompt_path(None)
    monkeypatch.delenv("SYSTEM_PROMPT_PATH", raising=False)

    loaded = LLMService._load_system_prompt()
    assert "Sos Luka" in loaded
    assert "capacidades declaradas" in loaded
    LLMService._system_prompt = None


def test_system_prompt_default_fallback_to_legacy_when_core_missing(monkeypatch):
    """Prueba: Si core_prompt.md no existe, recurre a prompt.md."""
    LLMService._system_prompt = None
    LLMService.set_prompt_path(None)
    monkeypatch.delenv("SYSTEM_PROMPT_PATH", raising=False)

    orig_open = open

    def fake_open(path, *args, **kwargs):
        if "core_prompt.md" in str(path):
            raise FileNotFoundError("core_prompt not found")
        return orig_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=fake_open):
        loaded = LLMService._load_system_prompt()
        assert "Sos Luka" in loaded
    LLMService._system_prompt = None


def test_system_prompt_default_fallback_to_internal_when_both_missing(monkeypatch):
    """Prueba: Si ni core_prompt.md ni prompt.md existen, usa fallback interno."""
    LLMService._system_prompt = None
    LLMService.set_prompt_path(None)
    monkeypatch.delenv("SYSTEM_PROMPT_PATH", raising=False)

    with patch("builtins.open", side_effect=FileNotFoundError("file not found")):
        loaded = LLMService._load_system_prompt()
        assert "Eres LUKA, un asistente financiero personal" in loaded
    LLMService._system_prompt = None


def test_system_prompt_env_var_override(tmp_path, monkeypatch):
    """Prueba: SYSTEM_PROMPT_PATH tiene precedencia sobre la búsqueda por defecto."""
    custom_prompt = tmp_path / "custom_prompt.md"
    custom_prompt.write_text("Prompt personalizado por env", encoding="utf-8")

    LLMService._system_prompt = None
    LLMService.set_prompt_path(None)
    monkeypatch.setenv("SYSTEM_PROMPT_PATH", str(custom_prompt))

    loaded = LLMService._load_system_prompt()
    assert loaded == "Prompt personalizado por env"
    LLMService._system_prompt = None


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
    assert result["expense"] == "Taxi"
    assert result["amount"] == 1500.0


@pytest.mark.asyncio
async def test_gemini_provider_raises_on_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiProvider()
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        await provider.generate_json("sys", "msg")


@pytest.mark.asyncio
async def test_gemini_provider_quota_error_does_not_try_fallback_or_leak_key(monkeypatch):
    api_key = "secret-test-key"
    monkeypatch.setenv("GEMINI_API_KEY", api_key)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-primary")

    request = httpx.Request(
        "POST",
        "https://example.test/models/gemini-primary:generateContent",
    )
    response = httpx.Response(
        429,
        request=request,
        json={"error": {"status": "RESOURCE_EXHAUSTED"}},
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=response)

    with (
        patch("app.services.llm_providers.base.asyncio.sleep", new_callable=AsyncMock),
        patch("app.services.llm_providers.gemini.httpx.AsyncClient", return_value=mock_client),
    ):
        provider = GeminiProvider()
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await provider.generate_json("sys", "msg")

    assert mock_client.post.await_count == provider.MAX_RETRIES_PER_MODEL
    assert api_key not in str(exc_info.value)


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
    raw = 'Texto previo {"intent": "expense", "amount": null} texto final'
    result = provider._safe_json_loads(raw)
    assert result["amount"] is None


def test_safe_json_loads_invalid_raises():
    provider = GeminiProvider()
    with pytest.raises(json.JSONDecodeError):
        provider._safe_json_loads("esto no es json en absoluto!!!")


class TestCreateReminderIntent:
    """Tests for intent create_reminder in LLMService.process_message()."""






class TestCompensationIntents:
    """Tests for budget compensation intents in LLMService.process_message()."""
