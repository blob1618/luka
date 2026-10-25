"""LLM contract for query_net_worth (STK-45 / STK-217)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm import LLMService

pytestmark = pytest.mark.unit


async def _process(raw_response=None, *, error: Exception | None = None):
    with patch.object(LLMService, "_get_provider") as get_provider:
        provider = AsyncMock()
        if error is not None:
            provider.generate_json.side_effect = error
        else:
            provider.generate_json.return_value = raw_response
        get_provider.return_value = provider
        return await LLMService.process_message("¿cuánto tengo?")


@pytest.mark.asyncio
async def test_query_net_worth_normalizes_quote_types_lowercase_unique_and_known():
    result = await _process(
        {
            "intent": "query_net_worth",
            "quote_types": ["BLUE", "blue", "mep", "dolar"],
            "reply_text": "",
        }
    )

    assert result["intent"] == "query_net_worth"
    assert result["quote_types"] == ["blue", "mep"]


@pytest.mark.asyncio
async def test_query_net_worth_accepts_single_string_quote_type():
    result = await _process({"intent": "query_net_worth", "quote_types": " MEP "})

    assert result["quote_types"] == ["mep"]


@pytest.mark.asyncio
async def test_query_net_worth_drops_non_list_quote_types():
    result = await _process({"intent": "query_net_worth", "quote_types": {"tipo": "blue"}})

    assert result["quote_types"] == []


@pytest.mark.asyncio
async def test_query_usd_quotes_normalizes_quote_types():
    result = await _process(
        {"intent": "query_usd_quotes", "quote_types": ["BLUE", "blue"]}
    )

    assert result["intent"] == "query_usd_quotes"
    assert result["quote_types"] == ["blue"]


@pytest.mark.asyncio
async def test_unknown_intent_remains_out_of_scope():
    result = await _process({"intent": "query_net_worth_unknown"})

    assert result["intent"] == "out_of_scope"
    assert result["quote_types"] == []


@pytest.mark.asyncio
async def test_llm_failure_fallback_has_empty_quote_types():
    result = await _process(error=RuntimeError("boom"))

    assert result["intent"] == "out_of_scope"
    assert result["quote_types"] == []
