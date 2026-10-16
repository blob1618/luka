import json
import re
from pathlib import Path

import pytest

from app.services.financial_education import load_glossary
from app.services.investment_guidance import (
    InvestmentGuidanceError,
    load_catalog,
    render_overview,
    render_topic,
)

CATALOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "prompts"
    / "investment_guidance.v1.json"
)
REQUIRED_TOPIC_IDS = {
    "reserva_imprevistos",
    "meta_ahorro",
    "plazo_fijo_tradicional",
    "fci_mercado_dinero",
    "comparacion_plazo_fijo_fci",
}
OFFICIAL_HOSTS = {"www.bcra.gob.ar", "www.cnv.gov.ar"}
FORBIDDEN_PATTERNS = (
    r"\d+(?:[.,]\d+)?\s*%",
    r"\bporcentaje[s]?\b",
    r"\btasa[s]?\b",
    r"\brendimiento[s]?\b",
    r"\brentabilidad\b",
    r"\bgarantiza(?:do|da|n)?\b",
    r"\bpromete(?:mos)?\b",
    r"\bpromesa[s]?\b",
    r"\basegura(?:do|da|n)?\b",
    r"\bdisponibilidad inmediata\b",
    r"\brecomendamos\b",
    r"\bte conviene\b",
)


def _topic(catalog, topic_id):
    return next(topic for topic in catalog.topics if topic.id == topic_id)


def _valid_payload():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _write(tmp_path, payload):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _drop_version(payload):
    payload.pop("version")


def _malformed_version(payload):
    payload["version"] = "1.0"


def _drop_review_date(payload):
    payload.pop("reviewed_at")


def _malformed_review_date(payload):
    payload["reviewed_at"] = "13-10-2026"


def _empty_sources(payload):
    payload["sources"] = []


def _malformed_source_url(payload):
    payload["sources"][0]["url"] = "ftp://www.bcra.gob.ar/"


def _duplicate_source_ids(payload):
    payload["sources"][1]["id"] = payload["sources"][0]["id"]


def _empty_topics(payload):
    payload["topics"] = []


def _unknown_source_id(payload):
    payload["topics"][0]["source_ids"] = ["fuente_inexistente"]


def _missing_topic_dimension(payload):
    payload["topics"][0].pop("horizon")


def _empty_topic_risks(payload):
    payload["topics"][0]["risks"] = []


def _malformed_topic_kind(payload):
    payload["topics"][0]["kind"] = "producto"


def _duplicate_topic_ids(payload):
    payload["topics"][1]["id"] = payload["topics"][0]["id"]


def test_catalog_declares_version_and_review_date():
    catalog = load_catalog()

    assert catalog.version == "1.0.0"
    assert catalog.reviewed_at == "2026-10-13"


def test_sources_have_unique_ids_titles_and_official_urls():
    catalog = load_catalog()
    source_ids = [source.id for source in catalog.sources]

    assert len(source_ids) == len(set(source_ids))
    assert source_ids
    for source in catalog.sources:
        assert source.title.strip()
        assert source.url.startswith("https://")
        assert source.url.split("/")[2] in OFFICIAL_HOSTS


def test_every_topic_references_a_known_source_and_every_source_is_used():
    catalog = load_catalog()
    source_ids = {source.id for source in catalog.sources}
    used: set[str] = set()

    for topic in catalog.topics:
        assert topic.source_ids
        assert set(topic.source_ids) <= source_ids
        used.update(topic.source_ids)

    assert used == source_ids


def test_catalog_covers_required_topics_kinds_and_dimensions():
    catalog = load_catalog()

    assert {topic.id for topic in catalog.topics} == REQUIRED_TOPIC_IDS
    assert _topic(catalog, "reserva_imprevistos").kind == "ahorro"
    assert _topic(catalog, "meta_ahorro").kind == "ahorro"
    assert _topic(catalog, "plazo_fijo_tradicional").kind == "inversion"
    assert _topic(catalog, "fci_mercado_dinero").kind == "inversion"
    assert _topic(catalog, "comparacion_plazo_fijo_fci").kind == "comparacion"

    for topic in catalog.topics:
        assert topic.title.strip()
        assert topic.purpose.strip()
        assert topic.horizon.strip()
        assert topic.availability.strip()
        assert topic.risks
        assert topic.limits


