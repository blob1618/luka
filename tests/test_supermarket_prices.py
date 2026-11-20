"""Contractual SEPA fixture: no provider network, Redis or production database."""
from datetime import date, timedelta
from decimal import Decimal
import io
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.models.database import SepaPrice, SepaSnapshot, MovimientoFinanciero
from app.services import sepa_ingest, supermarket_prices
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_pilot import NOW

accounts = scenarios.accounts
ingress = scenarios.ingress


def _csv(headers, rows):
    return ("|".join(headers) + "\n" + "\n".join("|".join(str(row.get(key, "")) for key in headers)
        for row in rows) + "\n").encode()


def _archive():
    commerce = [{"id_comercio": "1", "id_bandera": "1", "comercio_bandera_nombre": "Coto",
                 "comercio_ultima_actualizacion": "2026-11-19T12:00:00-03:00"},
                {"id_comercio": "2", "id_bandera": "1", "comercio_bandera_nombre": "Vea",
                 "comercio_ultima_actualizacion": "2026-11-19T12:00:00-03:00"}]
    branches = [{"id_comercio": str(i), "id_bandera": "1", "id_sucursal": "1",
                 "sucursales_nombre": name, "sucursales_localidad": "Resistencia",
                 "sucursales_provincia": "AR-H", "sucursales_latitud": "-27.45",
                 "sucursales_longitud": "-58.98"} for i, name in [(1, "Centro"), (2, "Norte")]]
    products = [
        {"id_comercio": "1", "id_bandera": "1", "id_sucursal": "1", "id_producto": "7790895000997",
         "productos_ean": "1", "productos_descripcion": "Coca Cola 2,25 L", "productos_marca": "Coca Cola",
         "productos_cantidad_presentacion": "2.25", "productos_unidad_medida_presentacion": "l",
         "productos_precio_lista": "6100", "productos_precio_unitario_promo1": "5900",
         "productos_leyenda_promo1": "Descuento. Desde el 19/11/2026 Hasta el 20/11/2026"},
        {"id_comercio": "2", "id_bandera": "1", "id_sucursal": "1", "id_producto": "7790895000997",
         "productos_ean": "1", "productos_descripcion": "Coca Cola 2,25 L", "productos_marca": "Coca Cola",
         "productos_cantidad_presentacion": "2.25", "productos_unidad_medida_presentacion": "l",
         "productos_precio_lista": "6250", "productos_precio_unitario_promo1": "5000",
         "productos_leyenda_promo1": "Descuento. Desde el 01/11/2026 Hasta el 02/11/2026"},
        {"id_comercio": "1", "id_bandera": "1", "id_sucursal": "1", "id_producto": "7790139101169",
         "productos_ean": "1", "productos_descripcion": "Vinagre Favinco 500 ml", "productos_marca": "Favinco",
         "productos_cantidad_presentacion": "500", "productos_unidad_medida_presentacion": "ml",
         "productos_precio_lista": "2000"},
    ]
    data = io.BytesIO()
    with ZipFile(data, "w", ZIP_DEFLATED) as outer:
        for merchant in ("1", "2"):
            nested = io.BytesIO()
            with ZipFile(nested, "w", ZIP_DEFLATED) as inner:
                inner.writestr("comercio.csv", _csv(list(commerce[0]), [r for r in commerce if r["id_comercio"] == merchant]))
                inner.writestr("sucursales.csv", _csv(list(branches[0]), [r for r in branches if r["id_comercio"] == merchant]))
                inner.writestr("productos.csv", _csv(list(products[0]), [r for r in products if r["id_comercio"] == merchant]))
            outer.writestr(merchant + ".zip", nested.getvalue())
    data.seek(0)
    return data


@pytest.fixture
def sepa_db(isolated_db, monkeypatch):
    monkeypatch.setattr(sepa_ingest, "SessionLocal", isolated_db)
    monkeypatch.setattr(supermarket_prices, "SessionLocal", isolated_db)
    with isolated_db() as session:
        snapshot = SepaSnapshot(data_date=date(2026, 11, 19), source_url="https://example.test/sepa.zip")
        session.add(snapshot)
        session.commit()
        count, rejected = sepa_ingest._ingest_archive(session, _archive(), snapshot, {"AR-H"})
        snapshot.status = "ready"
        snapshot.row_count = count
        session.commit()
        assert (count, rejected) == (3, 0)
    return isolated_db


