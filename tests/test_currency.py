"""Tests del servicio de cotizaciones con caché y stale (STK-217)."""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

import app.services.currency as currency
from app.services.currency import (
    QuoteType,
    UsdQuote,
    QuoteUnavailableError,
    get_usd_quote,
    get_usd_quotes,
    reset_currency_service,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class TTLFakeRedis:
    """Cliente Redis fake con TTL real (store + expiraciones + get/set/delete)."""

    def __init__(self):
        self._storage: dict[str, str] = {}
        self._expirations: dict[str, float] = {}
        self.set_calls: list[tuple[str, int | None]] = []
        self._closed = False

    async def get(self, key: str) -> str | None:
        key = str(key)
        if key not in self._storage:
            return None
        deadline = self._expirations.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self._storage.pop(key, None)
            self._expirations.pop(key, None)
            return None
        return self._storage[key]

    async def set(self, key: str, value, *, ex=None, **kwargs) -> bool:
        key = str(key)
        self._storage[key] = str(value) if not isinstance(value, str) else value
        if ex is not None:
            self._expirations[key] = time.monotonic() + int(ex)
        self.set_calls.append((key, int(ex) if ex is not None else None))
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            k = str(k)
            if (
                self._storage.pop(k, None) is not None
                or self._expirations.pop(k, None) is not None
            ):
                removed += 1
        return removed

    async def aclose(self) -> None:
        self._closed = True


def make_quote(
    tipo: QuoteType,
    fetched_at: datetime | None = None,
    compra: Decimal | None = Decimal("1500.00"),
    venta: Decimal = Decimal("1550.00"),
) -> UsdQuote:
    now = fetched_at if fetched_at is not None else datetime.now(timezone.utc)
    return UsdQuote(
        tipo=tipo,
        compra=compra,
        venta=venta,
        fuente="DolarAPI",
        as_of=now,
        fetched_at=now,
        stale=False,
    )


def ok_fetcher(quote: UsdQuote | None = None):
    async def f():
        q = quote if quote is not None else make_quote(QuoteType.OFICIAL)
        return {q.tipo: q}

    return f


def failing_fetcher(exc: Exception):
    async def f():
        raise exc

    return f


async def seed(redis: TTLFakeRedis, quote: UsdQuote) -> None:
    await redis.set(
        currency._key(quote.tipo), json.dumps(currency._quote_to_dict(quote)), ex=86400
    )


@pytest.fixture(autouse=True)
def _reset_service():
    reset_currency_service()
    yield
    reset_currency_service()


# ---------------------------------------------------------------------------
# 1. Hit fresh → fetcher no llamado, stale=False.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hit_fresh_does_not_call_fetcher(caplog):
    caplog.set_level(logging.INFO)
    redis = TTLFakeRedis()
    await seed(redis, make_quote(QuoteType.OFICIAL))

    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise AssertionError("fetcher should not be called on a fresh hit")

    quote = await get_usd_quote(QuoteType.OFICIAL, fetcher=boom, redis_client=redis)

    assert quote.stale is False
    assert calls["n"] == 0
    events = [
        getattr(r, "event", None)
        for r in caplog.records
        if r.name == "app.services.currency"
    ]
    assert "hit" in events
    assert any("event=hit" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. Hit de 20 min + fetch OK → refresca, stale=False.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_20min_refresh_ok(caplog):
    caplog.set_level(logging.INFO)
    redis = TTLFakeRedis()
    ts = datetime.now(timezone.utc) - timedelta(minutes=20)
    await seed(redis, make_quote(QuoteType.OFICIAL, fetched_at=ts))
    fresh = make_quote(QuoteType.OFICIAL, venta=Decimal("1600.00"))

    quote = await get_usd_quote(
        QuoteType.OFICIAL, fetcher=ok_fetcher(fresh), redis_client=redis
    )

    assert quote.stale is False
    assert quote.venta == Decimal("1600.00")
    cached = json.loads(await redis.get(currency._key(QuoteType.OFICIAL)))
    assert cached["venta"] == "1600.00"
    assert any("event=miss" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. Hit de 20 min + fetch falla → stale=True, fetched_at conservado.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_20min_fetch_fails_keeps_fetched_at(caplog):
    caplog.set_level(logging.INFO)
    redis = TTLFakeRedis()
    ts = datetime.now(timezone.utc) - timedelta(minutes=20)
    await seed(redis, make_quote(QuoteType.OFICIAL, fetched_at=ts))

    quote = await get_usd_quote(
        QuoteType.OFICIAL,
        fetcher=failing_fetcher(RuntimeError("down")),
        redis_client=redis,
    )

    assert quote.stale is True
    assert quote.fetched_at == ts
    events = [
        getattr(r, "event", None)
        for r in caplog.records
        if r.name == "app.services.currency"
    ]
    assert "stale" in events
    assert any("event=stale" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. Hit de >24 h + fetch falla → QuoteUnavailableError.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_over_24h_fetch_fails_raises():
    redis = TTLFakeRedis()
    ts = datetime.now(timezone.utc) - timedelta(hours=25)
    await seed(redis, make_quote(QuoteType.OFICIAL, fetched_at=ts))

    with pytest.raises(QuoteUnavailableError):
        await get_usd_quote(
            QuoteType.OFICIAL,
            fetcher=failing_fetcher(RuntimeError("down")),
            redis_client=redis,
        )


# ---------------------------------------------------------------------------
# 5. Miss + fetch OK → set con ex=86400.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_miss_sets_ttl_86400():
    redis = TTLFakeRedis()

    quote = await get_usd_quote(
        QuoteType.OFICIAL, fetcher=ok_fetcher(), redis_client=redis
    )

    assert quote.stale is False
    assert any(
        key == currency._key(QuoteType.OFICIAL) and ex == 86400
        for key, ex in redis.set_calls
    )


# ---------------------------------------------------------------------------
# 6. Concurrencia: dos get_usd_quote simultáneos → fetcher una vez.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_calls_fetch_once():
    redis = TTLFakeRedis()
    calls = {"n": 0}
    gate = asyncio.Event()

    async def slow_fetcher():
        calls["n"] += 1
        await gate.wait()
        return {QuoteType.OFICIAL: make_quote(QuoteType.OFICIAL)}

    async def one():
        return await get_usd_quote(
            QuoteType.OFICIAL, fetcher=slow_fetcher, redis_client=redis
        )

    t1 = asyncio.create_task(one())
    t2 = asyncio.create_task(one())
    await asyncio.sleep(0.05)
    gate.set()
    q1, q2 = await asyncio.gather(t1, t2)

    assert calls["n"] == 1
    assert q1.stale is False
    assert q2.stale is False


# ---------------------------------------------------------------------------
# 7. Mapeo DolarAPI: oficial/bolsa/blue; extra ignoradas.
# ---------------------------------------------------------------------------

def test_dolarapi_maps_oficial_bolsa_blue():
    now = datetime.now(timezone.utc)
    data = [
        {"casa": "oficial", "compra": 1000, "venta": 1010, "fechaActualizacion": now.isoformat()},
        {"casa": "bolsa", "compra": 1200, "venta": 1210},
        {"casa": "blue", "compra": 1300, "venta": 1310},
        {"casa": "contadoconliqui", "compra": 1, "venta": 2},
        {"casa": "cripto", "compra": 1, "venta": 2},
        {"casa": "mayorista", "compra": 1, "venta": 2},
        {"casa": "tarjeta", "compra": 1, "venta": 2},
    ]

    quotes = currency._parse_dolarapi(data, now)

    assert set(quotes) == {QuoteType.OFICIAL, QuoteType.MEP, QuoteType.BLUE}
    assert quotes[QuoteType.MEP].venta == Decimal("1210")
    assert quotes[QuoteType.BLUE].compra == Decimal("1300")


# ---------------------------------------------------------------------------
# 8. compra ausente → None; venta válida.
# ---------------------------------------------------------------------------

def test_compra_missing_is_none():
    now = datetime.now(timezone.utc)

    quotes = currency._parse_dolarapi([{"casa": "oficial", "venta": 1010}], now)

    assert quotes[QuoteType.OFICIAL].compra is None
    assert quotes[QuoteType.OFICIAL].venta == Decimal("1010")


# ---------------------------------------------------------------------------
# 9. compra > venta → compra=None (no se inventa).
# ---------------------------------------------------------------------------

def test_compra_gt_venta_is_none():
    now = datetime.now(timezone.utc)

    quotes = currency._parse_dolarapi(
        [{"casa": "oficial", "compra": 9999, "venta": 1010}], now
    )

    assert quotes[QuoteType.OFICIAL].compra is None


# ---------------------------------------------------------------------------
# 10. Non-200 / JSON inválido → error (parse) o stale (fallback).
# ---------------------------------------------------------------------------

def test_invalid_json_raises():
    now = datetime.now(timezone.utc)

    with pytest.raises(QuoteUnavailableError):
        currency._parse_dolarapi({"casa": "oficial"}, now)  # no es lista
    with pytest.raises(QuoteUnavailableError):
        currency._parse_dolarapi([], now)  # lista vacía
    with pytest.raises(QuoteUnavailableError):
        currency._parse_dolarapi([{"casa": "desconocida", "venta": 10}], now)


@pytest.mark.asyncio
async def test_non_200_stale_hit_serves_stale():
    redis = TTLFakeRedis()
    ts = datetime.now(timezone.utc) - timedelta(minutes=20)
    await seed(redis, make_quote(QuoteType.OFICIAL, fetched_at=ts))

    quote = await get_usd_quote(
        QuoteType.OFICIAL,
        fetcher=failing_fetcher(QuoteUnavailableError("non-200")),
        redis_client=redis,
    )

    assert quote.stale is True


# ---------------------------------------------------------------------------
# 11. reset_currency_service limpia locks/cliente sin aclose.
# ---------------------------------------------------------------------------

def test_reset_clears_state_without_aclose():
    fake = TTLFakeRedis()
    currency._redis_client = fake
    currency._redis_loop_id = 123
    currency._locks[QuoteType.OFICIAL] = asyncio.Lock()

    reset_currency_service()

    assert currency._redis_client is None
    assert currency._redis_loop_id is None
    assert currency._locks == {}
    assert fake._closed is False


# ---------------------------------------------------------------------------
# 12. TTL desde env; valores inválidos/negativos → defaults.
# ---------------------------------------------------------------------------

def test_ttl_from_env_and_defaults(monkeypatch):
    assert currency._stale_seconds() == 86400
    assert currency._cache_ttl_seconds() == 900

    monkeypatch.setenv("CURRENCY_STALE_SECONDS", "3600")
    monkeypatch.setenv("CURRENCY_CACHE_TTL_SECONDS", "60")
    assert currency._stale_seconds() == 3600
    assert currency._cache_ttl_seconds() == 60

    monkeypatch.setenv("CURRENCY_STALE_SECONDS", "abc")
    monkeypatch.setenv("CURRENCY_CACHE_TTL_SECONDS", "-1")
    assert currency._stale_seconds() == 86400
    assert currency._cache_ttl_seconds() == 900


# ---------------------------------------------------------------------------
# Extras exigidos por las reglas de caché.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_error_falls_back_to_fetch():
    class BrokenRedis:
        async def get(self, key):
            raise currency.redis.RedisError("down")

        async def set(self, key, value, *, ex=None, **kwargs):
            raise currency.redis.RedisError("down")

    calls = {"n": 0}

    async def fetcher():
        calls["n"] += 1
        return {QuoteType.OFICIAL: make_quote(QuoteType.OFICIAL)}

    quote = await get_usd_quote(
        QuoteType.OFICIAL, fetcher=fetcher, redis_client=BrokenRedis()
    )

    assert quote.stale is False
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_no_redis_url_direct_fetch(monkeypatch, caplog):
    monkeypatch.delenv("REDIS_URL", raising=False)
    caplog.set_level(logging.WARNING)
    calls = {"n": 0}

    async def fetcher():
        calls["n"] += 1
        return {QuoteType.OFICIAL: make_quote(QuoteType.OFICIAL)}

    quote = await get_usd_quote(QuoteType.OFICIAL, fetcher=fetcher)

    assert quote.stale is False
    assert calls["n"] == 1
    assert any("REDIS_URL" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Review fixes: REDIS_URL inválida, _fetch_dolarapi, get_usd_quotes y caché corrupto.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_redis_url_falls_back_to_fetch(monkeypatch, caplog):
    # El autouse de conftest patchea redis.asyncio.from_url globalmente; usamos el real
    # para ejercitar el ValueError de una URL malformada.
    from redis.asyncio.utils import from_url as real_redis_from_url

    monkeypatch.setattr(currency.redis, "from_url", real_redis_from_url)
    monkeypatch.setenv("REDIS_URL", "not-a-url")
    caplog.set_level(logging.WARNING)
    calls = {"n": 0}

    async def fetcher():
        calls["n"] += 1
        return {QuoteType.OFICIAL: make_quote(QuoteType.OFICIAL)}

    quote = await get_usd_quote(QuoteType.OFICIAL, fetcher=fetcher)

    assert quote.stale is False
    assert calls["n"] == 1
    assert any("REDIS_URL" in r.getMessage() for r in caplog.records)


def _mock_dolarapi(monkeypatch, handler):
    """Parchea httpx.AsyncClient para inyectar un MockTransport (sin red)."""
    captured = {}
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(currency.httpx, "AsyncClient", factory)
    return captured


@pytest.mark.asyncio
async def test_fetch_dolarapi_non_200_raises(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(500, json={"error": "boom"})

    captured = _mock_dolarapi(monkeypatch, handler)

    with pytest.raises(QuoteUnavailableError):
        await currency._fetch_dolarapi()

    assert seen["url"] == "https://dolarapi.com/v1/dolares"
    assert captured["timeout"] == 3.0


@pytest.mark.asyncio
async def test_fetch_dolarapi_invalid_json_raises(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"not json")

    _mock_dolarapi(monkeypatch, handler)

    with pytest.raises(QuoteUnavailableError):
        await currency._fetch_dolarapi()


@pytest.mark.asyncio
async def test_get_usd_quotes_empty_means_all_three():
    redis = TTLFakeRedis()
    calls = {"n": 0}

    async def fetcher():
        calls["n"] += 1
        return {t: make_quote(t) for t in QuoteType}

    quotes = await get_usd_quotes([], fetcher=fetcher, redis_client=redis)

    assert set(quotes) == {QuoteType.OFICIAL, QuoteType.MEP, QuoteType.BLUE}
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_get_usd_quotes_partial_failure_returns_available():
    redis = TTLFakeRedis()

    async def fetcher():
        return {QuoteType.OFICIAL: make_quote(QuoteType.OFICIAL)}

    quotes = await get_usd_quotes(fetcher=fetcher, redis_client=redis)

    assert set(quotes) == {QuoteType.OFICIAL}


@pytest.mark.asyncio
async def test_get_usd_quotes_all_fail_raises():
    redis = TTLFakeRedis()

    async def fetcher():
        raise RuntimeError("provider down")

    with pytest.raises(QuoteUnavailableError):
        await get_usd_quotes(fetcher=fetcher, redis_client=redis)


@pytest.mark.asyncio
async def test_corrupt_cache_json_falls_back_to_fetch(caplog):
    caplog.set_level(logging.WARNING)
    redis = TTLFakeRedis()
    await redis.set(currency._key(QuoteType.OFICIAL), "{not json")
    calls = {"n": 0}

    async def fetcher():
        calls["n"] += 1
        return {QuoteType.OFICIAL: make_quote(QuoteType.OFICIAL)}

    quote = await get_usd_quote(QuoteType.OFICIAL, fetcher=fetcher, redis_client=redis)

    assert quote.stale is False
    assert calls["n"] == 1
    assert any("corrupt" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cache_missing_fields_falls_back_to_fetch(caplog):
    caplog.set_level(logging.WARNING)
    redis = TTLFakeRedis()
    await redis.set(currency._key(QuoteType.OFICIAL), json.dumps({"tipo": "oficial"}))
    calls = {"n": 0}

    async def fetcher():
        calls["n"] += 1
        return {QuoteType.OFICIAL: make_quote(QuoteType.OFICIAL, venta=Decimal("1700.00"))}

    quote = await get_usd_quote(QuoteType.OFICIAL, fetcher=fetcher, redis_client=redis)

    assert quote.venta == Decimal("1700.00")
    assert calls["n"] == 1
    assert any("corrupt" in r.getMessage() for r in caplog.records)

