"""Prepare the official daily SEPA ZIP for the public GitHub release mirror."""
import hashlib
import json
from pathlib import Path

import httpx

from app.services.sepa_ingest import SEPA_USER_AGENT, _download, _source_resource


def prepare(output_dir=Path("."), *, client=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    owned_client = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=True,
        headers={"User-Agent": SEPA_USER_AGENT},
    )
    try:
        source_url, data_date, _ = _source_resource(client, allow_mirror=False)
        archive_path = output_dir / "sepa-latest.zip"
        digest = hashlib.sha256()
        with _download(client, source_url) as source, archive_path.open("wb") as target:
            while chunk := source.read(1_048_576):
                digest.update(chunk)
                target.write(chunk)
        manifest = {
            "data_date": data_date.isoformat(),
            "source_url": source_url,
            "sha256": digest.hexdigest(),
            "size": archive_path.stat().st_size,
        }
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
