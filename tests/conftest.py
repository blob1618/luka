"""Global test fixtures and isolation."""

from datetime import timedelta
import json
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
        self._lists: dict[str, list[str]] = {}
        self._expirations: dict[str, int] = {}
        self.eval_calls = 0
        self.get_calls = 0
        self.lrange_calls = 0

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
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
            key_str = str(k)
            removed = self._storage.pop(key_str, None) is not None
            removed = self._lists.pop(key_str, None) is not None or removed
            self._expirations.pop(key_str, None)
            if removed:
                count += 1
        return count

    async def rpush(self, key: str, *values: Any) -> int:
        items = self._lists.setdefault(str(key), [])
        items.extend(
            str(value) if not isinstance(value, str) else value for value in values
        )
        return len(items)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        self.lrange_calls += 1
        items = self._lists.get(str(key), [])
        start, stop = self._bounds(len(items), start, stop)
        if start > stop:
            return []
        return list(items[start : stop + 1])

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        key_str = str(key)
        items = self._lists.get(key_str)
        if items is None:
            return True
        start, stop = self._bounds(len(items), start, stop)
        kept = items[start : stop + 1] if start <= stop else []
        if kept:
            self._lists[key_str] = kept
        else:
            self._lists.pop(key_str, None)
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        key_str = str(key)
        if key_str not in self._storage and key_str not in self._lists:
            return False
        self._expirations[key_str] = int(seconds)
        return True

    @staticmethod
    def _bounds(length: int, start: int, stop: int) -> tuple[int, int]:
        if start < 0:
            start += length
        if stop < 0:
            stop += length
        return max(start, 0), min(stop, length - 1)

    @staticmethod
    def _memory_entry_user_id(entry: str) -> str | None:
        try:
            decoded = json.loads(entry)
        except (TypeError, ValueError):
            return None
        user = decoded.get("user") if isinstance(decoded, dict) else None
        if not isinstance(user, dict):
            return None
        return user.get("id")

    async def _eval_memory_append(self, key: str, args: tuple) -> int:
        message_id = str(args[0]) if len(args) > 0 else ""
        payload = str(args[1]) if len(args) > 1 else ""
        max_turns = int(args[2]) if len(args) > 2 else 4
        ttl_seconds = int(args[3]) if len(args) > 3 else 0
        items = self._lists.setdefault(key, [])
        existing = items[-max_turns:]
        if message_id and any(
            self._memory_entry_user_id(entry) == message_id for entry in existing
        ):
            return 0
        items.append(payload)
        keep = max_turns if max_turns > 0 else len(items)
        del items[: max(0, len(items) - keep)]
        await self.expire(key, ttl_seconds)
        return 1

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
    ) -> int:
        del numkeys
        self.eval_calls += 1
        if not keys_and_args:
            return 0
        key = str(keys_and_args[0])
        if "LRANGE" in script and "RPUSH" in script:
            return await self._eval_memory_append(key, keys_and_args[1:])
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
