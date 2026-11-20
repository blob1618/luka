"""Build a small AR-H SEPA ZIP from the official ZIP or a dated archival copy."""
import csv
from datetime import date
import io
from pathlib import PurePosixPath
import re
import shutil
from tempfile import SpooledTemporaryFile
import tarfile
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import zstandard

from app.services.sepa_ingest import MAX_INNER_FILE


def _rows(stream):
    return csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace", newline=""),
                          delimiter="|")


def _write_csv(archive, name, fieldnames, rows):
    with archive.open(name, "w") as raw:
        with io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
            writer = csv.DictWriter(text, fieldnames=fieldnames, delimiter="|", lineterminator="\n",
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)


def _write_merchant(outer, name, commerce, branches, products, provinces):
    branch_reader = _rows(branches)
    selected = [row for row in branch_reader
                if (row.get("sucursales_provincia") or "").strip().upper() in provinces]
    if not selected:
        return 0
    branch_keys = {(row.get("id_comercio"), row.get("id_bandera"), row.get("id_sucursal"))
                   for row in selected}
    with SpooledTemporaryFile(max_size=8_000_000) as inner_file:
        with ZipFile(inner_file, "w", compression=ZIP_DEFLATED, compresslevel=6) as inner:
            with inner.open("comercio.csv", "w") as target:
                shutil.copyfileobj(commerce, target, length=1_048_576)
            _write_csv(inner, "sucursales.csv", branch_reader.fieldnames, selected)
            product_reader = _rows(products)
            count = 0

            def matching():
                nonlocal count
                for row in product_reader:
                    key = (row.get("id_comercio"), row.get("id_bandera"), row.get("id_sucursal"))
                    if key in branch_keys:
                        count += 1
                        yield row

            _write_csv(inner, "productos.csv", product_reader.fieldnames, matching())
        if not count:
            return 0
        inner_file.seek(0)
        with outer.open(f"{name}.zip", "w", force_zip64=True) as target:
            shutil.copyfileobj(inner_file, target, length=1_048_576)
    return count


def compact_official(source, output, provinces):
    """Keep the original daily ZIP contract while dropping other provinces."""
    count = 0
    with ZipFile(source) as original, ZipFile(output, "w", compression=ZIP_STORED) as compact:
        for member in original.infolist():
            if not member.filename.lower().endswith(".zip") or member.file_size == 0:
                continue
            if member.file_size > MAX_INNER_FILE:
                raise ValueError("Comercio SEPA excesivo")
            with SpooledTemporaryFile(max_size=8_000_000) as spool:
                with original.open(member) as stream:
                    shutil.copyfileobj(stream, spool, length=1_048_576)
                spool.seek(0)
                with ZipFile(spool) as merchant:
                    with (merchant.open("comercio.csv") as commerce,
                          merchant.open("sucursales.csv") as branches,
                          merchant.open("productos.csv") as products):
                        count += _write_merchant(compact, PurePosixPath(member.filename).stem,
                                                 commerce, branches, products, provinces)
    if count == 0:
        raise ValueError("La fuente SEPA no contiene precios de las provincias configuradas")
    return count


def compact_repackaged(source, output, data_date, resource_id, revision_id, provinces):
    """Read an archived tar.zst and validate its embedded official metadata."""
    if not re.fullmatch(r"[0-9a-f-]{36}", resource_id) or not re.fullmatch(r"[0-9a-f-]{36}", revision_id):
        raise ValueError("Identidad de archivo SEPA inválida")
    metadata = None
    count = 0
    seen = set()
    current = None
    files = {}

    def finish(outer):
        nonlocal count
        if current is None:
            return
        if set(files) != {"comercio.csv", "sucursales.csv", "productos.csv"}:
            raise ValueError("Comercio archivado SEPA incompleto")
        for stream in files.values():
            stream.seek(0)
        count += _write_merchant(outer, current, files["comercio.csv"],
                                 files["sucursales.csv"], files["productos.csv"], provinces)
        for stream in files.values():
            stream.close()
        files.clear()

    with ZipFile(output, "w", compression=ZIP_STORED) as compact:
        with zstandard.ZstdDecompressor().stream_reader(source) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    parts = PurePosixPath(member.name).parts
                    if parts == ("dataset-info.json",) or parts == (".", "dataset-info.json"):
                        if member.size > 2_000_000:
                            raise ValueError("Metadatos SEPA excesivos")
                        import json
                        metadata = json.load(archive.extractfile(member))
                        continue
                    if parts and parts[0] == ".":
                        parts = parts[1:]
                    if len(parts) != 3 or parts[0] != data_date.isoformat() or parts[2] not in (
                            "comercio.csv", "sucursales.csv", "productos.csv"):
                        continue
                    merchant = parts[1]
                    if not re.fullmatch(r"[\w.-]+", merchant) or member.size > MAX_INNER_FILE:
                        raise ValueError("Archivo de comercio SEPA inválido")
                    if merchant != current:
                        finish(compact)
                        if merchant in seen:
                            raise ValueError("Comercio SEPA duplicado en el archivo")
                        seen.add(merchant)
                        current = merchant
                    if parts[2] in files:
                        raise ValueError("CSV SEPA duplicado en el archivo")
                    spool = SpooledTemporaryFile(max_size=8_000_000)
                    with archive.extractfile(member) as stream:
                        shutil.copyfileobj(stream, spool, length=1_048_576)
                    files[parts[2]] = spool
            finish(compact)
    if count == 0 or not isinstance(metadata, dict):
        raise ValueError("Archivo archivado SEPA vacío o sin metadatos")
    resources = metadata.get("result", {}).get("resources", [])
    resource = next((item for item in resources if item.get("id") == resource_id), None)
    if not resource or resource.get("revision_id") != revision_id:
        raise ValueError("La revisión archivada no coincide con los metadatos SEPA")
    if date.fromisoformat(resource["last_modified"][:10]) != data_date:
        raise ValueError("La fecha archivada no coincide con los metadatos SEPA")
    url = resource.get("url", "")
    if not url.startswith("https://datos.produccion.gob.ar/") or not url.endswith(".zip"):
        raise ValueError("La URL oficial archivada es inválida")
    return url, count
