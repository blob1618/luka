"""Catálogo curado de monedas soportadas por Luka.

Solo monedas de dos decimales: el formateo actual (`amount_text`) usa dos
decimales y no se cambia en esta etapa. Los totales se muestran por moneda;
no hay conversión entre ellas.
"""
from dataclasses import dataclass
from unicodedata import normalize


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    default_wallet_name: str
    symbol: str
    aliases: tuple[str, ...] = ()


CURRENCIES: dict[str, Currency] = {
    "ARS": Currency("ARS", "Peso argentino", "Pesos", "$", ("peso", "pesos", "argentino")),
    "USD": Currency("USD", "Dólar estadounidense", "Dólares", "US$", ("dolar", "dólares", "dolares", "dollar")),
    "BRL": Currency("BRL", "Real brasileño", "Reales", "R$", ("real", "reales", "reais", "brasileño")),
    "EUR": Currency("EUR", "Euro", "Euros", "€", ("euro", "euros")),
}
DEFAULT_CURRENCY = "ARS"


def normalize_currency(value) -> str | None:
    if value is None:
        return None
    code = str(value).strip().upper()
    return code or None


def is_supported(value) -> bool:
    code = normalize_currency(value)
    return code in CURRENCIES


def default_wallet_name(value) -> str:
    code = normalize_currency(value)
    return CURRENCIES[code].default_wallet_name


def supported_codes() -> tuple[str, ...]:
    return tuple(CURRENCIES)


def supported_text() -> str:
    codes = supported_codes()
    return f"{', '.join(codes[:-1])} o {codes[-1]}"


def resolve_alias(value) -> str | None:
    """Resuelve un nombre común (“reales”, “euros”) al código ISO."""
    if value is None:
        return None
    plain = normalize("NFKD", str(value)).encode("ascii", "ignore").decode().strip().casefold()
    if not plain:
        return None
    code = normalize_currency(value)
    if code in CURRENCIES:
        return code
    for currency in CURRENCIES.values():
        if plain == currency.name.casefold() or plain in currency.aliases:
            return currency.code
    return None
