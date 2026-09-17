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


def recent_count(text: str) -> int | None:
    """Recognize a bounded request for the newest shown movements."""
    normalized = normalize_text(text)
    match = re.search(
        r"\b(?:(?:ultimos|ultimas)\s+(dos|tres|cuatro|cinco|[2-5])|"
        r"(dos|tres|cuatro|cinco|[2-5])\s+(?:ultimos|ultimas|mas recientes))\b",
        normalized,
    )
    if match is None:
        return None
    value = match.group(1) or match.group(2)
    return {"dos": 2, "tres": 3, "cuatro": 4, "cinco": 5}.get(
        value, int(value) if value.isdigit() else None
    )


def named_movement_targets(text: str) -> list[str]:
    """Extract explicit coordinated names such as 'borrá ventilador y tv'."""
    normalized = normalize_text(text)
    match = re.match(r"^(?:borra|borrar|elimina|eliminar|anula|anular)\s+(.+)$", normalized)
    if match is None or " y " not in match.group(1):
        return []
    names = []
    for part in match.group(1).split(" y "):
        name = re.sub(r"^(?:(?:el|la|los|las|de|del|un|una|movimiento|movimientos|gasto|gastos|compra|compras)\s+)*", "", part).strip()
        if not name or re.search(r"\b(?:ultimos|ultimas|todos|todas)\b", name):
            return []
        names.append(name)
    return names if len(names) >= 2 and len(set(names)) == len(names) else []


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
