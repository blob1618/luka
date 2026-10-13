"""Catálogo educativo versionado sobre ahorro e inversión, sin datos de mercado.

El contenido vive en ``prompts/investment_guidance.v1.json``: es estático,
revisado y versionado en Git. Este servicio solo lo carga y lo renderiza para
WhatsApp; no accede a la red, no consulta productos y no personaliza consejos.
Si el catálogo no está disponible o es inválido, la falla se informa con
``InvestmentGuidanceError`` en lugar de degradarse en silencio.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any


_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "investment_guidance.v1.json"
)
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_HTTP_PATTERN = re.compile(r"^https?://\S+$")
_KIND_LABELS = {
    "ahorro": "Ahorro",
    "inversion": "Inversión",
    "comparacion": "Comparación",
}


class InvestmentGuidanceError(RuntimeError):
    """Falla explícita cuando el catálogo educativo no puede cargarse o es inválido."""


@dataclass(frozen=True)
class GuidanceSource:
    id: str
    title: str
    url: str


@dataclass(frozen=True)
class GuidanceTopic:
    id: str
    title: str
    kind: str
    purpose: str
    horizon: str
    availability: str
    risks: tuple[str, ...]
    limits: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class InvestmentCatalog:
    version: str
    reviewed_at: str
    sources: tuple[GuidanceSource, ...]
    topics: tuple[GuidanceTopic, ...]


def _require_text(item: dict[str, Any], key: str, context: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvestmentGuidanceError(
            f"Catálogo educativo inválido: falta {key!r} en {context}"
        )
    return value.strip()


def _require_text_list(item: dict[str, Any], key: str, context: str) -> tuple[str, ...]:
    value = item.get(key)
    if not isinstance(value, list) or not value:
        raise InvestmentGuidanceError(
            f"Catálogo educativo inválido: {key!r} debe ser una lista no vacía en {context}"
        )
    texts: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise InvestmentGuidanceError(
                f"Catálogo educativo inválido: {key!r} contiene un valor vacío en {context}"
            )
        texts.append(entry.strip())
    return tuple(texts)


def _parse_source(item: Any, index: int) -> GuidanceSource:
    if not isinstance(item, dict):
        raise InvestmentGuidanceError(
            "Catálogo educativo inválido: cada fuente debe ser un objeto"
        )
    context = f"la fuente #{index + 1}"
    source_id = _require_text(item, "id", context)
    if not _SLUG_PATTERN.match(source_id):
        raise InvestmentGuidanceError(
            f"Catálogo educativo inválido: id de fuente no válido {source_id!r}"
        )
    url = _require_text(item, "url", context)
    if not _HTTP_PATTERN.match(url):
        raise InvestmentGuidanceError(
            f"Catálogo educativo inválido: URL no válida en la fuente {source_id!r}"
        )
    return GuidanceSource(
        id=source_id,
        title=_require_text(item, "title", context),
        url=url,
    )


def _parse_topic(item: Any, index: int, source_ids: set[str]) -> GuidanceTopic:
    if not isinstance(item, dict):
        raise InvestmentGuidanceError(
            "Catálogo educativo inválido: cada tema debe ser un objeto"
        )
    context = f"el tema #{index + 1}"
    topic_id = _require_text(item, "id", context)
    if not _SLUG_PATTERN.match(topic_id):
        raise InvestmentGuidanceError(
            f"Catálogo educativo inválido: id de tema no válido {topic_id!r}"
        )
    kind = _require_text(item, "kind", context)
    if kind not in _KIND_LABELS:
        raise InvestmentGuidanceError(
            f"Catálogo educativo inválido: tipo desconocido {kind!r} en el tema {topic_id!r}"
        )
    references = _require_text_list(item, "source_ids", f"el tema {topic_id!r}")
    unknown = [reference for reference in references if reference not in source_ids]
    if unknown:
        raise InvestmentGuidanceError(
            f"Catálogo educativo inválido: el tema {topic_id!r} cita fuentes "
            f"inexistentes {unknown!r}"
        )
    return GuidanceTopic(
        id=topic_id,
        title=_require_text(item, "title", context),
        kind=kind,
        purpose=_require_text(item, "purpose", context),
        horizon=_require_text(item, "horizon", context),
        availability=_require_text(item, "availability", context),
        risks=_require_text_list(item, "risks", f"el tema {topic_id!r}"),
        limits=_require_text_list(item, "limits", f"el tema {topic_id!r}"),
        source_ids=references,
    )


def _parse_catalog(raw: Any) -> InvestmentCatalog:
    if not isinstance(raw, dict):
        raise InvestmentGuidanceError(
            "Catálogo educativo inválido: se esperaba un objeto JSON"
        )

    version = raw.get("version")
    if not isinstance(version, str) or not _VERSION_PATTERN.match(version):
        raise InvestmentGuidanceError(
            "Catálogo educativo inválido: versión ausente o no válida"
        )

    reviewed_at = raw.get("reviewed_at")
    if not isinstance(reviewed_at, str):
        raise InvestmentGuidanceError(
            "Catálogo educativo inválido: fecha de revisión ausente"
        )
    try:
        date.fromisoformat(reviewed_at)
    except ValueError as error:
        raise InvestmentGuidanceError(
            f"Catálogo educativo inválido: fecha de revisión no válida {reviewed_at!r}"
        ) from error

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise InvestmentGuidanceError(
            "Catálogo educativo inválido: fuentes ausentes"
        )
    sources = tuple(_parse_source(item, index) for index, item in enumerate(raw_sources))
    source_ids = [source.id for source in sources]
    if len(set(source_ids)) != len(source_ids):
        raise InvestmentGuidanceError(
            "Catálogo educativo inválido: ids de fuente duplicados"
        )

    raw_topics = raw.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise InvestmentGuidanceError("Catálogo educativo inválido: temas ausentes")
    topics = tuple(
        _parse_topic(item, index, set(source_ids))
        for index, item in enumerate(raw_topics)
    )
    topic_ids = [topic.id for topic in topics]
    if len(set(topic_ids)) != len(topic_ids):
        raise InvestmentGuidanceError(
            "Catálogo educativo inválido: ids de tema duplicados"
        )

    return InvestmentCatalog(
        version=version,
        reviewed_at=reviewed_at,
        sources=sources,
        topics=topics,
    )


@lru_cache(maxsize=4)
def load_catalog(path: Path | None = None) -> InvestmentCatalog:
    """Cargar el catálogo educativo versionado una vez por proceso.

    Si el archivo no existe, no es JSON válido o no cumple el schema, se lanza
    ``InvestmentGuidanceError`` con el motivo explícito.
    """
    catalog_path = path or _CATALOG_PATH
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvestmentGuidanceError(
            f"No se pudo cargar el catálogo educativo {catalog_path}: {error}"
        ) from error
    return _parse_catalog(raw)


def _source_by_id(catalog: InvestmentCatalog, source_id: str) -> GuidanceSource:
    for source in catalog.sources:
        if source.id == source_id:
            return source
    raise InvestmentGuidanceError(
        f"Catálogo educativo inconsistente: falta la fuente {source_id!r}"
    )


def render_overview() -> str:
    """Renderizar un resumen breve del catálogo, apto para WhatsApp."""
    catalog = load_catalog()
    lines = [
        f"Orientación educativa v{catalog.version} — revisada el {catalog.reviewed_at}",
        "",
        "Ahorro: separás dinero para un objetivo o un imprevisto.",
        "Inversión: ponés dinero esperando un resultado que puede variar.",
        "",
        "Temas disponibles:",
    ]
    for topic in catalog.topics:
        lines.append(f"- {topic.title} ({_KIND_LABELS[topic.kind]}): {topic.purpose}")
    lines.extend(
        [
            "",
            "Es contenido general y educativo; no es una recomendación personal ni una oferta.",
            "Fuentes:",
        ]
    )
    for source in catalog.sources:
        lines.append(f"- {source.title}: {source.url}")
    return "\n".join(lines)


def render_topic(topic_id: str) -> str:
    """Renderizar un tema completo con sus dimensiones, fuentes y fecha de revisión."""
    catalog = load_catalog()
    normalized = topic_id.strip()
    topic = next((item for item in catalog.topics if item.id == normalized), None)
    if topic is None:
        available = ", ".join(item.id for item in catalog.topics)
        raise InvestmentGuidanceError(
            f"Tema educativo desconocido: {topic_id!r}. Disponibles: {available}"
        )

    lines = [
        topic.title,
        f"Tipo: {_KIND_LABELS[topic.kind]}",
        "",
        f"Propósito: {topic.purpose}",
        f"Horizonte: {topic.horizon}",
        f"Disponibilidad: {topic.availability}",
        "",
        "Riesgos:",
    ]
    lines.extend(f"- {risk}" for risk in topic.risks)
    lines.extend(["", "Límites:"])
    lines.extend(f"- {limit}" for limit in topic.limits)
    lines.extend(["", "Fuentes:"])
    for source_id in topic.source_ids:
        source = _source_by_id(catalog, source_id)
        lines.append(f"- {source.title}: {source.url}")
    lines.append(f"Revisado: {catalog.reviewed_at}")
    return "\n".join(lines)