@pytest.mark.parametrize(
    "mutator",
    [
        _drop_version,
        _malformed_version,
        _drop_review_date,
        _malformed_review_date,
        _empty_sources,
        _malformed_source_url,
        _duplicate_source_ids,
        _empty_topics,
        _unknown_source_id,
        _missing_topic_dimension,
        _empty_topic_risks,
        _malformed_topic_kind,
        _duplicate_topic_ids,
    ],
)
def test_load_catalog_rejects_malformed_payloads(tmp_path, mutator):
    payload = _valid_payload()
    mutator(payload)

    with pytest.raises(InvestmentGuidanceError):
        load_catalog(_write(tmp_path, payload))


def test_missing_catalog_reports_explicitly(tmp_path):
    with pytest.raises(InvestmentGuidanceError, match="No se pudo cargar"):
        load_catalog(tmp_path / "no_existe.json")


def test_invalid_json_catalog_reports_explicitly(tmp_path):
    path = tmp_path / "roto.json"
    path.write_text("{ no es json", encoding="utf-8")

    with pytest.raises(InvestmentGuidanceError, match="No se pudo cargar"):
        load_catalog(path)


def test_render_overview_is_brief_and_includes_version_date_topics_and_sources():
    catalog = load_catalog()
    text = render_overview()

    assert "v1.0.0" in text
    assert catalog.reviewed_at in text
    for topic in catalog.topics:
        assert topic.title in text
    for source in catalog.sources:
        assert source.url in text

    assert len(text) <= 2000
    assert all(len(line) <= 300 for line in text.splitlines())


def test_render_overview_distinguishes_savings_from_investment():
    text = render_overview()

    assert "Ahorro:" in text
    assert "Inversión:" in text
    assert "(Ahorro)" in text
    assert "(Inversión)" in text
    assert "(Comparación)" in text


def test_render_topic_includes_all_dimensions_sources_and_review_date():
    catalog = load_catalog()
    source_by_id = {source.id: source for source in catalog.sources}

    for topic in catalog.topics:
        text = render_topic(topic.id)

        assert topic.title in text
        assert "Propósito:" in text
        assert topic.purpose in text
        assert "Horizonte:" in text
        assert topic.horizon in text
        assert "Disponibilidad:" in text
        assert topic.availability in text
        assert "Riesgos:" in text
        assert "Límites:" in text
        for risk in topic.risks:
            assert risk in text
        for limit in topic.limits:
            assert limit in text
        for source_id in topic.source_ids:
            assert source_by_id[source_id].title in text
            assert source_by_id[source_id].url in text
        assert f"Revisado: {catalog.reviewed_at}" in text

        assert len(text) <= 1500
        assert all(len(line) <= 300 for line in text.splitlines())


def test_render_unknown_topic_reports_explicitly():
    with pytest.raises(InvestmentGuidanceError, match="Tema educativo desconocido"):
        render_topic("producto_inexistente")


def test_fci_topic_is_not_described_as_a_bank_deposit_or_immediately_available():
    normalized = render_topic("fci_mercado_dinero").casefold()

    assert "no son depósitos bancarios" in normalized
    assert "reglamento" in normalized
    assert "disponibilidad inmediata" not in normalized


def test_rendered_content_has_no_prohibited_claims():
    texts = [render_overview()]
    texts.extend(render_topic(topic.id) for topic in load_catalog().topics)

    for text in texts:
        # Las URLs oficiales citadas pueden contener palabras como "rentabilidad";
        # solo se auditan las afirmaciones del texto educativo.
        body = re.sub(r"https?://\S+", "", text).casefold()
        for pattern in FORBIDDEN_PATTERNS:
            match = re.search(pattern, body)
            assert match is None, (
                f"Afirmacion prohibida {pattern!r} en: "
                f"{body.encode('unicode_escape').decode('ascii')}"
            )


def test_financial_glossary_remains_available_and_versioned():
    payload = json.loads(
        (
            Path(__file__).resolve().parent.parent
            / "prompts"
            / "financial_glossary.v1.json"
        ).read_text(encoding="utf-8")
    )
    glossary = load_glossary()

    assert payload["version"] == "1.0.0"
    assert glossary.version == "1.0.0"
    assert len(glossary.entries) == len(payload["entries"]) == 9
