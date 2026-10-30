"""Contrato de redacción financiera con placeholders (STK-45 / STK-217)."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.currency import QuoteType, UsdQuote
from app.services.assistant_capabilities.financial_reads import net_worth_facts, usd_quote_facts
from app.services.llm import LLMService, _violates_financial_reply_contract
from app.services.net_worth import NetWorthResult, NetWorthStatus

pytestmark = pytest.mark.unit

PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "financial_reply_prompt.md"
)
FETCHED_AT = datetime(2026, 10, 23, 15, 30, tzinfo=timezone.utc)
ALL_TYPES = [QuoteType.OFICIAL, QuoteType.MEP, QuoteType.BLUE]

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


def test_prompt_allows_grouped_facts_within_twelve_line_cap():
    text = PROMPT_PATH.read_text(encoding="utf-8")

    assert "Máximo 12 líneas" in text
    assert "agrupar varios facts en una misma línea" in text
    assert "cada `key` exactamente una vez" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("brace", ["{", "}"])
async def test_render_falls_back_on_single_brace_left_in_prose(brace):
    bad_reply = f"*Tu patrimonio*\n• ARS: {{{{ARS_TOTAL}}}}\n• Blue: {{{{BLUE_TOTAL}}}} {brace}"
    result, provider = await _render([{"reply_text": bad_reply}, {"reply_text": bad_reply}])

    assert provider.generate_json.await_count == 2
    assert result == EXPECTED_FALLBACK


@pytest.mark.asyncio
async def test_render_accepts_words_that_only_start_like_forbidden_metadata():
    # `ventaja` y `comprar` no son los metadatos prohibidos `venta` y `compra`.
    reply = "*Ventaja de comprar blue*\n• ARS: {{ARS_TOTAL}}\n• Blue: {{BLUE_TOTAL}}"
    result, provider = await _render([{"reply_text": reply}], quote_types=["blue"])

    assert provider.generate_json.await_count == 1
    assert result == "*Ventaja de comprar blue*\n• ARS: $80.000\n• Blue: $188.000 ARS"


def _quote(tipo, compra="1000", venta="1100", *, stale=False):
    return UsdQuote(
        tipo=tipo,
        compra=Decimal(compra) if compra is not None else None,
        venta=Decimal(venta),
        fuente="DolarAPI",
        as_of=FETCHED_AT,
        fetched_at=FETCHED_AT,
        stale=stale,
    )


def _template(facts, title, field):
    lines = [title] + [f"• {fact['label']}: {fact[field]}" for fact in facts]
    return "\n".join(lines)


def _token_template(facts, title):
    return "\n".join(
        [title] + ["• " + fact["label"] + ": {{" + fact["key"] + "}}" for fact in facts]
    )


async def _render_facts(reply, facts, quote_types, kind):
    with patch.object(LLMService, "_get_provider") as get_provider:
        provider = AsyncMock()
        provider.generate_json.return_value = {"reply_text": reply}
        get_provider.return_value = provider
        result = await LLMService.render_financial_reply(kind, facts, quote_types)
    return result, provider


@pytest.mark.asyncio
async def test_net_worth_facts_match_renderer_contract():
    result = NetWorthResult(
        status=NetWorthStatus.OK,
        ars_total=Decimal("80000"),
        usd_total=Decimal("100"),
        totals={
            QuoteType.OFICIAL: Decimal("180000"),
            QuoteType.MEP: Decimal("190000"),
            QuoteType.BLUE: Decimal("200000"),
        },
        quotes={
            QuoteType.OFICIAL: _quote(QuoteType.OFICIAL),
            QuoteType.MEP: _quote(QuoteType.MEP),
            QuoteType.BLUE: _quote(QuoteType.BLUE, stale=True),
        },
        missing=(),
        stale=(QuoteType.BLUE,),
    )
    facts, quote_types = net_worth_facts(result, ALL_TYPES)

    assert [fact["key"] for fact in facts] == [
        "ARS_TOTAL",
        "USD_TOTAL",
        "OFICIAL_TOTAL",
        "MEP_TOTAL",
        "BLUE_TOTAL",
        "BLUE_STALE",
    ]
    assert quote_types == ["oficial", "mep", "blue"]

    reply = _token_template(facts, "*Tu patrimonio*")
    assert _violates_financial_reply_contract(reply, facts, quote_types, "net_worth") is False

    rendered, provider = await _render_facts(reply, facts, quote_types, "net_worth")
    assert provider.generate_json.await_count == 1
    assert rendered == _template(facts, "*Tu patrimonio*", "value")


@pytest.mark.asyncio
async def test_usd_quote_facts_match_renderer_contract_with_stale_and_missing():
    quotes = {
        QuoteType.OFICIAL: _quote(QuoteType.OFICIAL, stale=True),
        QuoteType.MEP: _quote(QuoteType.MEP, compra=None),
    }
    facts, quote_types = usd_quote_facts(quotes, ALL_TYPES)

    assert [fact["key"] for fact in facts] == [
        "OFICIAL_COMPRA",
        "OFICIAL_VENTA",
        "OFICIAL_AS_OF",
        "OFICIAL_STALE",
        "MEP_COMPRA",
        "MEP_VENTA",
        "MEP_AS_OF",
        "BLUE_STATUS",
        "SOURCE",
        "FETCHED_AT",
    ]
    assert quote_types == ["oficial", "mep", "blue"]

    reply = _token_template(facts, "*Cotizaciones*")
    assert _violates_financial_reply_contract(reply, facts, quote_types, "usd_quotes") is False

    rendered, provider = await _render_facts(reply, facts, quote_types, "usd_quotes")
    assert provider.generate_json.await_count == 1
    assert rendered == _template(facts, "*Cotizaciones*", "value")
