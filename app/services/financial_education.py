"""Respuestas pedagógicas estáticas para consultas financieras no transaccionales."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


_GLOSSARY_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "financial_glossary.v1.json"
)
_CONCEPTUAL_MARKERS = re.compile(
    r"\b(?:que es|que significa|como funciona|explicame|explica|defini|definicion|"
    r"diferencia entre|para que sirve|que quiere decir|como se calcula|tasa actual|"
    r"valor actual|cotizacion actual)\b"
)
_CURRENT_INFORMATION_MARKERS = re.compile(
    r"\b(?:tasa|valor|cotizacion|indice|inflacion)\s+(?:actual|de hoy|vigente)\b"
)


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


@dataclass(frozen=True)
class GlossaryEntry:
    term: str
    aliases: tuple[str, ...]
    definition: str
    example: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class FinancialEducationReply:
    text: str
    status: str
    term: str | None = None


@dataclass(frozen=True)
class FinancialGlossary:
    version: str
    entries: tuple[GlossaryEntry, ...]


@lru_cache(maxsize=1)
def load_glossary() -> FinancialGlossary:
    """Load the reviewed, repository-versioned glossary once per process."""
    with _GLOSSARY_PATH.open(encoding="utf-8") as glossary_file:
        raw: dict[str, Any] = json.load(glossary_file)

    version = raw.get("version")
    entries = raw.get("entries")
    if not isinstance(version, str) or not version or not isinstance(entries, list):
        raise ValueError("Financial glossary has an invalid version or entries list")

    parsed_entries: list[GlossaryEntry] = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("Financial glossary entries must be objects")
        term = item.get("term")
        aliases = item.get("aliases")
        definition = item.get("definition")
        example = item.get("example")
        references = item.get("references")
        if not (
            isinstance(term, str)
            and term.strip()
            and isinstance(aliases, list)
            and all(isinstance(alias, str) and alias.strip() for alias in aliases)
            and isinstance(definition, str)
            and definition.strip()
            and isinstance(example, str)
            and example.strip()
            and isinstance(references, list)
            and all(isinstance(reference, str) and reference.strip() for reference in references)
        ):
            raise ValueError(f"Financial glossary entry is invalid: {item!r}")
        parsed_entries.append(
            GlossaryEntry(
                term=term.strip(),
                aliases=tuple(_normalize(alias) for alias in aliases),
                definition=definition.strip(),
                example=example.strip(),
                references=tuple(reference.strip() for reference in references),
            )
        )

    return FinancialGlossary(version=version, entries=tuple(parsed_entries))


class FinancialEducationService:
    """Resolve conceptual questions without accessing financial persistence services."""

    @staticmethod
    def is_conceptual_query(text: str) -> bool:
        normalized = _normalize(text)
        if not normalized:
            return False

        has_known_term = FinancialEducationService._find_entry(normalized) is not None
        is_ambiguous_interest = "interes" in normalized
        if _CONCEPTUAL_MARKERS.search(normalized):
            return True

        return (has_known_term or is_ambiguous_interest) and len(normalized.split()) <= 4

    @staticmethod
    def answer(text: str, *, requested_term: str | None = None) -> FinancialEducationReply:
        normalized = _normalize(text)
        requested_normalized = _normalize(requested_term) if requested_term else ""

        if _CURRENT_INFORMATION_MARKERS.search(normalized):
            return FinancialEducationReply(
                text=(
                    "No puedo confirmar valores o tasas actuales desde este glosario. "
                    "Para una cifra vigente, consultá una fuente oficial actualizada."
                ),
                status="current_information_unavailable",
            )

        if "interes" in normalized and not any(
            phrase in normalized for phrase in ("interes simple", "interes compuesto")
        ):
            return FinancialEducationReply(
                text=(
                    "¿Te referís a interés simple o interés compuesto? "
                    "Se calculan de manera distinta."
                ),
                status="ambiguous",
            )

        entry = FinancialEducationService._find_entry(normalized)
        if entry is None and requested_normalized:
            entry = FinancialEducationService._find_entry(requested_normalized)
        if entry is None:
            return FinancialEducationReply(
                text=(
                    "No tengo una definición verificada de ese concepto en mi glosario actual. "
                    "Puedo ayudarte con presupuesto, gastos, ahorro, intereses, inflación, deuda o CFT."
                ),
                status="unsupported",
            )

        return FinancialEducationReply(
            text=(
                f"{entry.term.capitalize()}: {entry.definition} "
                f"Ejemplo ilustrativo: {entry.example}"
            ),
            status="supported",
            term=entry.term,
        )

    @staticmethod
    def static_prompt_context() -> str:
        """Return only reviewed shared content, safe for a future shared provider cache."""
        glossary = load_glossary()
        lines = [
            f"GLOSARIO FINANCIERO VERSIONADO v{glossary.version}",
            "Para intent=financial_education, clasificá el término sin convertir ejemplos "
            "con importes en movimientos. Nunca inventes tasas, cotizaciones ni condiciones actuales.",
        ]
        for entry in glossary.entries:
            lines.append(f"- {entry.term}: {entry.definition} Ejemplo: {entry.example}")
        return "\n".join(lines)

    @staticmethod
    def _find_entry(normalized_text: str) -> GlossaryEntry | None:
        candidates: list[tuple[int, GlossaryEntry]] = []
        for entry in load_glossary().entries:
            for alias in entry.aliases:
                if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized_text):
                    candidates.append((len(alias), entry))
        return max(candidates, default=(0, None), key=lambda candidate: candidate[0])[1]
