"""Bounded daily SEPA download and atomic snapshot publication."""
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
MAX_DOWNLOAD = 550_000_000
MAX_INNER_FILE = 1_500_000_000
BATCH_SIZE = 2000


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


def _source_resource(client):
    response = client.get(CATALOG_URL)
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
    return item["url"], published.date()


def _download(client, url):
    file = TemporaryFile()
    size = 0
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(1_048_576):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise ValueError("El archivo SEPA excede el límite de descarga")
                file.write(chunk)
        file.seek(0)
        return file
    except Exception:
        file.close()
        raise


def _price(row, branches, chains, snapshot_id):
    commerce, flag, branch = (row.get(key, "").strip() for key in ("id_comercio", "id_bandera", "id_sucursal"))
    product = row.get("id_producto", "").strip()
    store = branches.get((commerce, flag, branch))
    chain = chains.get((commerce, flag))
    amount = _decimal(row.get("productos_precio_lista"))
    description = row.get("productos_descripcion", "").strip()
    if not (commerce and flag and branch and product and store and chain and amount and description):
        return None
    quantity = _decimal(row.get("productos_cantidad_presentacion"))
    def promo(number):
        price = _decimal(row.get(f"productos_precio_unitario_promo{number}"))
        legend = (row.get(f"productos_leyenda_promo{number}") or "").strip()
        return (price, legend) if price and price < amount and legend else (None, None)
    promo1, text1 = promo(1)
    promo2, text2 = promo(2)
    def coord(value):
        try:
            return Decimal(value) if value else None
        except InvalidOperation:
            return None
    return dict(snapshot_id=snapshot_id, commerce_id=commerce, flag_id=flag, branch_id=branch,
        product_id=product, report_date=chain[1], is_ean=row.get("productos_ean", "").strip() == "1",
        description=description[:500], brand=(row.get("productos_marca") or "").strip()[:100],
        quantity=quantity, unit=(row.get("productos_unidad_medida_presentacion") or "").strip()[:30],
        chain=chain[0][:120], branch=store["sucursales_nombre"][:120],
        province=store.get("sucursales_provincia", "")[:80], locality=store.get("sucursales_localidad", "")[:120],
        latitude=coord(store.get("sucursales_latitud")), longitude=coord(store.get("sucursales_longitud")),
        list_price=amount, promo1_price=promo1, promo1_text=text1, promo2_price=promo2, promo2_text=text2)


def _ingest_archive(session, source, snapshot, provinces):
    count = rejected = 0
    with ZipFile(source) as outer:
        for item in outer.infolist():
            if not item.filename.lower().endswith(".zip"):
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
                            report_date = datetime.fromisoformat(row["comercio_ultima_actualizacion"]).date()
                        except (ValueError, KeyError):
                            continue
                        if not 0 <= (snapshot.data_date - report_date).days <= 2:
                            continue
                        chains[(row["id_comercio"].strip(), row["id_bandera"].strip())] = (
                            row["comercio_bandera_nombre"].strip(), report_date)
                    branches = {(r["id_comercio"].strip(), r["id_bandera"].strip(), r["id_sucursal"].strip()): r
                        for r in _csv(inner, "sucursales.csv")}
                    batch = []
                    for row in _csv(inner, "productos.csv"):
                        product_id = row.get("id_producto", "").strip()
                        if row.get("productos_ean", "").strip() != "1" or not re.fullmatch(
                                r"\d{8}|\d{12}|\d{13}", product_id):
                            continue
                        store = branches.get((row.get("id_comercio", "").strip(),
                            row.get("id_bandera", "").strip(), row.get("id_sucursal", "").strip()))
                        if store and store.get("sucursales_provincia", "").strip().upper() not in provinces:
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
    client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
    try:
        url, data_date = _source_resource(client)
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
                with _download(client, url) as source:
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
