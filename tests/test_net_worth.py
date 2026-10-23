"""Unit tests for the net worth aggregation (STK-45 / STK-217)."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.models.database import Billetera, MovimientoFinanciero, Usuario
from app.services.currency import QuoteType, UsdQuote
from app.services.net_worth import NetWorthStatus, calculate_net_worth

pytestmark = pytest.mark.unit

FETCHED_AT = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def quote(tipo: QuoteType, compra: str | None, *, stale: bool = False) -> UsdQuote:
    return UsdQuote(
        tipo=tipo,
        compra=Decimal(compra) if compra is not None else None,
        venta=Decimal("1"),
        fuente="test",
        as_of=FETCHED_AT,
        fetched_at=FETCHED_AT,
        stale=stale,
    )


def seed_user(session, phone: str, wallets: list[tuple[str, str, list[tuple[str, str]]]]):
    """Create a user with wallets and their (tipo, amount) movements."""
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


def test_no_wallets_returns_no_wallets_status(isolated_db):
    with isolated_db() as session:
        user = Usuario(nombre="Sin billeteras", email="sin@example.com", whatsapp_id="5491100000101")
        session.add(user)
        session.commit()
        user_id = user.id

        result = calculate_net_worth(user_id, session=session, quotes={})

    assert result.status is NetWorthStatus.NO_WALLETS
    assert result.ars_total == Decimal("0")
    assert result.usd_total == Decimal("0")
    assert all(total == 0 for total in result.totals.values())
    assert result.quotes == {}
    assert result.missing == ()
    assert result.stale == ()


def test_ars_only_does_not_need_quotes(isolated_db):
    with isolated_db() as session:
        user_id = seed_user(
            session,
            "5491100000102",
            [("Efectivo", "ARS", [("ingreso", "15000"), ("egreso", "5000")])],
        )

        result = calculate_net_worth(user_id, session=session)

    assert result.status is NetWorthStatus.OK
    assert result.ars_total == Decimal("10000")
    assert result.usd_total == Decimal("0")
    assert result.totals == dict.fromkeys(QuoteType, Decimal("10000"))
    assert result.quotes == {}
    assert result.missing == ()
    assert result.stale == ()


def test_ars_plus_usd_with_three_quotes_computes_exact_totals(isolated_db):
    quotes = {
        QuoteType.OFICIAL: quote(QuoteType.OFICIAL, "1000"),
        QuoteType.MEP: quote(QuoteType.MEP, "1100"),
        QuoteType.BLUE: quote(QuoteType.BLUE, "1200"),
    }
    with isolated_db() as session:
        user_id = seed_user(
            session,
            "5491100000103",
            [
                ("Efectivo", "ARS", [("ingreso", "15000"), ("egreso", "5000")]),
                ("Ahorro", "USD", [("ingreso", "500")]),
            ],
        )

        result = calculate_net_worth(user_id, session=session, quotes=quotes)

    assert result.status is NetWorthStatus.OK
    assert result.ars_total == Decimal("10000")
    assert result.usd_total == Decimal("500")
    assert result.totals == {
        QuoteType.OFICIAL: Decimal("510000"),
        QuoteType.MEP: Decimal("560000"),
        QuoteType.BLUE: Decimal("610000"),
    }
    assert all(isinstance(total, Decimal) for total in result.totals.values())
    assert result.quotes == quotes
    assert result.missing == ()
    assert result.stale == ()


def test_usd_without_quotes_is_unavailable_and_missing(isolated_db):
    with isolated_db() as session:
        user_id = seed_user(
            session,
            "5491100000104",
            [("Ahorro", "USD", [("ingreso", "500")])],
        )

        result = calculate_net_worth(user_id, session=session, quotes={})

    assert result.status is NetWorthStatus.QUOTE_UNAVAILABLE
    assert result.ars_total == Decimal("0")
    assert result.usd_total == Decimal("500")
    assert result.totals == dict.fromkeys(QuoteType, None)
    assert result.missing == (QuoteType.OFICIAL, QuoteType.MEP, QuoteType.BLUE)
    assert result.stale == ()


def test_missing_compra_yields_none_total(isolated_db):
    quotes = {QuoteType.MEP: quote(QuoteType.MEP, None)}
    with isolated_db() as session:
        user_id = seed_user(
            session,
            "5491100000105",
            [
                ("Efectivo", "ARS", [("ingreso", "1000")]),
                ("Ahorro", "USD", [("ingreso", "100")]),
            ],
        )

        result = calculate_net_worth(user_id, session=session, quotes=quotes)

    assert result.status is NetWorthStatus.QUOTE_UNAVAILABLE
    assert result.totals[QuoteType.MEP] is None
    assert result.totals[QuoteType.OFICIAL] is None
    assert result.totals[QuoteType.BLUE] is None
    assert result.missing == (QuoteType.OFICIAL, QuoteType.MEP, QuoteType.BLUE)
    assert result.stale == ()


def test_stale_quote_still_computes_total_and_is_reported(isolated_db):
    quotes = {
        QuoteType.OFICIAL: quote(QuoteType.OFICIAL, "1000"),
        QuoteType.MEP: quote(QuoteType.MEP, "1100"),
        QuoteType.BLUE: quote(QuoteType.BLUE, "1200", stale=True),
    }
    with isolated_db() as session:
        user_id = seed_user(
            session,
            "5491100000106",
            [
                ("Efectivo", "ARS", [("ingreso", "10000")]),
                ("Ahorro", "USD", [("ingreso", "200")]),
            ],
        )

        result = calculate_net_worth(user_id, session=session, quotes=quotes)

    assert result.status is NetWorthStatus.OK
    assert result.totals[QuoteType.BLUE] == Decimal("250000")
    assert result.stale == (QuoteType.BLUE,)
    assert result.missing == ()


def test_unknown_currency_is_ignored(isolated_db):
    quotes = {QuoteType.OFICIAL: quote(QuoteType.OFICIAL, "1000")}
    with isolated_db() as session:
        user_id = seed_user(
            session,
            "5491100000107",
            [
                ("Efectivo", "ARS", [("ingreso", "10000")]),
                ("Viajes", "EUR", [("ingreso", "999999")]),
            ],
        )

        result = calculate_net_worth(user_id, session=session, quotes=quotes)

    assert result.status is NetWorthStatus.OK
    assert result.ars_total == Decimal("10000")
    assert result.usd_total == Decimal("0")
    assert result.totals == dict.fromkeys(QuoteType, Decimal("10000"))
    assert result.missing == ()


def test_lowercase_usd_is_normalized(isolated_db):
    quotes = {QuoteType.OFICIAL: quote(QuoteType.OFICIAL, "1000")}
    with isolated_db() as session:
        user_id = seed_user(
            session,
            "5491100000108",
            [("Ahorro", "usd", [("ingreso", "300")])],
        )

        result = calculate_net_worth(user_id, session=session, quotes=quotes)

    assert result.usd_total == Decimal("300")
    assert result.totals[QuoteType.OFICIAL] == Decimal("300000")
    assert QuoteType.OFICIAL not in result.missing


def test_net_worth_is_isolated_per_user(isolated_db):
    with isolated_db() as session:
        user_a = seed_user(
            session,
            "5491100000109",
            [("Efectivo", "ARS", [("ingreso", "1000")])],
        )
        seed_user(
            session,
            "5491100000110",
            [("Efectivo", "ARS", [("ingreso", "999999")])],
        )

        result = calculate_net_worth(user_a, session=session)

    assert result.ars_total == Decimal("1000")
    assert result.totals == dict.fromkeys(QuoteType, Decimal("1000"))


def test_calculate_net_worth_does_not_write_to_session(isolated_db):
    quotes = {QuoteType.OFICIAL: quote(QuoteType.OFICIAL, "1000")}
    with isolated_db() as session:
        user_id = seed_user(
            session,
            "5491100000111",
            [("Efectivo", "ARS", [("ingreso", "1000")])],
        )
        session.add = Mock()
        session.commit = Mock()

        result = calculate_net_worth(user_id, session=session, quotes=quotes)

    assert result.status is NetWorthStatus.OK
    session.add.assert_not_called()
    session.commit.assert_not_called()
