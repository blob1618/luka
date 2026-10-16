import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher

DEFAULT_TAXONOMY: dict[str, tuple[str, ...]] = {
    "Servicios": ("servicios", "luz", "electricidad", "agua", "gas", "internet", "wifi", "telefono", "cable"),
    "Comida": ("comida", "alimentacion", "alimentos", "super", "supermercado", "despensa"),
    "Transporte": ("transporte", "nafta", "combustible", "gasolina", "colectivo", "uber", "taxi", "subtes"),
    "Ocio": ("ocio", "salidas", "entretenimiento", "cine"),
    "Vivienda": ("vivienda", "alquiler", "renta", "expensas", "inmobiliaria"),
    "Salud": ("salud", "farmacia", "medicina", "consulta"),
    "Ingresos": ("ingresos", "sueldo", "salario", "sueldos", "haberes"),
    "Educacion": ("educacion", "estudios", "colegio", "universidad", "cursos"),
    "Ropa": ("ropa", "indumentaria", "vestimenta", "calzado"),
}


def _normalize(name: str | None) -> str:
    if name is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", name.strip().lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_SYNONYM_TO_CANONICAL: dict[str, str] = {
    _normalize(synonym): canonical for canonical, synonyms in DEFAULT_TAXONOMY.items() for synonym in synonyms
}


def resolve_category(name: str | None, user_category_names: set[str] | None = None) -> str | None:
    normalized = _normalize(name)
    if not normalized:
        return None

    if normalized in _SYNONYM_TO_CANONICAL:
        return _SYNONYM_TO_CANONICAL[normalized]

    user_names: Iterable[str] = user_category_names or set()
    for raw in user_names:
        if _normalize(raw) == normalized:
            return name

    return None


def resolve_category_for_user(
    name: str | None, user_category_names: set[str] | None = None, *,
    allow_fuzzy: bool = False,
) -> str | None:
    """Resuelve `name` a un nombre de categoría *adjuntable* al usuario.

    Si la taxonomía resuelve a un canónico C y el usuario tiene una categoría que
    también resuelve a C (sinónimo del mismo canónico, o igual a C), devuelve el
    nombre real de esa categoría del usuario para que el movimiento se adhiera a ella.
    Si no hay tal categoría, devuelve el canónico C (o el match exacto del usuario).
    Si nada coincide, devuelve ``None``.
    """
    normalized = _normalize(name)
    if not normalized:
        return None

    user_names = sorted(user_category_names or set(), key=lambda raw: (_normalize(raw), raw))
    exact_user_names = [raw for raw in user_names if _normalize(raw) == normalized]
    if exact_user_names:
        return exact_user_names[0] if len(exact_user_names) == 1 else None

    canonical = _SYNONYM_TO_CANONICAL.get(normalized)
    if canonical:
        for raw in user_names:
            if _normalize(raw) == _normalize(canonical):
                return raw
        for raw in user_names:
            if _SYNONYM_TO_CANONICAL.get(_normalize(raw)) == canonical:
                return raw
        return canonical

    return suggest_user_category(name, user_names) if allow_fuzzy else None


def suggest_user_category(name: str | None, user_category_names: Iterable[str]) -> str | None:
    """Return one clear active-name match; never guess between close candidates."""
    requested = _normalize(name)
    if len(requested) < 4:
        return None
    names = sorted(set(user_category_names), key=lambda raw: (_normalize(raw), raw))
    exact = [raw for raw in names if _normalize(raw) == requested]
    if exact:
        return exact[0] if len(exact) == 1 else None
    scores = sorted(
        ((SequenceMatcher(None, requested, _normalize(raw)).ratio(), raw) for raw in names),
        reverse=True,
    )
    if not scores or scores[0][0] < 0.82:
        return None
    if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.08:
        return None
    return scores[0][1]


def category_named_in_text(text: str) -> str | None:
    """Extract an explicit category label at the end of a movement message."""
    named = re.search(
        r"\bcategor[ií]a\s+(?P<name>[\wÀ-ÿ][\wÀ-ÿ -]{0,99})\s*[.!]?\s*$",
        text,
        re.IGNORECASE,
    )
    return named.group("name").strip() if named else None


def category_explicitly_named_in_text(text: str) -> str | None:
    """Return a canonical category only when one taxonomy family is named."""
    normalized = _normalize(text)
    matches = {
        canonical
        for synonym, canonical in _SYNONYM_TO_CANONICAL.items()
        if re.search(rf"(?<!\w){re.escape(synonym)}(?!\w)", normalized)
    }
    return next(iter(matches)) if len(matches) == 1 else None