@pytest.mark.integration
@pytest.mark.asyncio
async def test_product_and_basket_prices_use_real_csv_contract_and_cache(sepa_db, fake_redis, monkeypatch):
    async def cache():
        return fake_redis
    monkeypatch.setattr(supermarket_prices.assistant_state, "client", cache)
    result = await supermarket_prices.compare_prices(["7790895000997", "7790139101169"],
        locality="Resistencia", province="AR-H", today=date(2026, 11, 19))
    assert result["status"] == "ok"
    assert result["items"][0]["options"][0]["price"] == "5900.00"
    assert result["items"][0]["options"][0]["saving_vs_highest"] == "350.00"
    assert result["items"][0]["options"][1]["promotion"] is None
    assert result["items"][1]["status"] == "ok"
    def should_not_query(*args):
        raise AssertionError("Cache hit must avoid SQL lookup")
    monkeypatch.setattr(supermarket_prices, "_lookup_sync", should_not_query)
    await supermarket_prices.compare_prices(["7790895000997", "7790139101169"],
        locality="Resistencia", province="AR-H", today=date(2026, 11, 19))
    assert fake_redis.get_calls == 2
    assert len(fake_redis._storage) == 1
    with sepa_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_promotion_and_missing_product_do_not_become_offers(sepa_db):
    result = await supermarket_prices.compare_prices(["7790895000997", "9999999999999"],
        locality="Resistencia", today=date(2026, 11, 21))
    assert result["status"] == "ok"
    assert result["items"][0]["options"][0]["promotion"] is None
    assert result["items"][1]["status"] == "missing"
    stale = await supermarket_prices.compare_prices(["7790895000997"], locality="Resistencia",
        today=date(2026, 11, 22))
    assert stale["status"] == "unavailable"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_name_ambiguity_requires_ean_and_does_not_invent_a_comparison(sepa_db):
    with sepa_db() as session:
        snapshot = session.query(SepaSnapshot).one()
        session.add(SepaPrice(snapshot_id=snapshot.id, commerce_id="1", flag_id="1", branch_id="1",
            product_id="7790895000998", report_date=date(2026, 11, 19), is_ean=True,
            description="Coca Cola 1,5 L", brand="Coca Cola", quantity=Decimal("1.5"), unit="l",
            chain="Coto", branch="Centro", province="AR-H", locality="Resistencia",
            list_price=Decimal("4900")))
        session.commit()
    result = await supermarket_prices.compare_prices(["Coca Cola"], locality="Resistencia",
        today=date(2026, 11, 19))
    assert result["items"][0]["status"] == "ambiguous"
    assert len(result["items"][0]["options"]) == 2
    assert "Indicá el EAN" in supermarket_prices.present_comparison(result, locality="Resistencia")[1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_letter_name_returns_missing_instead_of_failing(sepa_db):
    result = await supermarket_prices.compare_prices(["x"], locality="Resistencia",
        today=date(2026, 11, 19))
    assert result["items"][0] == {"status": "missing", "query": "x"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_whatsapp_tool_compares_list_and_never_registers_a_movement(ingress, accounts):
    day = (NOW - timedelta(hours=3)).date()
    with accounts[0]() as session:
        snapshot = SepaSnapshot(data_date=day, source_url="https://example.test/sepa.zip", status="ready")
        session.add(snapshot)
        session.flush()
        session.add(SepaPrice(snapshot_id=snapshot.id, commerce_id="1", flag_id="1", branch_id="1",
            product_id="7790895000997", report_date=day, is_ean=True, description="Coca Cola 2,25 L",
            quantity=Decimal("2.25"), unit="l", chain="Coto", branch="Centro", province="AR-H",
            locality="Resistencia", list_price=Decimal("6100")))
        session.commit()
        before = session.query(MovimientoFinanciero).count()
    result = await ingress.tool("compare_supermarket_prices", {
        "products": ["7790895000997", "9999999999999"], "locality": "Resistencia",
        "province": "AR-H"}, "¿Cuánto cuesta Coca Cola en Resistencia?")
    assert "Coto" in result.reply_text and "$6.100,00" in result.reply_text
    assert "9999999999999: sin precio" in result.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count() == before
