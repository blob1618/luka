"""Tests for FakeRedis and test isolation fixtures (STK-220)."""

import socket
import pytest

from app.services.conversation import ConversationService
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
