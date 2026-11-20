"""Bounded daily SEPA download and atomic snapshot publication."""
import csv
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import io
import logging
import os
import re
from tempfile import SpooledTemporaryFile, TemporaryFile
from zipfile import ZipFile

import httpx
from sqlalchemy import delete, insert

from app.models.database import SepaPrice, SepaSnapshot, SessionLocal

logger = logging.getLogger(__name__)
CATALOG_URL = "https://datos.produccion.gob.ar/api/3/action/package_show?id=sepa-precios"
SEPA_USER_AGENT = "Mozilla/5.0 LukaSEPA/1.0 (+https://github.com/blob1618/luka)"
WEEKLY_RESOURCES = (
    ("91bc072a-4726-44a1-85ec-4a8467aad27e", "viernes"),
    ("d076720f-a7f0-4af8-b1d6-1b99d5a90c14", "jueves"),
    ("1e92cd42-4f94-4071-a165-62c4cb2ce23c", "miercoles"),
    ("9dc06241-cc83-44f4-8e25-c9b1636b8bc8", "martes"),
    ("0a9069a9-06e8-4f98-874d-da5578693290", "lunes"),
    ("f8e75128-515a-436e-bf8d-5c63a62f2005", "domingo"),
    ("b3c3da5d-213d-41e7-8d74-f23fda0a3c30", "sabado"),
)
RESOURCE_BASE = "https://datos.produccion.gob.ar/dataset/6f47ec76-d1ce-4e34-a7e1-621fe9b1d0b5/resource"
MIRROR_BASE = "https://github.com/blob1618/luka/releases/download/sepa-feed"
MAX_DOWNLOAD = 550_000_000
MAX_INNER_FILE = 1_500_000_000
BATCH_SIZE = 2000


def _cell(row, key):
    return (row.get(key) or "").strip()


def _decimal(value):
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError):
        return None
    return result if result.is_finite() and result > 0 else None


def _csv(archive, name):
    member = next((item for item in archive.infolist()
                   if item.filename.lower().endswith("/" + name) or item.filename.lower() == name), None)
    if member is None or member.file_size > MAX_INNER_FILE:
        raise ValueError(f"Archivo SEPA ausente o excesivo: {name}")
    with archive.open(member) as stream:
        with io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace", newline="") as text:
            yield from csv.DictReader(text, delimiter="|")


def _source_resource(client, *, allow_mirror=True):
    response = client.get(CATALOG_URL)
    if response.status_code == 403:
        logger.warning("sepa_catalog_forbidden_using_published_resources")
        try:
            url, published = _source_from_published_resources(client)
            return url, published, None
        except ValueError:
            if not allow_mirror:
                raise
            logger.warning("sepa_official_resources_unavailable_using_mirror")
            return _source_from_mirror(client)
    response.raise_for_status()
    resources = response.json()["result"]["resources"]
    candidates = [item for item in resources if item.get("url", "").startswith(
        "https://datos.produccion.gob.ar/") and item.get("url", "").endswith(".zip")]
    if not candidates:
        raise ValueError("SEPA no publicó un ZIP minorista")
    item = max(candidates, key=lambda value: value.get("last_modified") or "")
    published = datetime.fromisoformat(item["last_modified"].replace("Z", "+00:00"))
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - published).days > 3:
        raise ValueError("El archivo SEPA más reciente es demasiado antiguo")
    if int(item.get("size") or 0) > MAX_DOWNLOAD:
        raise ValueError("El archivo SEPA excede el límite de descarga")
    return item["url"], published.date(), None


def _source_from_published_resources(client):
    """Use the seven official weekly ZIP resources when the CKAN API rejects us."""
    candidates = []
    for resource_id, weekday in WEEKLY_RESOURCES:
        url = f"{RESOURCE_BASE}/{resource_id}/download/sepa_{weekday}.zip"
        try:
            response = client.head(url, timeout=10)
            response.raise_for_status()
            modified = parsedate_to_datetime(response.headers["last-modified"])
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            candidates.append((modified, url))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning("sepa_weekly_resource_unavailable url=%s error=%s", url, exc)
    if not candidates:
        raise ValueError("No se pudo consultar ningún ZIP semanal oficial de SEPA")
    published, url = max(candidates)
    if (datetime.now(timezone.utc) - published).days > 3:
        raise ValueError("El ZIP semanal SEPA más reciente es demasiado antiguo")
    return url, published.date()


