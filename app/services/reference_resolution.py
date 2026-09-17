"""Resolve bounded conversational references without trusting model-supplied IDs."""

import re
import unicodedata


MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def normalize_text(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value.lower())
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def selects_all(text: str) -> bool:
    return normalize_text(text) in {"ambos", "ambas", "los dos", "las dos", "todos", "todas", "todos los mostrados"}


def selects_recent(text: str) -> bool:
    normalized = normalize_text(text)
    return bool(re.search(r"\b(ese|esa|ultimo|ultima|anterior|recien|lo|la)\b", normalized))


def select_named_months(text: str, items: list[dict]) -> list[dict]:
    """Return a batch only when several months were explicitly named and unambiguous."""
    normalized = normalize_text(text)
    months = {number for name, number in MONTHS.items() if re.search(rf"\b{name}\b", normalized)}
    if len(months) < 2:
        return []
    selected = [item for item in items if item.get("month") in months]
    if len(selected) != len(months) or {item.get("month") for item in selected} != months:
        return []
    return selected


def select_items(text: str, items: list[dict], *, allow_all: bool = False) -> list[dict]:
    """Select only from the candidate set that was actually shown to the user."""
    normalized = normalize_text(text)
    if allow_all and selects_all(text):
        return items[:]
    ordinals = {"primero": 0, "primera": 0, "segundo": 1, "segunda": 1,
                "tercero": 2, "tercera": 2, "cuarto": 3, "cuarta": 3,
                "quinto": 4, "quinta": 4}
    for word, index in ordinals.items():
        if re.search(rf"\b{word}\b", normalized):
            return [items[index]] if index < len(items) else []
    months = {number for name, number in MONTHS.items() if re.search(rf"\b{name}\b", normalized)}
    if months:
        return [item for item in items if item.get("month") in months]
    named = [item for item in items if item.get("label") and normalize_text(str(item["label"])) in normalized]
    if named:
        return named
    if len(items) == 1 and selects_recent(text):
        return items[:]
    return []
