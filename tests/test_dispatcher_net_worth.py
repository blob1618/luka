"""Dispatcher integration for query_net_worth (STK-45 / STK-217).

Every test drives the public message pipeline, so the LLM normalizer runs
before the dispatcher: this is the D1 regression this file exists to catch.
"""

import threading
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models.database import Billetera, MovimientoFinanciero, Usuario
from app.services import net_worth as net_worth_module
from app.services.currency import QuoteType, QuoteUnavailableError, UsdQuote
from app.services.dispatcher import DispatchResult, process_incoming_message
from app.services.llm import LLMService
from app.services.onboarding import OnboardingDecision, OnboardingResult

pytestmark = pytest.mark.unit

FETCHED_AT = datetime(2026, 10, 23, 15, 30, tzinfo=timezone.utc)
RENDERED_REPLY = "• respuesta redactada"
_METADATA_SUFFIXES = ("_COMPRA", "_VENTA", "_SOURCE", "_AS_OF", "_FETCHED_AT")


def known_user() -> OnboardingResult:
    return OnboardingResult(OnboardingDecision.KNOWN_USER)


def quote(
    tipo: QuoteType,
    compra: str | None = "1000",
    venta: str = "1100",
    *,
    stale: bool = False,
) -> UsdQuote:
    return UsdQuote(
        tipo=tipo,
        compra=Decimal(compra) if compra is not None else None,
        venta=Decimal(venta),
        fuente="DolarAPI",
        as_of=FETCHED_AT,
        fetched_at=FETCHED_AT,
        stale=stale,
    )


def all_three_quotes() -> dict[QuoteType, UsdQuote]:
    return {
        QuoteType.OFICIAL: quote(QuoteType.OFICIAL, "1000", "1100"),
        QuoteType.MEP: quote(QuoteType.MEP, "1100", "1200"),
        QuoteType.BLUE: quote(QuoteType.BLUE, "1200", "1300"),
    }


def seed_user(session, phone: str, wallets: list[tuple[str, str, list[tuple[str, str]]]]):
    user = Usuario(nombre=phone, email=f"{uuid.uuid4()}@example.com", whatsapp_id=phone)
    session.add(user)
    session.flush()
    for name, currency, movements in wallets:
        wallet = Billetera(usuario_id=user.id, nombre=name, moneda=currency)
        session.add(wallet)
        session.flush()
        for tipo, amount in movements:
            session.add(
                MovimientoFinanciero(
                    billetera_id=wallet.id,
                    usuario_id=user.id,
                    tipo=tipo,
                    cantidad=Decimal(amount),
                    moneda=currency,
                    descripcion="seed",
                    fecha_movimiento=date(2026, 10, 1),
                )
            )
    session.commit()
    return user.id


async def ask(
    phone: str, payload: dict
) -> tuple[DispatchResult, AsyncMock]:
    """Run one message through provider -> normalizer -> dispatcher -> renderer."""
    provider = AsyncMock()
    provider.generate_json.return_value = payload
    render = AsyncMock(return_value=RENDERED_REPLY)
    with (
        patch.object(LLMService, "_get_provider", return_value=provider),
        patch.object(LLMService, "render_financial_reply", render),
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=known_user(),
        ),
    ):
        result = await process_incoming_message(phone, "¿Cuánto tengo?")
    provider.generate_json.assert_awaited()
    return result, render


def facts_and_types(render: AsyncMock) -> tuple[dict[str, str], list[str]]:
    """Reads the contract the dispatcher hands to the financial renderer."""
    args = render.await_args
    assert args.args[0] == "net_worth"
    facts = {fact["key"]: fact["value"] for fact in args.args[1]}
    return facts, args.args[2]


def assert_no_metadata(facts: dict[str, str]) -> None:
    assert not any(key.endswith(_METADATA_SUFFIXES) for key in facts)


