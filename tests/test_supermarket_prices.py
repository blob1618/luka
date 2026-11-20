"""Contractual SEPA fixture: no provider network, Redis or production database."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import httpx

from app.models.database import SepaPrice, SepaSnapshot, MovimientoFinanciero, _connect_args
from app.services import sepa_ingest, supermarket_prices
from app import scheduler as scheduler_module
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_pilot import NOW

accounts = scenarios.accounts
ingress = scenarios.ingress


@pytest.mark.unit
def test_postgres_connection_disables_prepared_statements_for_transaction_pooler():
    assert _connect_args("postgresql+psycopg://example.test/db") == {"prepare_threshold": None}
    assert _connect_args("sqlite:///local.db") == {"check_same_thread": False}


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
        outer.writestr("empty_merchant.zip", b"")
        for merchant in ("1", "2"):
            nested = io.BytesIO()
            with ZipFile(nested, "w", ZIP_DEFLATED) as inner:
                inner.writestr("comercio.csv", _csv(list(commerce[0]), [r for r in commerce if r["id_comercio"] == merchant]))
                inner.writestr("sucursales.csv", _csv(list(branches[0]), [r for r in branches if r["id_comercio"] == merchant]) + b"1|1\n")
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


@pytest.mark.unit
def test_empty_catalog_bootstraps_once_and_ready_catalog_is_left_alone(isolated_db, monkeypatch):
    monkeypatch.setattr(sepa_ingest, "SessionLocal", isolated_db)
    calls = []
    monkeypatch.setattr(sepa_ingest, "ingest_latest", lambda: calls.append("load") or "loaded")
    assert sepa_ingest.ingest_if_missing() == "loaded"
    with isolated_db() as session:
        session.add(SepaSnapshot(data_date=date(2026, 11, 19),
            source_url="https://example.test/sepa.zip", status="ready"))
        session.commit()
    assert sepa_ingest.ingest_if_missing() is None
    assert calls == ["load"]


@pytest.mark.unit
def test_catalog_403_uses_freshest_official_weekly_zip_without_downloading(monkeypatch):
    monkeypatch.setattr(sepa_ingest, "WEEKLY_RESOURCES", (("thu", "jueves"), ("wed", "miercoles")))
    monkeypatch.setattr(sepa_ingest, "RESOURCE_BASE", "https://datos.produccion.gob.ar/resource")
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 20, tzinfo=tz)

    monkeypatch.setattr(sepa_ingest, "datetime", FrozenDateTime)
    requests = []

    def answer(request):
        requests.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(403)
        stamp = "Thu, 19 Nov 2026 16:19:56 GMT" if "jueves" in str(request.url) else "Wed, 18 Nov 2026 16:19:56 GMT"
        return httpx.Response(200, headers={"Last-Modified": stamp})

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        url, data_date, checksum = sepa_ingest._source_resource(client)
    assert url.endswith("/thu/download/sepa_jueves.zip")
    assert data_date == date(2026, 11, 19)
    assert checksum is None
    assert [method for method, _ in requests] == ["GET", "HEAD", "HEAD"]


@pytest.mark.unit
def test_official_403_uses_dated_mirror_and_checks_archive(monkeypatch):
    import hashlib

    monkeypatch.setattr(sepa_ingest, "WEEKLY_RESOURCES", (("thu", "jueves"),))
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 20, tzinfo=tz)

    monkeypatch.setattr(sepa_ingest, "datetime", FrozenDateTime)
    archive = _archive().getvalue()
    digest = hashlib.sha256(archive).hexdigest()
    requests = []

    def answer(request):
        requests.append((request.method, str(request.url)))
        if str(request.url).endswith("sepa-latest.json"):
            return httpx.Response(200, json={"data_date": "2026-11-19",
                "source_url": "https://datos.produccion.gob.ar/source.zip",
                "sha256": digest, "size": len(archive)})
        if str(request.url).endswith("sepa-latest.zip"):
            return httpx.Response(200, content=archive)
        return httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        url, data_date, checksum = sepa_ingest._source_resource(client)
        with sepa_ingest._download(client, url, checksum) as source:
            assert source.read() == archive
        with pytest.raises(ValueError, match="no coincide"):
            sepa_ingest._download(client, url, "0" * 64)
    assert data_date == date(2026, 11, 19)
    assert requests[0][0] == "GET"
    assert requests[1][0] == "HEAD"
    assert requests[2][1].endswith("sepa-latest.json")


@pytest.mark.unit
def test_mirror_rejects_stale_manifest(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 20, tzinfo=tz)

    monkeypatch.setattr(sepa_ingest, "datetime", FrozenDateTime)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "data_date": "2026-11-12", "source_url": "https://datos.produccion.gob.ar/source.zip",
            "sha256": "a" * 64, "size": 123}))) as client:
        with pytest.raises(ValueError, match="demasiado antiguo"):
            sepa_ingest._source_from_mirror(client)


@pytest.mark.unit
def test_mirror_publisher_records_official_date_and_digest(tmp_path, monkeypatch):
    import hashlib
    import json
    from scripts.publish_sepa_mirror import prepare

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 20, tzinfo=tz)

    monkeypatch.setattr(sepa_ingest, "datetime", FrozenDateTime)
    archive = _archive().getvalue()
    source_url = "https://datos.produccion.gob.ar/daily.zip"

    def answer(request):
        if str(request.url) == source_url:
            return httpx.Response(200, content=archive)
        return httpx.Response(200, json={"result": {"resources": [{
            "url": source_url, "last_modified": "2026-11-19T16:19:56Z",
            "size": len(archive)}]}})

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        manifest = prepare(tmp_path, client=client)
    compact = (tmp_path / "sepa-latest.zip").read_bytes()
    assert manifest == {"data_date": "2026-11-19", "source_url": source_url,
        "sha256": hashlib.sha256(compact).hexdigest(), "size": len(compact),
        "coverage": ["AR-H"]}
    with ZipFile(io.BytesIO(compact)) as result:
        assert len(result.namelist()) == 2
    assert json.loads((tmp_path / "sepa-latest.json").read_text()) == manifest


@pytest.mark.unit
def test_archive_fallback_repackages_only_requested_province(tmp_path, monkeypatch):
    import hashlib
    import json
    import tarfile
    import zstandard
    from scripts import publish_sepa_mirror

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 20, tzinfo=tz)

    monkeypatch.setattr(publish_sepa_mirror, "datetime", FrozenDateTime)
    resource_id = "d076720f-a7f0-4af8-b1d6-1b99d5a90c14"
    revision_id = "f53812da-ec57-4d8b-8eda-3e75e3a204ff"
    official_url = "https://datos.produccion.gob.ar/source.zip"
    archive_url = publish_sepa_mirror.ARCHIVE_PREFIX + "test.tar.zst"
    metadata = {"result": {"resources": [{"id": resource_id,
        "revision_id": revision_id, "last_modified": "2026-11-19T16:19:56",
        "url": official_url}]}}
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as tar:
        body = json.dumps(metadata).encode()
        info = tarfile.TarInfo("./dataset-info.json")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
        with ZipFile(_archive()) as original:
            for name in original.namelist():
                if not name.endswith(".zip") or not original.read(name):
                    continue
                with ZipFile(io.BytesIO(original.read(name))) as merchant:
                    for csv_name in ("productos.csv", "sucursales.csv", "comercio.csv"):
                        body = merchant.read(csv_name)
                        info = tarfile.TarInfo(f"./2026-11-19/{name.rsplit('/', 1)[-1][:-4]}/{csv_name}")
                        info.size = len(body)
                        tar.addfile(info, io.BytesIO(body))
    compressed = zstandard.ZstdCompressor().compress(tar_bytes.getvalue())

    def answer(request):
        url = str(request.url)
        if url == publish_sepa_mirror.ARCHIVE_INDEX:
            return httpx.Response(200, json={"2026-11-19": [{
                "id": f"{resource_id}-revID-{revision_id}", "link": archive_url}]})
        if url == archive_url:
            return httpx.Response(200, content=compressed)
        return httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        manifest = publish_sepa_mirror.prepare(tmp_path, client=client)
    compact = (tmp_path / "sepa-latest.zip").read_bytes()
    assert manifest["source_url"] == official_url
    assert manifest["archive_url"] == archive_url
    assert manifest["sha256"] == hashlib.sha256(compact).hexdigest()
    with ZipFile(io.BytesIO(compact)) as output:
        assert len(output.namelist()) == 2


@pytest.mark.unit
def test_weekly_zip_fallback_rejects_stale_archive(monkeypatch):
    monkeypatch.setattr(sepa_ingest, "WEEKLY_RESOURCES", (("old", "jueves"),))
    monkeypatch.setattr(sepa_ingest, "RESOURCE_BASE", "https://datos.produccion.gob.ar/resource")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 20, tzinfo=tz)

    monkeypatch.setattr(sepa_ingest, "datetime", FrozenDateTime)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"Last-Modified": "Thu, 12 Nov 2026 16:19:56 GMT"}))) as client:
        with pytest.raises(ValueError, match="demasiado antiguo"):
            sepa_ingest._source_from_published_resources(client)


@pytest.mark.unit
def test_enabled_ingest_schedules_bootstrap_and_daily_run(monkeypatch):
    class RecordingScheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, *args, **kwargs):
            self.jobs.append((args, kwargs))

        def start(self):
            pass

    fake = RecordingScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", fake)
    monkeypatch.setenv("SEPA_INGEST_ENABLED", "1")
    monkeypatch.delenv("RUN_RECURRING_DETECTION_ON_STARTUP", raising=False)
    before = datetime.now(timezone.utc)
    scheduler_module.start_scheduler()
    after = datetime.now(timezone.utc)
    bootstrap = next(kwargs for (args, kwargs) in fake.jobs if args[0] is scheduler_module.run_sepa_bootstrap)
    daily = next(kwargs for (args, kwargs) in fake.jobs if args[0] is scheduler_module.run_sepa_ingest)
    assert before + timedelta(minutes=1) <= bootstrap["run_date"] <= after + timedelta(minutes=1)
    assert bootstrap["id"] == "sepa_bootstrap"
    assert bootstrap["misfire_grace_time"] == 300
    assert daily["id"] == "sepa_daily_ingest"
    assert daily["misfire_grace_time"] == 300
    assert (daily["hour"], daily["minute"]) == (17, 30)


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
async def test_name_ambiguity_asks_for_presentation_and_does_not_invent_a_comparison(sepa_db):
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
    assert "Decime la marca o presentación" in supermarket_prices.present_comparison(result, locality="Resistencia")[1]
    assert "EAN" not in supermarket_prices.present_comparison(result, locality="Resistencia")[1]


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
