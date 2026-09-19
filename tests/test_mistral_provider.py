"""Tests del MistralProvider sobre el driver común de retry/fallback."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.llm_providers.mistral import MistralProvider
from tests.provider_fakes import FakeAsyncClient, FakeResponse

VALID_JSON = '{"intent": "expense", "amount": 5000}'


@asynccontextmanager
async def _run_generate(responses, model="mistral-small-latest"):
    provider = MistralProvider()
    client = FakeAsyncClient(responses)
    with (
        patch.dict("os.environ", {"MISTRAL_API_KEY": "test-key", "MISTRAL_MODEL": model}, clear=False),
        patch("app.services.llm_providers.mistral.httpx.AsyncClient", lambda **kw: client),
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
    models = [b["model"] for b in client.requested_bodies]
    assert "mistral-small-latest" in models
    assert "ministral-8b-latest" in models


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
    assert all(b["model"] == "mistral-small-latest" for b in client.requested_bodies)


@pytest.mark.asyncio
async def test_404_cambia_inmediatamente_al_siguiente_modelo():
    async with _run_generate([FakeResponse(404), FakeResponse(200, text=VALID_JSON)]) as (provider, client):
        result = await provider.generate_json("sys", "user")
    assert result["intent"] == "expense"
    assert client.requested_bodies[-1]["model"] == "ministral-8b-latest"


@pytest.mark.asyncio
async def test_json_invalido_expone_el_error_de_parseo():
    async with _run_generate([FakeResponse(200, text="esto no es json")]) as (provider, client):
        with pytest.raises(json.JSONDecodeError):
            await provider.generate_json("sys", "user")


@pytest.mark.asyncio
async def test_sin_api_key_lanza_runtime_error():
    provider = MistralProvider()
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(RuntimeError, match="MISTRAL_API_KEY"):
            await provider.generate_json("sys", "user")


@pytest.mark.asyncio
async def test_mistral_sin_choices_levanta_value_error():
    payload = {"choices": []}
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = payload
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch.dict("os.environ", {"MISTRAL_API_KEY": "test-key"}, clear=False),
        patch("app.services.llm_providers.mistral.httpx.AsyncClient", return_value=mock_client),
    ):
        provider = MistralProvider()
        with pytest.raises(ValueError, match="no choices"):
            await provider.generate_json("sys", "user")


@pytest.mark.asyncio
async def test_history_is_sent_with_roles_in_one_request():
    history = [
        {"role": "user", "content": "mostrame mis transacciones"},
        {"role": "assistant", "content": "¿Gastos, ingresos o todos?"},
    ]
    async with _run_generate([FakeResponse(200, text=VALID_JSON)]) as (provider, client):
        await provider.generate_json("sys", "todos", history=history)

    assert client.requested_bodies[0]["messages"] == [
        {"role": "system", "content": "sys"},
        *history,
        {"role": "user", "content": "todos"},
    ]