@pytest.mark.asyncio
async def test_query_net_worth_goes_through_normalizer_and_dispatcher(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000201",
            [("Efectivo", "ARS", [("ingreso", "80000")])],
        )

    result, render = await ask(
        "5491100000201",
        {
            "intent": "query_net_worth",
            "quote_types": ["BLUE", "blue", "mep", "dolar"],
            "reply_text": "Consultando.",
        },
    )

    facts, tipos = facts_and_types(render)
    assert result.intent == "query_net_worth"
    assert result.service_invoked == "net_worth"
    assert result.raw_llm_response["quote_types"] == ["blue", "mep"]
    assert result.raw_llm_response["reply_text"] == "Consultando."
    assert result.reply_text == RENDERED_REPLY
    assert facts["ARS_TOTAL"] == "$80.000"
    assert facts["USD_TOTAL"] == "sin saldo"
    assert tipos == []
    assert_no_metadata(facts)


@pytest.mark.asyncio
async def test_default_quote_types_requests_all_three(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000202",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90")]),
            ],
        )

    get_quotes = AsyncMock(return_value=all_three_quotes())
    with patch("app.services.dispatcher.get_usd_quotes", get_quotes):
        result, render = await ask("5491100000202", {"intent": "query_net_worth"})

    get_quotes.assert_awaited_once_with([QuoteType.OFICIAL, QuoteType.MEP, QuoteType.BLUE])
    facts, tipos = facts_and_types(render)
    assert tipos == ["oficial", "mep", "blue"]
    assert facts["ARS_TOTAL"] == "$80.000"
    assert facts["USD_TOTAL"] == "u$s90,00"
    assert facts["OFICIAL_TOTAL"] == "$170.000 ARS"
    assert facts["MEP_TOTAL"] == "$179.000 ARS"
    assert facts["BLUE_TOTAL"] == "$188.000 ARS"
    assert not any(key.endswith("_STALE") for key in facts)
    assert_no_metadata(facts)
    assert result.reply_text == RENDERED_REPLY


@pytest.mark.asyncio
async def test_single_blue_quote_type_only_values_blue(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000203",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90")]),
            ],
        )

    get_quotes = AsyncMock(
        return_value={QuoteType.BLUE: quote(QuoteType.BLUE, "1200", "1300")}
    )
    with patch("app.services.dispatcher.get_usd_quotes", get_quotes):
        result, render = await ask(
            "5491100000203",
            {"intent": "query_net_worth", "quote_types": ["BLUE"]},
        )

    get_quotes.assert_awaited_once_with([QuoteType.BLUE])
    facts, tipos = facts_and_types(render)
    assert tipos == ["blue"]
    assert facts["BLUE_TOTAL"] == "$188.000 ARS"
    assert "OFICIAL_TOTAL" not in facts
    assert "MEP_TOTAL" not in facts


@pytest.mark.asyncio
async def test_invalid_quote_types_fall_back_to_all_three(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000204",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90")]),
            ],
        )

    get_quotes = AsyncMock(return_value=all_three_quotes())
    with patch("app.services.dispatcher.get_usd_quotes", get_quotes):
        result, render = await ask(
            "5491100000204",
            {"intent": "query_net_worth", "quote_types": ["dolar", "USDT"]},
        )

    get_quotes.assert_awaited_once_with([QuoteType.OFICIAL, QuoteType.MEP, QuoteType.BLUE])
    assert result.raw_llm_response["quote_types"] == []
    facts, tipos = facts_and_types(render)
    assert tipos == ["oficial", "mep", "blue"]
    assert {"OFICIAL_TOTAL", "MEP_TOTAL", "BLUE_TOTAL"} <= set(facts)


