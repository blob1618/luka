"""Contrato de redacción financiera con placeholders (STK-45 / STK-217)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm import LLMService

pytestmark = pytest.mark.unit

FACTS = [
    {"key": "ARS_TOTAL", "label": "Saldos ARS", "value": "$80.000"},
    {"key": "BLUE_TOTAL", "label": "Blue", "value": "$188.000 ARS"},
]
VALID_REPLY = "*Tu patrimonio*\n• ARS: {{ARS_TOTAL}}\n• Blue: {{BLUE_TOTAL}}"
EXPECTED_REPLY = "*Tu patrimonio*\n• ARS: $80.000\n• Blue: $188.000 ARS"
EXPECTED_FALLBACK = "• Saldos ARS: $80.000\n• Blue: $188.000 ARS"


async def _render(responses, *, kind="net_worth", quote_types=("blue",), facts=None):
    with patch.object(LLMService, "_get_provider") as get_provider:
        provider = AsyncMock()
        if isinstance(responses, Exception):
            provider.generate_json.side_effect = responses
        else:
            provider.generate_json.side_effect = list(responses)
        get_provider.return_value = provider
        result = await LLMService.render_financial_reply(
            kind, FACTS if facts is None else facts, list(quote_types)
        )
    return result, provider


@pytest.mark.asyncio
async def test_render_substitutes_exact_values():
    result, provider = await _render([{"reply_text": VALID_REPLY}])

    assert result == EXPECTED_REPLY
    assert provider.generate_json.await_count == 1
    kwargs = provider.generate_json.await_args.kwargs
    assert kwargs["temperature"] == 0.1
    assert kwargs["history"] is None
    assert json.loads(kwargs["user_message"]) == {
        "kind": "net_worth",
        "quote_types": ["blue"],
        "facts": FACTS,
    }


@pytest.mark.asyncio
async def test_render_retries_once_when_token_is_missing():
    result, provider = await _render(
        [
            {"reply_text": "*Tu patrimonio*\n• ARS: {{ARS_TOTAL}}"},
            {"reply_text": VALID_REPLY},
        ]
    )

    assert provider.generate_json.await_count == 2
    assert result == EXPECTED_REPLY


@pytest.mark.asyncio
async def test_render_falls_back_on_unknown_token():
    bad_reply = "*Tu patrimonio*\n• ARS: {{ARS_TOTAL}}\n• Blue: {{BLUE_TOTAL}}\n• Otro: {{OTRO}}"
    result, provider = await _render([{"reply_text": bad_reply}, {"reply_text": bad_reply}])

    assert provider.generate_json.await_count == 2
    assert result == EXPECTED_FALLBACK


@pytest.mark.asyncio
async def test_render_falls_back_on_literal_digit_outside_token():
    bad_reply = "*Tu patrimonio*\n• ARS: {{ARS_TOTAL}}\n• Blue: $188.000 {{BLUE_TOTAL}}"
    result, provider = await _render([{"reply_text": bad_reply}, {"reply_text": bad_reply}])

    assert provider.generate_json.await_count == 2
    assert result == EXPECTED_FALLBACK


@pytest.mark.asyncio
async def test_render_falls_back_when_token_is_not_in_bullet_line():
    bad_reply = "*Tu patrimonio*\n{{ARS_TOTAL}}\n• Blue: {{BLUE_TOTAL}}"
    result, provider = await _render([{"reply_text": bad_reply}, {"reply_text": bad_reply}])

    assert provider.generate_json.await_count == 2
    assert result == EXPECTED_FALLBACK


@pytest.mark.asyncio
async def test_render_falls_back_on_quote_type_not_requested():
    # Violación solo en prosa: sin metadata prohibida ni tokens inválidos.
    bad_reply = "*Resumen oficial*\n• ARS: {{ARS_TOTAL}}\n• Blue: {{BLUE_TOTAL}}"
    result, provider = await _render(
        [{"reply_text": bad_reply}, {"reply_text": bad_reply}],
        quote_types=["blue"],
    )

    assert provider.generate_json.await_count == 2
    assert result == EXPECTED_FALLBACK


@pytest.mark.asyncio
async def test_render_accepts_requested_quote_types():
    reply = "*Resumen oficial y blue*\n• ARS: {{ARS_TOTAL}}\n• Blue: {{BLUE_TOTAL}}"
    result, provider = await _render(
        [{"reply_text": reply}],
        quote_types=["blue", "oficial"],
    )

    assert provider.generate_json.await_count == 1
    assert result == "*Resumen oficial y blue*\n• ARS: $80.000\n• Blue: $188.000 ARS"
    assert "{{" not in result and "}}" not in result


@pytest.mark.asyncio
async def test_render_ignores_quote_and_metadata_words_inside_fact_keys():
    keyed_facts = [
        {"key": "BLUE_TOTAL", "label": "Blue", "value": "$188.000 ARS"},
        {"key": "OFICIAL_COMPRA", "label": "Oficial compra", "value": "$1.000"},
    ]
    reply = "*Tu patrimonio*\n• Total: {{OFICIAL_COMPRA}}\n• Blue: {{BLUE_TOTAL}}"
    result, provider = await _render(
        [{"reply_text": reply}],
        quote_types=["blue"],
        facts=keyed_facts,
    )

    assert provider.generate_json.await_count == 1
    assert result == "*Tu patrimonio*\n• Total: $1.000\n• Blue: $188.000 ARS"
    assert "{{" not in result and "}}" not in result


@pytest.mark.asyncio
async def test_render_allows_compra_and_venta_metadata_for_usd_quotes():
    reply = "*Cotizaciones*\n• Compra: {{ARS_TOTAL}}\n• Venta: {{BLUE_TOTAL}}"
    result, provider = await _render([{"reply_text": reply}], kind="usd_quotes")

    assert provider.generate_json.await_count == 1
    assert result == "*Cotizaciones*\n• Compra: $80.000\n• Venta: $188.000 ARS"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_token", ["{{otro}}", "{{ otro }}", "{{blue_total}}"])
async def test_render_falls_back_on_malformed_placeholder(bad_token):
    bad_reply = f"*Tu patrimonio*\n• ARS: {{{{ARS_TOTAL}}}}\n• Blue: {bad_token}"
    result, provider = await _render([{"reply_text": bad_reply}, {"reply_text": bad_reply}])

    assert provider.generate_json.await_count == 2
    assert result == EXPECTED_FALLBACK
    assert "{{" not in result and "}}" not in result


@pytest.mark.asyncio
async def test_render_falls_back_when_token_line_has_no_bullet_space():
    bad_reply = "*Tu patrimonio*\n• ARS: {{ARS_TOTAL}}\n•Blue: {{BLUE_TOTAL}}"
    result, provider = await _render([{"reply_text": bad_reply}, {"reply_text": bad_reply}])

    assert provider.generate_json.await_count == 2
    assert result == EXPECTED_FALLBACK


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", ["compra", "venta", "DolarAPI", "consultadas", "actualizada"])
async def test_render_falls_back_on_net_worth_metadata(forbidden):
    bad_reply = f"*Patrimonio {forbidden}*\n• ARS: {{ARS_TOTAL}}\n• Blue: {{BLUE_TOTAL}}"
    result, provider = await _render([{"reply_text": bad_reply}, {"reply_text": bad_reply}])

    assert provider.generate_json.await_count == 2
    assert result == EXPECTED_FALLBACK


@pytest.mark.asyncio
async def test_render_falls_back_without_retry_on_provider_error():
    result, provider = await _render(RuntimeError("boom"))

    assert provider.generate_json.await_count == 1
    assert result == EXPECTED_FALLBACK


@pytest.mark.asyncio
async def test_render_always_returns_string_when_facts_are_malformed():
    result, provider = await _render(RuntimeError("boom"), facts=[{"key": "ARS_TOTAL"}])

    assert provider.generate_json.await_count == 1
    assert isinstance(result, str)
