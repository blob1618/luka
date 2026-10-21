"""DeepSeek provider integration without external API calls."""

from unittest.mock import patch

import pytest

from app.services.llm_providers import DeepSeekProvider, create_provider
from tests.provider_fakes import FakeAsyncClient, FakeResponse


def test_factory_selects_deepseek(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    assert isinstance(create_provider(), DeepSeekProvider)


@pytest.mark.asyncio
async def test_deepseek_sends_json_request_with_history_and_parses_reply(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    client = FakeAsyncClient([FakeResponse(200, text='{"intent": "greeting"}')])

    with patch("app.services.llm_providers.deepseek.httpx.AsyncClient", return_value=client):
        result = await DeepSeekProvider().generate_json(
            "Return JSON", "hola", history=[{"role": "assistant", "content": "¡Hola!"}]
        )

    assert result == {"intent": "greeting"}
    assert client.requested_urls == ["https://api.deepseek.com/chat/completions"]
    body = client.requested_bodies[0]
    assert body["model"] == "deepseek-v4-pro"
    assert body["messages"] == [
        {"role": "system", "content": "Return JSON"},
        {"role": "assistant", "content": "¡Hola!"},
        {"role": "user", "content": "hola"},
    ]
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_deepseek_requires_its_own_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        await DeepSeekProvider().generate_json("Return JSON", "hola")


@pytest.mark.asyncio
async def test_deepseek_rejects_missing_choices(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    client = FakeAsyncClient([FakeResponse(200, text="")])
    with patch("app.services.llm_providers.deepseek.httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="no choices"):
            await DeepSeekProvider().generate_json("Return JSON", "hola")


@pytest.mark.asyncio
async def test_deepseek_rejects_empty_content(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    class EmptyContentResponse(FakeResponse):
        def json(self):
            return {"choices": [{"message": {"content": ""}}]}

    client = FakeAsyncClient([EmptyContentResponse(200)])
    with patch("app.services.llm_providers.deepseek.httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="empty content"):
            await DeepSeekProvider().generate_json("Return JSON", "hola")