@pytest.mark.asyncio
async def test_ars_only_skips_quotes(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000205",
            [("Efectivo", "ARS", [("ingreso", "80000")])],
        )

    get_quotes = AsyncMock()
    with patch("app.services.dispatcher.get_usd_quotes", get_quotes):
        result, render = await ask("5491100000205", {"intent": "query_net_worth"})

    get_quotes.assert_not_awaited()
    facts, tipos = facts_and_types(render)
    assert tipos == []
    assert facts == {"ARS_TOTAL": "$80.000", "USD_TOTAL": "sin saldo"}


@pytest.mark.asyncio
async def test_usd_recalculates_with_quotes_off_event_loop(isolated_db, monkeypatch):
    with isolated_db() as session:
        user_id = seed_user(
            session,
            "5491100000206",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90")]),
            ],
        )

    main_thread = threading.get_ident()
    calls: list[tuple[int, tuple]] = []
    real_calculate = net_worth_module.calculate_net_worth

    def spy_calculate_net_worth(*args, **kwargs):
        calls.append((threading.get_ident(), args))
        return real_calculate(*args, **kwargs)

    monkeypatch.setattr(
        "app.services.dispatcher.calculate_net_worth", spy_calculate_net_worth
    )
    quotes = all_three_quotes()
    with patch("app.services.dispatcher.get_usd_quotes", AsyncMock(return_value=quotes)):
        await ask("5491100000206", {"intent": "query_net_worth"})

    assert len(calls) == 2
    assert all(thread_id != main_thread for thread_id, _ in calls)
    assert calls[0][1] == (user_id,)
    assert calls[1][1] == (user_id, None, quotes)


@pytest.mark.asyncio
async def test_no_wallets_replies_with_status_fact(isolated_db):
    with isolated_db() as session:
        user = Usuario(
            nombre="Sin billeteras",
            email="sinbilleteras@example.com",
            whatsapp_id="5491100000207",
        )
        session.add(user)
        session.commit()

    result, render = await ask("5491100000207", {"intent": "query_net_worth"})

    facts, tipos = facts_and_types(render)
    assert facts == {"STATUS": "sin billeteras registradas"}
    assert tipos == []
    assert result.reply_text == RENDERED_REPLY


@pytest.mark.asyncio
async def test_missing_quote_is_reported_not_invented(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000208",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90")]),
            ],
        )

    with patch(
        "app.services.dispatcher.get_usd_quotes",
        AsyncMock(return_value={QuoteType.BLUE: quote(QuoteType.BLUE, "1200", "1300")}),
    ):
        _result, render = await ask("5491100000208", {"intent": "query_net_worth"})

    facts, _tipos = facts_and_types(render)
    assert facts["OFICIAL_TOTAL"] == "total no disponible"
    assert facts["MEP_TOTAL"] == "total no disponible"
    assert facts["BLUE_TOTAL"] == "$188.000 ARS"
    assert_no_metadata(facts)


@pytest.mark.asyncio
async def test_missing_compra_reports_total_unavailable(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000209",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90")]),
            ],
        )

    quotes = all_three_quotes()
    quotes[QuoteType.OFICIAL] = quote(QuoteType.OFICIAL, None, "1200")
    with patch("app.services.dispatcher.get_usd_quotes", AsyncMock(return_value=quotes)):
        _result, render = await ask("5491100000209", {"intent": "query_net_worth"})

    facts, _tipos = facts_and_types(render)
    assert facts["OFICIAL_TOTAL"] == "total no disponible"
    assert not any(key.startswith("OFICIAL_") and key != "OFICIAL_TOTAL" for key in facts)
    assert "1.200" not in " ".join(facts.values())


@pytest.mark.asyncio
async def test_all_compra_missing_reports_totals_unavailable(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000215",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90")]),
            ],
        )

    quotes = {tipo: quote(tipo, None, "1200") for tipo in QuoteType}
    with patch("app.services.dispatcher.get_usd_quotes", AsyncMock(return_value=quotes)):
        _result, render = await ask("5491100000215", {"intent": "query_net_worth"})

    facts, _tipos = facts_and_types(render)
    assert facts["ARS_TOTAL"] == "$80.000"
    assert facts["USD_TOTAL"] == "u$s90,00"
    for tipo in ("OFICIAL", "MEP", "BLUE"):
        assert facts[f"{tipo}_TOTAL"] == "total no disponible"
    assert_no_metadata(facts)
    assert "1.200" not in " ".join(facts.values())