def _source_from_mirror(client):
    """A dated, checksummed copy of the official ZIP published by our CI job."""
    response = client.get(f"{MIRROR_BASE}/sepa-latest.json")
    response.raise_for_status()
    manifest = response.json()
    if not isinstance(manifest, dict):
        raise ValueError("Manifiesto espejo SEPA inválido")
    try:
        published = datetime.fromisoformat(manifest["data_date"]).date()
        source_url = manifest["source_url"]
        digest = manifest["sha256"]
        size = manifest["size"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Manifiesto espejo SEPA incompleto") from exc
    if not (source_url.startswith("https://datos.produccion.gob.ar/")
            and source_url.endswith(".zip") and isinstance(digest, str)
            and re.fullmatch(r"[a-f0-9]{64}", digest)
            and isinstance(size, int) and 0 < size <= MAX_DOWNLOAD):
        raise ValueError("Manifiesto espejo SEPA inválido")
    age = (datetime.now(timezone.utc).date() - published).days
    if not 0 <= age <= 3:
        raise ValueError("El ZIP espejo SEPA es demasiado antiguo")
    logger.info("sepa_mirror_source date=%s official_url=%s", published, source_url)
    return f"{MIRROR_BASE}/sepa-latest.zip", published, digest


def _download(client, url, expected_sha256=None):
    file = TemporaryFile()
    size = 0
    digest = hashlib.sha256() if expected_sha256 else None
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(1_048_576):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise ValueError("El archivo SEPA excede el límite de descarga")
                file.write(chunk)
                if digest:
                    digest.update(chunk)
        if digest and digest.hexdigest() != expected_sha256:
            raise ValueError("El ZIP espejo SEPA no coincide con su manifiesto")
        file.seek(0)
        return file
    except Exception:
        file.close()
        raise


def _price(row, branches, chains, snapshot_id):
    commerce, flag, branch = (_cell(row, key) for key in ("id_comercio", "id_bandera", "id_sucursal"))
    product = _cell(row, "id_producto")
    store = branches.get((commerce, flag, branch))
    chain = chains.get((commerce, flag))
    amount = _decimal(row.get("productos_precio_lista"))
    description = " ".join(_cell(row, "productos_descripcion").split())
    branch_name = " ".join(_cell(store or {}, "sucursales_nombre").split())
    if not (commerce and flag and branch and product and store and chain and amount and description and branch_name):
        return None
    quantity = _decimal(row.get("productos_cantidad_presentacion"))
    def promo(number):
        price = _decimal(row.get(f"productos_precio_unitario_promo{number}"))
        legend = _cell(row, f"productos_leyenda_promo{number}")
        return (price, legend) if price and price < amount and legend else (None, None)
    promo1, text1 = promo(1)
    promo2, text2 = promo(2)
    def coord(value):
        try:
            return Decimal(value) if value else None
        except InvalidOperation:
            return None
    return dict(snapshot_id=snapshot_id, commerce_id=commerce, flag_id=flag, branch_id=branch,
        product_id=product, report_date=chain[1], is_ean=_cell(row, "productos_ean") == "1",
        description=description[:500], brand=_cell(row, "productos_marca")[:100],
        quantity=quantity, unit=_cell(row, "productos_unidad_medida_presentacion")[:30],
        chain=" ".join(chain[0].split())[:120], branch=branch_name[:120],
        province=_cell(store, "sucursales_provincia")[:80], locality=_cell(store, "sucursales_localidad")[:120],
        latitude=coord(store.get("sucursales_latitud")), longitude=coord(store.get("sucursales_longitud")),
        list_price=amount, promo1_price=promo1, promo1_text=text1, promo2_price=promo2, promo2_text=text2)


def _ingest_archive(session, source, snapshot, provinces):
    count = rejected = 0
    with ZipFile(source) as outer:
        for item in outer.infolist():
            if not item.filename.lower().endswith(".zip"):
                continue
            if item.file_size == 0:
                logger.warning("sepa_ingest_skipped_empty_archive name=%s", item.filename)
                continue
            if item.file_size > MAX_INNER_FILE:
                raise ValueError("Comercio SEPA excesivo")
            with SpooledTemporaryFile(max_size=8_000_000) as inner_file:
                with outer.open(item) as stream:
                    while chunk := stream.read(1_048_576):
                        inner_file.write(chunk)
                inner_file.seek(0)
                with ZipFile(inner_file) as inner:
                    chains = {}
                    for row in _csv(inner, "comercio.csv"):
                        try:
                            report_date = datetime.fromisoformat(_cell(row, "comercio_ultima_actualizacion")).date()
                        except (ValueError, TypeError, KeyError):
                            continue
                        if not 0 <= (snapshot.data_date - report_date).days <= 2:
                            continue
                        chains[(_cell(row, "id_comercio"), _cell(row, "id_bandera"))] = (
                            _cell(row, "comercio_bandera_nombre"), report_date)
                    branches = {(_cell(r, "id_comercio"), _cell(r, "id_bandera"), _cell(r, "id_sucursal")): r
                        for r in _csv(inner, "sucursales.csv")}
                    batch = []
                    for row in _csv(inner, "productos.csv"):
                        product_id = _cell(row, "id_producto")
                        if _cell(row, "productos_ean") != "1" or not re.fullmatch(
                                r"\d{8}|\d{12}|\d{13}", product_id):
                            continue
                        store = branches.get((_cell(row, "id_comercio"),
                            _cell(row, "id_bandera"), _cell(row, "id_sucursal")))
                        if store and _cell(store, "sucursales_provincia").upper() not in provinces:
                            continue
                        value = _price(row, branches, chains, snapshot.id)
                        if value is None:
                            rejected += 1
                            continue
                        batch.append(value)
                        if len(batch) == BATCH_SIZE:
                            session.execute(insert(SepaPrice), batch)
                            session.commit()
                            count += len(batch)
                            batch = []
                    if batch:
                        session.execute(insert(SepaPrice), batch)
                        session.commit()
                        count += len(batch)
    return count, rejected


def ingest_latest(*, client=None):
    """Loads an unpublished snapshot; readers see only a completed snapshot."""
    provinces = {part.strip().upper() for part in os.getenv("SEPA_PROVINCES", "").split(",") if part.strip()}
    if not provinces:
        raise ValueError("Configurá SEPA_PROVINCES antes de cargar el catálogo")
    owned_client = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=True,
        headers={"User-Agent": SEPA_USER_AGENT},
    )
    try:
        url, data_date, expected_sha256 = _source_resource(client)
        with SessionLocal() as session:
            existing = session.query(SepaSnapshot).filter(SepaSnapshot.data_date == data_date).first()
            if existing:
                if existing.status == "ready":
                    return existing.id
                started = existing.started_at
                if started and started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if existing.status == "loading" and started and datetime.now(timezone.utc) - started < timedelta(hours=3):
                    raise ValueError("Una carga SEPA ya está en curso")
                session.execute(delete(SepaPrice).where(SepaPrice.snapshot_id == existing.id))
                session.delete(existing)
                session.commit()
            snapshot = SepaSnapshot(data_date=data_date, source_url=url, status="loading")
            session.add(snapshot)
            session.commit()
            snapshot_id = snapshot.id
            try:
                with _download(client, url, expected_sha256) as source:
                    count, rejected = _ingest_archive(session, source, snapshot, provinces)
                if count == 0:
                    raise ValueError("El archivo SEPA no contiene precios utilizables")
                snapshot.status = "ready"
                snapshot.row_count, snapshot.rejected_count = count, rejected
                snapshot.loaded_at = datetime.now(timezone.utc)
                session.commit()
                logger.info("sepa_ingest_ready rows=%d rejected=%d date=%s", count, rejected, data_date)
            except Exception:
                session.rollback()
                session.execute(delete(SepaPrice).where(SepaPrice.snapshot_id == snapshot_id))
                snapshot.status = "failed"
                session.commit()
                logger.exception("sepa_ingest_failed date=%s", data_date)
                raise
            try:
                old = session.query(SepaSnapshot).filter(SepaSnapshot.id != snapshot_id).all()
                for item in old:
                    session.execute(delete(SepaPrice).where(SepaPrice.snapshot_id == item.id))
                    session.delete(item)
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.warning("sepa_old_snapshot_cleanup_failed error=%s", type(exc).__name__)
            return snapshot_id
    finally:
        if owned_client:
            client.close()


def ingest_if_missing():
    """Bootstrap an empty catalog without reloading an existing ready snapshot."""
    with SessionLocal() as session:
        ready = session.query(SepaSnapshot.id).filter(SepaSnapshot.status == "ready").first()
    if ready:
        return None
    return ingest_latest()
