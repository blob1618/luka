"""Fake Redis para el eval, aislado del guardia de red de tests/conftest.py."""
import json
from datetime import timedelta
from typing import Any


class FakeRedis:
    """Copia acotada del fake de tests; el eval no importa conftest para no bloquear red."""

    def __init__(self):
        self._storage: dict[str, str] = {}
        self._lists: dict[str, list[str]] = {}
        self._expirations: dict[str, int] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self._storage.get(str(key))

    async def set(self, key: str, value: Any, *, ex=None, px=None, nx=False, xx=False, **kwargs):
        del ex, px, kwargs
        key_str = str(key)
        if nx and key_str in self._storage:
            return None
        if xx and key_str not in self._storage:
            return None
        self._storage[key_str] = str(value) if not isinstance(value, str) else value
        return True

    async def setex(self, key: str, time: int | timedelta, value: Any) -> bool:
        del time
        key_str = str(key)
        self._storage[key_str] = str(value) if not isinstance(value, str) else value
        return True

    async def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            key_str = str(key)
            removed = self._storage.pop(key_str, None) is not None
            removed = self._lists.pop(key_str, None) is not None or removed
            self._expirations.pop(key_str, None)
            if removed:
                count += 1
        return count

    async def rpush(self, key: str, *values: Any) -> int:
        items = self._lists.setdefault(str(key), [])
        items.extend(str(value) if not isinstance(value, str) else value for value in values)
        return len(items)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self._lists.get(str(key), [])
        start, stop = self._bounds(len(items), start, stop)
        if start > stop:
            return []
        return list(items[start:stop + 1])

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        key_str = str(key)
        items = self._lists.get(key_str)
        if items is None:
            return True
        start, stop = self._bounds(len(items), start, stop)
        kept = items[start:stop + 1] if start <= stop else []
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

    async def ttl(self, key: str) -> int:
        if str(key) not in self._storage and str(key) not in self._lists:
            return -2
        return self._expirations.get(str(key), -1)

    @staticmethod
    def _bounds(length: int, start: int, stop: int) -> tuple[int, int]:
        if start < 0:
            start += length
        if stop < 0:
            stop += length
        return max(start, 0), min(stop, length - 1)

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        del numkeys
        if not keys_and_args:
            return 0
        key = str(keys_and_args[0])
        if "LRANGE" in script and "RPUSH" in script:
            return await self._memory_append(key, keys_and_args[1:])
        expected = str(keys_and_args[1]) if len(keys_and_args) > 1 else ""
        current = self._storage.get(key)
        if current != expected:
            return 0
        if "'completed'" in script:
            self._storage[key] = "completed"
            return 1
        self._storage.pop(key, None)
        return 1

    async def _memory_append(self, key: str, args: tuple) -> int:
        message_id = str(args[0]) if len(args) > 0 else ""
        payload = str(args[1]) if len(args) > 1 else ""
        max_turns = int(args[2]) if len(args) > 2 else 4
        ttl_seconds = int(args[3]) if len(args) > 3 else 0
        items = self._lists.setdefault(key, [])
        if message_id and any(
            self._entry_user_id(entry) == message_id for entry in items[-max_turns:]
        ):
            return 0
        items.append(payload)
        keep = max_turns if max_turns > 0 else len(items)
        del items[: max(0, len(items) - keep)]
        await self.expire(key, ttl_seconds)
        return 1

    @staticmethod
    def _entry_user_id(entry: str) -> str | None:
        try:
            decoded = json.loads(entry)
        except (TypeError, ValueError):
            return None
        user = decoded.get("user") if isinstance(decoded, dict) else None
        return user.get("id") if isinstance(user, dict) else None

    async def close(self) -> None:
        pass
