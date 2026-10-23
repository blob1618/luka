"""Cotizaciones USD con caché Redis y degradación stale (STK-217).

Caché por tipo de cotización con una ventana fresh corta y un TTL largo, para
poder servir datos desactualizados ("stale") hasta 24 h si el proveedor falla.

El lock por tipo es por proceso: no evita estampida entre workers (suficiente
para esta HU; si se escala, usar un lock distribuido en Redis).
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Awaitable, Callable, Iterable

import httpx
import redis.asyncio as redis

logger = logging.getLogger(__name__)

DOLARAPI_URL = "https://dolarapi.com/v1/dolares"
_CACHE_KEY = "luka:quote:usd:{tipo}"
DEFAULT_STALE_SECONDS = 86400
DEFAULT_CACHE_TTL_SECONDS = 900
FETCHER_TIMEOUT = 3.0
_SOURCE = "DolarAPI"


class QuoteType(str, Enum):
    OFICIAL = "oficial"
    MEP = "mep"
    BLUE = "blue"


@dataclass(frozen=True)
class UsdQuote:
    tipo: QuoteType
    compra: Decimal | None
    venta: Decimal
    fuente: str
    as_of: datetime
    fetched_at: datetime
    stale: bool = False


class QuoteUnavailableError(Exception):
    """No hay cotización disponible para el tipo pedido."""


# Estado global del servicio (por proceso).
_redis_client: redis.Redis | None = None
_redis_loop_id: int | None = None
_locks: dict[QuoteType, asyncio.Lock] = {}

# Casa de DolarAPI → tipo interno. Las otras 4 casas se ignoran.
_QUOTE_HOUSES = {
    "oficial": QuoteType.OFICIAL,
    "bolsa": QuoteType.MEP,
    "blue": QuoteType.BLUE,
}

_Fetcher = Callable[[], Awaitable[dict[QuoteType, UsdQuote]]]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _stale_seconds() -> int:
    return _env_int("CURRENCY_STALE_SECONDS", DEFAULT_STALE_SECONDS)


def _cache_ttl_seconds() -> int:
    return _env_int("CURRENCY_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS)


def _key(tipo: QuoteType) -> str:
    return _CACHE_KEY.format(tipo=tipo.value)


def _lock_for(tipo: QuoteType) -> asyncio.Lock:
    return _locks.setdefault(tipo, asyncio.Lock())


def reset_currency_service() -> None:
    """Suelta el cliente Redis y limpia los locks; no cierra conexiones async."""
    global _redis_client, _redis_loop_id
    _redis_client = None
    _redis_loop_id = None
    _locks.clear()


async def _resolve_redis_client() -> redis.Redis | None:
    global _redis_client, _redis_loop_id
    if _redis_client is not None and _redis_loop_id == id(asyncio.get_running_loop()):
        return _redis_client
    url = os.getenv("REDIS_URL")
    if not url:
        logger.warning("currency_cache redis disabled: REDIS_URL not set; serving without cache")
        return None
    try:
        _redis_client = redis.from_url(
            url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3
        )
    except Exception as exc:
        logger.warning(
            "currency_cache redis disabled: invalid REDIS_URL (%s); serving without cache",
            exc,
        )
        return None
    _redis_loop_id = id(asyncio.get_running_loop())
    return _redis_client


# ---------------------------------------------------------------------------
# Serialización del caché
# ---------------------------------------------------------------------------

def _quote_to_dict(q: UsdQuote) -> dict:
    return {
        "tipo": q.tipo.value,
        "compra": str(q.compra) if q.compra is not None else None,
        "venta": str(q.venta),
        "fuente": q.fuente,
        "as_of": q.as_of.isoformat(),
        "fetched_at": q.fetched_at.isoformat(),
    }


def _quote_from_dict(data: dict) -> UsdQuote:
    compra_raw = data.get("compra")
    return UsdQuote(
        tipo=QuoteType(data["tipo"]),
        compra=Decimal(str(compra_raw)) if compra_raw is not None else None,
        venta=Decimal(str(data["venta"])),
        fuente=data["fuente"],
        as_of=datetime.fromisoformat(data["as_of"]),
        fetched_at=datetime.fromisoformat(data["fetched_at"]),
        stale=False,
    )


async def _cache_get(client, tipo: QuoteType) -> UsdQuote | None:
    if client is None:
        return None
    try:
        raw = await client.get(_key(tipo))
    except redis.RedisError as exc:
        logger.warning("currency_cache redis get failed tipo=%s err=%s", tipo.value, exc)
        return None
    if raw is None:
        return None
    try:
        return _quote_from_dict(json.loads(raw))
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        logger.warning("currency_cache corrupt cache tipo=%s err=%s", tipo.value, exc)
        return None


async def _cache_set(client, tipo: QuoteType, quote: UsdQuote) -> None:
    if client is None:
        return
    try:
        await client.set(
            _key(tipo), json.dumps(_quote_to_dict(quote)), ex=_stale_seconds()
        )
    except redis.RedisError as exc:
        logger.warning("currency_cache redis set failed tipo=%s err=%s", tipo.value, exc)


# ---------------------------------------------------------------------------
# Fetcher DolarAPI
# ---------------------------------------------------------------------------

def _parse_positive_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d > 0 else None


def _parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_dolarapi(data, fetched_at: datetime) -> dict[QuoteType, UsdQuote]:
    if not isinstance(data, list):
        raise QuoteUnavailableError("DolarAPI devolvió un JSON que no es lista")
    quotes: dict[QuoteType, UsdQuote] = {}
    for casa in data:
        if not isinstance(casa, dict):
            continue
        tipo = _QUOTE_HOUSES.get(casa.get("casa"))
        if tipo is None:
            continue
        venta = _parse_positive_decimal(casa.get("venta"))
        if venta is None:
            continue  # sin venta válida la casa no sirve
        compra = _parse_positive_decimal(casa.get("compra"))
        if compra is not None and compra > venta:
            compra = None
        as_of = _parse_datetime(casa.get("fechaActualizacion")) or fetched_at
        quotes[tipo] = UsdQuote(
            tipo=tipo,
            compra=compra,
            venta=venta,
            fuente=_SOURCE,
            as_of=as_of,
            fetched_at=fetched_at,
            stale=False,
        )
    if not quotes:
        raise QuoteUnavailableError("DolarAPI no devolvió cotizaciones válidas")
    return quotes


async def _fetch_dolarapi() -> dict[QuoteType, UsdQuote]:
    fetched_at = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=FETCHER_TIMEOUT) as client:
        resp = await client.get(DOLARAPI_URL)
    if resp.status_code != 200:
        raise QuoteUnavailableError(f"DolarAPI respondió {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise QuoteUnavailableError("DolarAPI devolvió JSON inválido") from exc
    return _parse_dolarapi(data, fetched_at)


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------

def _is_fresh(quote: UsdQuote, now: datetime) -> bool:
    return (now - quote.fetched_at).total_seconds() <= _cache_ttl_seconds()


def _within_stale(quote: UsdQuote, now: datetime) -> bool:
    return (now - quote.fetched_at).total_seconds() <= _stale_seconds()


async def _fetch_one(fetcher: _Fetcher, tipo: QuoteType) -> UsdQuote:
    quotes = await fetcher()
    if not isinstance(quotes, dict):
        raise QuoteUnavailableError("fetcher devolvió un tipo inválido")
    quote = quotes.get(tipo)
    if quote is None:
        raise QuoteUnavailableError(f"cotización no disponible para {tipo.value}")
    return quote


def _log(tipo: QuoteType, event: str, start: float) -> None:
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    logger.info(
        "currency_cache tipo=%s event=%s latency_ms=%.2f",
        tipo.value,
        event,
        latency_ms,
        extra={"tipo": tipo.value, "event": event, "latency_ms": latency_ms},
    )


async def get_usd_quote(
    tipo: QuoteType, *, fetcher: _Fetcher | None = None, redis_client=None
) -> UsdQuote:
    tipo = QuoteType(tipo)
    fetcher = fetcher or _fetch_dolarapi
    start = time.perf_counter()

    client = redis_client if redis_client is not None else await _resolve_redis_client()
    now = datetime.now(timezone.utc)

    cached = await _cache_get(client, tipo)
    if cached is not None and _is_fresh(cached, now):
        _log(tipo, "hit", start)
        return cached

    lock = _lock_for(tipo)
    async with lock:
        cached = await _cache_get(client, tipo)
        if cached is not None and _is_fresh(cached, now):
            _log(tipo, "hit", start)
            return cached
        try:
            quote = await _fetch_one(fetcher, tipo)
        except Exception as exc:
            if cached is not None and _within_stale(cached, now):
                _log(tipo, "stale", start)
                return replace(cached, stale=True)
            raise QuoteUnavailableError(
                f"cotización no disponible para {tipo.value}"
            ) from exc
        await _cache_set(client, tipo, quote)
        _log(tipo, "miss", start)
        return quote


async def get_usd_quotes(
    tipos: Iterable[QuoteType] | None = None, *, fetcher=None, redis_client=None
) -> dict[QuoteType, UsdQuote]:
    if tipos is None:
        selected = list(QuoteType)
    else:
        selected: list[QuoteType] = []
        for t in tipos:
            t = QuoteType(t)
            if t not in selected:
                selected.append(t)
        if not selected:
            selected = list(QuoteType)

    results = await asyncio.gather(
        *(get_usd_quote(t, fetcher=fetcher, redis_client=redis_client) for t in selected),
        return_exceptions=True,
    )
    quotes: dict[QuoteType, UsdQuote] = {}
    for t, r in zip(selected, results):
        if isinstance(r, UsdQuote):
            quotes[t] = r
    if not quotes:
        raise QuoteUnavailableError("no hay cotizaciones disponibles")
    return quotes
