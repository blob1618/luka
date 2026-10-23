from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.main import app
from app.services.currency import QuoteType, QuoteUnavailableError, UsdQuote

pytestmark = pytest.mark.unit

FETCHED_AT = datetime(2026, 10, 23, 16, 5, tzinfo=timezone.utc)


def make_quote(
    tipo: QuoteType,
    *,
    compra: Decimal | None = Decimal("1500.00"),
    venta: Decimal = Decimal("1550.00"),
    as_of: datetime | None = None,
    stale: bool = False,
) -> UsdQuote:
    return UsdQuote(
        tipo=tipo,
        compra=compra,
        venta=venta,
        fuente="DolarAPI",
        as_of=as_of or datetime(2026, 10, 23, 13, 0, tzinfo=timezone(timedelta(hours=-3))),
        fetched_at=FETCHED_AT,
        stale=stale,
    )


def install_service(monkeypatch, result, calls: list | None = None):
    async def fake_get_usd_quotes(tipos=None, **_kwargs):
        if calls is not None:
            calls.append(tipos)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app_main, "get_usd_quotes", fake_get_usd_quotes)


@pytest.fixture
def client():
    return TestClient(app)


def test_default_returns_the_three_quotes(client, monkeypatch):
    calls: list = []
    quotes = {tipo: make_quote(tipo) for tipo in QuoteType}
    install_service(monkeypatch, quotes, calls)

    response = client.get("/api/cotizaciones")

    assert response.status_code == 200
    assert set(response.json()["quotes"]) == {"oficial", "mep", "blue"}
    assert calls == [list(QuoteType)]


def test_subset_blue_only(client, monkeypatch):
    calls: list = []
    install_service(
        monkeypatch, {QuoteType.BLUE: make_quote(QuoteType.BLUE)}, calls
    )

    response = client.get("/api/cotizaciones", params={"tipos": "blue"})

    assert response.status_code == 200
    assert set(response.json()["quotes"]) == {"blue"}
    assert calls == [[QuoteType.BLUE]]


@pytest.mark.parametrize(
    "tipos",
    ["", ",", "  ,  ", "oficial,foo", "oficial,blue,", "OFICIAL, oficial, blue!"],
)
def test_invalid_tipos_returns_400(client, monkeypatch, tipos):
    calls: list = []
    install_service(monkeypatch, {tipo: make_quote(tipo) for tipo in QuoteType}, calls)

    response = client.get("/api/cotizaciones", params={"tipos": tipos})

    assert response.status_code == 400
    assert response.json() == {"error": "tipo_invalido"}
    assert calls == []


def test_all_unavailable_returns_503(client, monkeypatch):
    install_service(monkeypatch, QuoteUnavailableError("sin cotizaciones"))

    response = client.get("/api/cotizaciones")

    assert response.status_code == 503
    assert response.json() == {"error": "cotizaciones_no_disponibles"}


def test_partial_availability_returns_the_available_subset(client, monkeypatch):
    quotes = {
        QuoteType.OFICIAL: make_quote(QuoteType.OFICIAL),
        QuoteType.BLUE: make_quote(QuoteType.BLUE),
    }
    install_service(monkeypatch, quotes)

    response = client.get("/api/cotizaciones", params={"tipos": "oficial,mep,blue"})

    assert response.status_code == 200
    assert set(response.json()["quotes"]) == {"oficial", "blue"}


def test_stale_quote_is_flagged(client, monkeypatch):
    install_service(monkeypatch, {QuoteType.MEP: make_quote(QuoteType.MEP, stale=True)})

    response = client.get("/api/cotizaciones", params={"tipos": "mep"})

    assert response.status_code == 200
    assert response.json()["quotes"]["mep"]["stale"] is True


def test_serialization_uses_strings_and_aware_iso_offsets(client, monkeypatch):
    naive_as_of = datetime(2026, 10, 23, 16, 0)
    install_service(
        monkeypatch,
        {
            QuoteType.OFICIAL: make_quote(QuoteType.OFICIAL),
            QuoteType.BLUE: make_quote(
                QuoteType.BLUE, compra=None, as_of=naive_as_of
            ),
        },
    )

    body = client.get("/api/cotizaciones", params={"tipos": "oficial,blue"}).json()

    oficial = body["quotes"]["oficial"]
    assert oficial == {
        "compra": "1500.00",
        "venta": "1550.00",
        "fuente": "DolarAPI",
        "as_of": "2026-10-23T13:00:00-03:00",
        "fetched_at": "2026-10-23T16:05:00+00:00",
        "stale": False,
    }
    assert isinstance(oficial["compra"], str)
    assert isinstance(oficial["venta"], str)
    assert datetime.fromisoformat(oficial["as_of"]).utcoffset() is not None
    assert datetime.fromisoformat(oficial["fetched_at"]).utcoffset() is not None

    blue = body["quotes"]["blue"]
    assert blue["compra"] is None
    assert blue["venta"] == "1550.00"
    # naive as_of se asume UTC, igual que _parse_datetime en el servicio de cotizaciones.
    assert blue["as_of"] == "2026-10-23T16:00:00+00:00"
    assert datetime.fromisoformat(blue["as_of"]).utcoffset() is not None
