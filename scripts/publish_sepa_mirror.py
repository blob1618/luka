"""Prepare a dated, compact SEPA ZIP for the public GitHub release mirror."""
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import httpx

from app.services.sepa_ingest import SEPA_USER_AGENT, _download, _source_resource
from scripts.sepa_mirror_archive import compact_official, compact_repackaged

ARCHIVE_INDEX = "https://raw.githubusercontent.com/catdevnull/sepa-precios-metadata/main/index.json"
ARCHIVE_PREFIX = "https://f004.backblazeb2.com/file/precios-justos-datasets/"


def _archival_source(client):
    response = client.get(ARCHIVE_INDEX)
    response.raise_for_status()
    index = response.json()
    if not isinstance(index, dict) or not index:
        raise ValueError("Índice histórico SEPA inválido")
    latest = max(index)
    day = date.fromisoformat(latest)
    age = (datetime.now(timezone.utc).date() - day).days
    if not 0 <= age <= 3:
        raise ValueError("El archivo histórico SEPA es demasiado antiguo")
    entries = index[latest]
    if not isinstance(entries, list):
        raise ValueError("Índice histórico SEPA inválido")
    entry = next((item for item in reversed(entries) if isinstance(item, dict)
                  and item.get("link") and item.get("id")), None)
    if entry is None:
        raise ValueError("El archivo histórico SEPA no está publicado")
    link = entry["link"]
    if not isinstance(link, str) or not link.startswith(ARCHIVE_PREFIX) or not link.endswith(".tar.zst"):
        raise ValueError("URL de archivo histórico SEPA inválida")
    match = re.fullmatch(r"([0-9a-f-]{36})-revID-([0-9a-f-]{36})", entry["id"])
    if not match:
        raise ValueError("Revisión histórica SEPA inválida")
    return link, day, match.group(1), match.group(2)


def prepare(output_dir=Path("."), *, client=None, provinces=frozenset({"AR-H"})):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    owned_client = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=True,
        headers={"User-Agent": SEPA_USER_AGENT},
    )
    try:
        archive_path = output_dir / "sepa-latest.zip"
        try:
            source_url, data_date, _ = _source_resource(client, allow_mirror=False)
            with _download(client, source_url) as source:
                compact_official(source, archive_path, provinces)
            archive_url = None
        except (httpx.HTTPError, ValueError) as exc:
            print(f"Official source unavailable ({type(exc).__name__}); checking dated archive")
            archive_url, data_date, resource_id, revision_id = _archival_source(client)
            with _download(client, archive_url) as source:
                source_url, _ = compact_repackaged(source, archive_path, data_date,
                                                    resource_id, revision_id, provinces)
        digest = hashlib.sha256()
        with archive_path.open("rb") as source:
            while chunk := source.read(1_048_576):
                digest.update(chunk)
        manifest = {
            "data_date": data_date.isoformat(),
            "source_url": source_url,
            "sha256": digest.hexdigest(),
            "size": archive_path.stat().st_size,
            "coverage": sorted(provinces),
        }
        if archive_url:
            manifest["archive_url"] = archive_url
        if not manifest["size"]:
            raise ValueError("El ZIP SEPA descargado está vacío")
        (output_dir / "sepa-latest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest
    finally:
        if owned_client:
            client.close()


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False))
