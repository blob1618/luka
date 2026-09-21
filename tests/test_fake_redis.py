"""Tests for FakeRedis and test isolation fixtures."""

import json
import socket
import pytest

from app.services.conversation import ConversationService, _MEMORY_APPEND_SCRIPT
from tests.conftest import FakeRedis

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_fake_redis_ping():
    fake = FakeRedis()
    assert await fake.ping() is True


@pytest.mark.asyncio
async def test_fake_redis_get_set_delete():
    fake = FakeRedis()
    assert await fake.get("key1") is None

    # Set and get
    assert await fake.set("key1", "val1") is True
    assert await fake.get("key1") == "val1"

    # Set with nx
    assert await fake.set("key1", "val2", nx=True) is None
    assert await fake.get("key1") == "val1"
    assert await fake.set("key2", "val2", nx=True) is True
    assert await fake.get("key2") == "val2"

    # Delete
    assert await fake.delete("key1", "key3") == 1
    assert await fake.get("key1") is None


@pytest.mark.asyncio
async def test_fake_redis_setex():
    fake = FakeRedis()
    assert await fake.setex("key1", 60, "val1") is True
    assert await fake.get("key1") == "val1"


@pytest.mark.asyncio
async def test_fake_redis_list_operations():
    fake = FakeRedis()
    assert await fake.lrange("list", 0, -1) == []
    assert await fake.rpush("list", "a", "b", "c") == 3
    assert await fake.lrange("list", 0, -1) == ["a", "b", "c"]
    assert await fake.lrange("list", -2, -1) == ["b", "c"]
    assert await fake.lrange("list", 1, 1) == ["b"]
    assert await fake.ltrim("list", -2, -1) is True
    assert await fake.lrange("list", 0, -1) == ["b", "c"]

    assert await fake.ltrim("list", 5, 10) is True
    assert await fake.lrange("list", 0, -1) == []


@pytest.mark.asyncio
async def test_fake_redis_expire():
    fake = FakeRedis()
    assert await fake.expire("missing", 60) is False

    await fake.set("key1", "val1")
    assert await fake.expire("key1", 60) is True
    assert fake._expirations["key1"] == 60

    await fake.delete("key1")
    assert "key1" not in fake._expirations


@pytest.mark.asyncio
async def test_fake_redis_eval_conversation_memory_dedup_and_trim():
    fake = FakeRedis()
    key = "conversation_memory:whatsapp:5491100000001"
    script = _MEMORY_APPEND_SCRIPT
    payload1 = json.dumps(
        {"user": {"id": "wamid-1", "content": "hola"}, "assistant": {"content": "buenas"}}
    )
    payload2 = json.dumps(
        {"user": {"id": "wamid-2", "content": "chau"}, "assistant": {"content": ""}}
    )
    payload3 = json.dumps(
        {"user": {"id": None, "content": "sin id"}, "assistant": {"content": ""}}
    )

    assert await fake.eval(script, 1, key, "wamid-1", payload1, 4, 86400) == 1
    assert await fake.eval(script, 1, key, "wamid-2", payload2, 4, 86400) == 1
    assert await fake.eval(script, 1, key, "wamid-1", payload1, 4, 86400) == 0
    assert await fake.lrange(key, 0, -1) == [payload1, payload2]
    assert fake._expirations[key] == 86400

    assert await fake.eval(script, 1, key, "", payload3, 1, 60) == 1
    assert await fake.lrange(key, 0, -1) == [payload3]
    assert fake._expirations[key] == 60


@pytest.mark.asyncio
async def test_fake_redis_eval_idempotency():
    fake = FakeRedis()
    key = "claim:1"
    token = "token123"

    # Not claimed yet -> eval should return 0
    complete_script = "if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('SET', KEYS[1], 'completed', 'EX', ARGV[2]) return 1 end return 0"
    assert await fake.eval(complete_script, 1, key, token, 3600) == 0

    # Claim
    await fake.set(key, token)
    # Wrong token -> 0
    assert await fake.eval(complete_script, 1, key, "wrong_token", 3600) == 0
    assert await fake.get(key) == token

    # Complete
    assert await fake.eval(complete_script, 1, key, token, 3600) == 1
    assert await fake.get(key) == "completed"

    # Release on another key
    key2 = "claim:2"
    release_script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
    await fake.set(key2, token)
    assert await fake.eval(release_script, 1, key2, token) == 1
    assert await fake.get(key2) is None


@pytest.mark.asyncio
async def test_conversation_service_uses_fake_redis(fake_redis):
    client = await ConversationService._get_client()
    assert client is fake_redis
    await client.set("test_key", "test_val")
    assert await fake_redis.get("test_key") == "test_val"


def test_network_guard_blocks_accidental_socket_connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="Accidental network/socket connection attempted"):
            s.connect(("127.0.0.1", 1))
    finally:
        s.close()