@pytest.mark.asyncio
async def test_stale_quote_is_flagged(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000210",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90")]),
            ],
        )

    quotes = all_three_quotes()
    quotes[QuoteType.BLUE] = quote(QuoteType.BLUE, "1200", "1300", stale=True)
    with patch("app.services.dispatcher.get_usd_quotes", AsyncMock(return_value=quotes)):
        _result, render = await ask("5491100000210", {"intent": "query_net_worth"})

    facts, _tipos = facts_and_types(render)
    assert facts["BLUE_STALE"] == "cotización desactualizada"
    assert "OFICIAL_STALE" not in facts
    assert "MEP_STALE" not in facts
    assert_no_metadata(facts)


@pytest.mark.asyncio
async def test_amounts_are_es_ar_formatted(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000211",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90.5")]),
            ],
        )
        seed_user(
            session,
            "5491100000212",
            [("Efectivo", "ARS", [("egreso", "80000")])],
        )

    quotes = all_three_quotes()
    with patch("app.services.dispatcher.get_usd_quotes", AsyncMock(return_value=quotes)):
        _with_usd, with_usd_render = await ask(
            "5491100000211", {"intent": "query_net_worth"}
        )

    facts, _tipos = facts_and_types(with_usd_render)
    assert facts["ARS_TOTAL"] == "$80.000"
    assert facts["USD_TOTAL"] == "u$s90,50"

    _negative, negative_render = await ask(
        "5491100000212", {"intent": "query_net_worth"}
    )

    negative_facts, _tipos = facts_and_types(negative_render)
    assert negative_facts == {"ARS_TOTAL": "-$80.000", "USD_TOTAL": "sin saldo"}


@pytest.mark.asyncio
async def test_no_quotes_available_does_not_invent_totals(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000213",
            [
                ("Efectivo", "ARS", [("ingreso", "80000")]),
                ("Ahorro", "USD", [("ingreso", "90")]),
            ],
        )

    with patch(
        "app.services.dispatcher.get_usd_quotes",
        AsyncMock(side_effect=QuoteUnavailableError("caído")),
    ):
        _result, render = await ask("5491100000213", {"intent": "query_net_worth"})

    facts, tipos = facts_and_types(render)
    assert facts["ARS_TOTAL"] == "$80.000"
    assert facts["USD_TOTAL"] == "u$s90,00"
    for tipo in ("OFICIAL", "MEP", "BLUE"):
        assert facts[f"{tipo}_TOTAL"] == "total no disponible"
    assert tipos == ["oficial", "mep", "blue"]
    assert_no_metadata(facts)


@pytest.mark.asyncio
async def test_query_net_worth_writes_no_movements(isolated_db):
    with isolated_db() as session:
        seed_user(
            session,
            "5491100000214",
            [("Efectivo", "ARS", [("ingreso", "80000")])],
        )

    result, _render = await ask(
        "5491100000214",
        {"intent": "query_net_worth", "reply_text": "Consultando."},
    )

    assert result.event_key is None
    assert result.raw_llm_response["intent"] == "query_net_worth"
    assert result.raw_llm_response["reply_text"] == "Consultando."
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 1
        assert session.query(Billetera).count() == 1


@pytest.mark.asyncio
async def test_unlinked_account_is_reported(isolated_db):
    result, render = await ask("5499999999999", {"intent": "query_net_worth"})

    assert result.reply_text == "No encontré una cuenta vinculada a este WhatsApp."
    assert result.service_invoked == "net_worth"
    render.assert_not_awaited()
