"""Benchmark del hit de caché de cotizaciones USD (STK-217).

Usa un fetcher fake (sin red real), calienta el caché y mide 1000 llamadas a
`get_usd_quote`; imprime mediana y p95. No es un gate de CI; el AC de <2 ms se
valida localmente.

Uso:
    .venv/bin/python scripts/bench_currency_cache.py
"""

import asyncio
import statistics
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from app.services.currency import QuoteType, UsdQuote, get_usd_quote  # noqa: E402


def make_quote(tipo: QuoteType) -> UsdQuote:
    now = datetime.now(timezone.utc)
    return UsdQuote(
        tipo=tipo,
        compra=Decimal("1500.00"),
        venta=Decimal("1550.00"),
        fuente="DolarAPI",
        as_of=now,
        fetched_at=now,
        stale=False,
    )


class FakeRedis:
    """Cliente Redis fake mínimo (solo lo que usa el benchmark)."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(str(key))

    async def set(self, key: str, value, *, ex=None, **kwargs) -> bool:
        self._store[str(key)] = str(value) if not isinstance(value, str) else value
        return True

    async def delete(self, *keys: str) -> int:
        for k in keys:
            self._store.pop(str(k), None)
        return len(keys)


async def fetcher() -> dict[QuoteType, UsdQuote]:
    return {t: make_quote(t) for t in QuoteType}


async def main() -> None:
    redis = FakeRedis()

    # Calentamiento: primer fetch puebla el caché.
    await get_usd_quote(QuoteType.OFICIAL, fetcher=fetcher, redis_client=redis)

    latencies: list[float] = []
    for _ in range(1000):
        t0 = time.perf_counter()
        await get_usd_quote(QuoteType.OFICIAL, fetcher=fetcher, redis_client=redis)
        latencies.append((time.perf_counter() - t0) * 1000)

    latencies.sort()
    median = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    print(f"samples={len(latencies)}")
    print(f"median={median:.3f} ms")
    print(f"p95={p95:.3f} ms")


if __name__ == "__main__":
    asyncio.run(main())
