"""Global test fixtures and isolation for STK-220."""

from datetime import timedelta
import sys
from typing import Any
import pytest

from app.services.conversation import ConversationService
import app.main


# ---------------------------------------------------------------------------
# Network Guard
# ---------------------------------------------------------------------------

import socket
import threading

_tls = threading.local()
_orig_socketpair = getattr(socket, "socketpair", None)

if _orig_socketpair is not None:
    def _safe_socketpair(*args, **kwargs):
        _tls.in_socketpair = True
        try:
            return _orig_socketpair(*args, **kwargs)
        finally:
            _tls.in_socketpair = False

    socket.socketpair = _safe_socketpair


def _network_guard(event: str, args: tuple) -> None:
    if event in ("socket.connect", "socket.sendto"):
        if getattr(_tls, "in_socketpair", False):
            return
        address = args[1] if len(args) > 1 else "unknown"
        raise RuntimeError(
            f"Accidental network/socket connection attempted to {address}. "
            "Unit tests must not perform real network or Redis calls; use mocks or fakes."
        )


sys.addaudithook(_network_guard)




# ---------------------------------------------------------------------------
# FakeRedis
# ---------------------------------------------------------------------------

class FakeRedis:
    """Fake de cliente Redis en memoria, asíncrono, determinista y aislado por instancia."""

    def __init__(self):
        self._storage: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self._storage.get(str(key))

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ex: int | timedelta | None = None,
        px: int | timedelta | None = None,
        nx: bool = False,
        xx: bool = False,
        **kwargs: Any,
    ) -> bool | None:
        del ex, px, kwargs
        key_str = str(key)
        if nx and key_str in self._storage:
            return None
        if xx and key_str not in self._storage:
            return None
        self._storage[key_str] = str(value) if not isinstance(value, str) else value
        return True

    async def setex(
        self,
        key: str,
        time: int | timedelta,
        value: Any,
    ) -> bool:
        del time
        key_str = str(key)
        self._storage[key_str] = str(value) if not isinstance(value, str) else value
        return True

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if self._storage.pop(str(k), None) is not None:
                count += 1
        return count

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
    ) -> int:
        del numkeys
        if not keys_and_args:
            return 0
        key = str(keys_and_args[0])
        expected = str(keys_and_args[1]) if len(keys_and_args) > 1 else ""
        current = self._storage.get(key)
        if current != expected:
            return 0
        if "'completed'" in script:
            self._storage[key] = "completed"
            return 1
        self._storage.pop(key, None)
        return 1

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis() -> FakeRedis:
    """Provee una instancia limpia de FakeRedis para un test."""
    return FakeRedis()


@pytest.fixture(autouse=True)
def isolate_redis_and_conversation(monkeypatch, fake_redis: FakeRedis):
    """Aisla ConversationService y Redis para cada prueba."""
    ConversationService._client = None
    ConversationService._loop_id = None
    ConversationService.reset_cache()

    # Redirigir la creación de clientes en ConversationService al fake
    def _fake_from_url(*args, **kwargs):
        return fake_redis

    monkeypatch.setattr("app.services.conversation.redis.from_url", _fake_from_url)
    monkeypatch.setattr(app.main, "redis_client", fake_redis)

    yield fake_redis

    ConversationService._client = None
    ConversationService._loop_id = None
    ConversationService.reset_cache()
