"""Tests del GeminiProvider sobre el driver común de retry/fallback."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.llm_providers.gemini import GeminiProvider
from tests.provider_fakes import FakeAsyncClient, FakeResponse

VALID_JSON = '{"intent": "expense", "amount": 5000}'


@asynccontextmanager
async def _run_generate(responses, model="gemini-3.1-flash-lite"):
    provider = GeminiProvider()
    client = FakeAsyncClient(responses)
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": model}, clear=False),
        patch("app.services.llm_providers.gemini.httpx.AsyncClient", lambda **kw: client),
    ):
        yield provider, client


@pytest.mark.asyncio
async def test_503_se_reintenta_y_luego_retorna_ok():
    async with _run_generate([FakeResponse(503), FakeResponse(200, text=VALID_JSON)]) as (provider, client):
        result = await provider.generate_json("sys", "user")
    assert result["intent"] == "expense"
    assert len(client.requested_urls) == 2


@pytest.mark.asyncio
async def test_503_repetido_cae_al_modelo_fallback():
    async with _run_generate(
        [FakeResponse(503), FakeResponse(503), FakeResponse(200, text=VALID_JSON)]
    ) as (provider, client):
        result = await provider.generate_json("sys", "user")
    assert result["intent"] == "expense"
    urls = client.requested_urls
    assert any("gemini-3.1-flash-lite" in u for u in urls)
    assert any("gemini-3.5-flash" in u for u in urls)


@pytest.mark.asyncio
async def test_503_en_todos_los_modelos_expone_el_error_real():
    async with _run_generate([FakeResponse(503)] * 6) as (provider, client):  # 2 reintentos x 3 modelos
        with pytest.raises(httpx.HTTPStatusError):
            await provider.generate_json("sys", "user")


@pytest.mark.asyncio
async def test_429_se_reintenta_sin_cambiar_de_modelo():
    async with _run_generate(
        [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, text=VALID_JSON)]
    ) as (provider, client):
        result = await provider.generate_json("sys", "user")
    assert result["intent"] == "expense"
    assert all("gemini-3.1-flash-lite" in u for u in client.requested_urls)


@pytest.mark.asyncio
async def test_404_cambia_inmediatamente_al_siguiente_modelo():
    async with _run_generate([FakeResponse(404), FakeResponse(200, text=VALID_JSON)]) as (provider, client):
        result = await provider.generate_json("sys", "user")
    assert result["intent"] == "expense"
    assert client.requested_urls[-1].endswith("gemini-3.5-flash:generateContent")


@pytest.mark.asyncio
async def test_json_invalido_expone_el_error_de_parseo():
    async with _run_generate([FakeResponse(200, text="esto no es json")]) as (provider, client):
        with pytest.raises(json.JSONDecodeError):
            await provider.generate_json("sys", "user")


@pytest.mark.asyncio
async def test_sin_api_key_lanza_runtime_error():
    provider = GeminiProvider()
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            await provider.generate_json("sys", "user")


def test_safe_json_loads_extrae_json_embebido():
    provider = GeminiProvider()
    raw = 'texto previo {"a": 1} texto posterior'
    assert provider._safe_json_loads(raw) == {"a": 1}


def test_safe_json_loads_parsea_json_plano():
    provider = GeminiProvider()
    assert provider._safe_json_loads('{"a": 1}') == {"a": 1}


@pytest.mark.asyncio
async def test_429_con_retry_after_invalido_usa_backoff():
    async with _run_generate(
        [FakeResponse(429, headers={"Retry-After": "no-es-numero"}), FakeResponse(200, text=VALID_JSON)]
    ) as (provider, client):
        result = await provider.generate_json("sys", "user")
    assert result["intent"] == "expense"
    assert len(client.requested_urls) == 2


@pytest.mark.asyncio
async def test_error_de_red_connect_error_se_propaga():
    async with _run_generate([httpx.ConnectError("network down")]) as (provider, client):
        with pytest.raises(httpx.ConnectError):
            await provider.generate_json("sys", "user")
    assert len(client.requested_urls) == 1


class _NoCandidatesProvider(GeminiProvider):
    """Provider que no ofrece ningún modelo candidato."""

    def _get_model_candidates(self, primary_model):
        return []


@pytest.mark.asyncio
async def test_sin_candidatos_de_modelo_lanza_runtime_error():
    provider = _NoCandidatesProvider()
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=False):
        with pytest.raises(RuntimeError, match="todos los modelos fallaron sin error registrado"):
            await provider.generate_json("sys", "user")


def test_normalize_model_name_strips_models_prefix():
    provider = GeminiProvider()
    assert provider._normalize_model_name("models/gemini-3.6-flash") == "gemini-3.6-flash"
    assert provider._normalize_model_name('"gemini-3.6-flash"') == "gemini-3.6-flash"
    assert provider._normalize_model_name("  models/gemini-3.5-flash  ") == "gemini-3.5-flash"


@pytest.mark.asyncio
async def test_gemini_sin_candidates_levanta_value_error():
    payload = {"candidates": []}
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = payload
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=False),
        patch("app.services.llm_providers.gemini.httpx.AsyncClient", return_value=mock_client),
    ):
        provider = GeminiProvider()
        with pytest.raises(ValueError, match="no candidates"):
            await provider.generate_json("sys", "user")


@pytest.mark.asyncio
async def test_gemini_candidates_con_parts_vacias_levanta_value_error():
    payload = {"candidates": [{"content": {"parts": []}}]}
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = payload
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=False),
        patch("app.services.llm_providers.gemini.httpx.AsyncClient", return_value=mock_client),
    ):
        provider = GeminiProvider()
        with pytest.raises(ValueError, match="empty content payload"):
            await provider.generate_json("sys", "user")


@pytest.mark.asyncio
async def test_history_is_sent_with_gemini_roles_in_one_request():
    history = [
        {"role": "user", "content": "mostrame mis transacciones"},
        {"role": "assistant", "content": "¿Gastos, ingresos o todos?"},
    ]
    async with _run_generate([FakeResponse(200, text=VALID_JSON)]) as (provider, client):
        await provider.generate_json("sys", "todos", history=history)

    assert client.requested_bodies[0]["contents"] == [
        {"role": "user", "parts": [{"text": "mostrame mis transacciones"}]},
        {"role": "model", "parts": [{"text": "¿Gastos, ingresos o todos?"}]},
        {"role": "user", "parts": [{"text": "todos"}]},
    ]
