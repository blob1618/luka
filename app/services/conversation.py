"""Redis connection, visible history and published editor navigation state.

Financial tasks live exclusively in assistant_state/OperationService.
"""
import asyncio
import contextvars
import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import timedelta
from typing import Any
import redis.asyncio as redis
from app.services.telemetry import track_phase

CONVERSATION_FLOW_TTL = timedelta(minutes=30)
_state_cache = contextvars.ContextVar('conversation_cache', default=None)


@dataclass(frozen=True)
class PendingConversationFlow:
    flow_id: str
    version_id: str
    event_key: str
    node_id: str
    variables: dict[str, str] = field(default_factory=dict)
    backend_binding: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PendingConversationFlow":
        return cls(
            flow_id=str(data["flow_id"]),
            version_id=str(data["version_id"]),
            event_key=str(data["event_key"]),
            node_id=str(data["node_id"]),
            variables={
                str(key): str(value)
                for key, value in dict(data.get("variables") or {}).items()
            },
            backend_binding=data.get('backend_binding'),
        )

@dataclass(frozen=True)
class ConversationMessage:
    """A bounded, provider-safe message from the recent conversation."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

class ConversationStateUnavailable(RuntimeError):
    pass

def _conversation_flow_key(whatsapp_id: str) -> str:
    return f"conversation_flow:{whatsapp_id}"

def _memory_key(whatsapp_id: str) -> str:
    return f"conversation_memory:whatsapp:{whatsapp_id}"

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default

def _sanitize_history_content(content: str) -> str:
    sanitized = re.sub(r"https?://\S+", "[enlace]", str(content))
    sanitized = re.sub(
        r"(?i)\btoken\s*[=:]\s*\S+",
        "token=[redactado]",
        sanitized,
    )
    return sanitized.strip()[:_env_int("CONVERSATION_MEMORY_MAX_CHARS", 1200)]

def _turn_to_messages(turn: dict) -> list[ConversationMessage]:
    user = turn.get("user") or {}
    user_text = _sanitize_history_content(user.get("content") or "")
    if not user_text:
        return []
    assistant = turn.get("assistant") or {}
    assistant_text = _sanitize_history_content(assistant.get("content") or "")
    messages = [ConversationMessage("user", user_text)]
    if assistant_text:
        messages.append(ConversationMessage("assistant", assistant_text))
    return messages

class ConversationHistoryService:
    """Stores the last visible exchanges as turns in a Redis list."""

    @classmethod
    async def get_recent(
        cls,
        client: Any,
        whatsapp_id: str,
    ) -> list[ConversationMessage]:
        if client is None:
            return []
        try:
            with track_phase("redis"):
                entries = await client.lrange(_memory_key(whatsapp_id), 0, -1)
            messages = []
            for entry in entries or []:
                try:
                    turn = json.loads(entry)
                except (TypeError, ValueError):
                    continue
                messages.extend(_turn_to_messages(turn))
            return messages
        except Exception as exc:
            print(
                "[ConversationHistoryService] get_recent error: "
                f"{type(exc).__name__}: {exc}"
            )
            return []

    @classmethod
    async def append_exchange(
        cls,
        client: Any,
        whatsapp_id: str,
        user_content: str,
        assistant_content: str | None,
        message_id: str | None = None,
    ) -> None:
        if client is None:
            return
        user_text = _sanitize_history_content(user_content)
        if not user_text:
            return
        payload = json.dumps(
            {
                "user": {"id": message_id or None, "content": user_text},
                "assistant": {
                    "content": _sanitize_history_content(assistant_content or ""),
                },
            }
        )
        max_turns = _env_int("CONVERSATION_MEMORY_TURNS", 4)
        ttl_seconds = _env_int("CONVERSATION_MEMORY_TTL_HOURS", 24) * 3600
        try:
            with track_phase("redis"):
                await client.eval(
                    _MEMORY_APPEND_SCRIPT,
                    1,
                    _memory_key(whatsapp_id),
                    message_id or "",
                    payload,
                    max_turns,
                    ttl_seconds,
                )
        except Exception as exc:
            print(
                "[ConversationHistoryService] append_exchange error: "
                f"{type(exc).__name__}: {exc}"
            )

    @classmethod
    async def clear(cls, client: Any, whatsapp_id: str) -> None:
        if client is None:
            return
        try:
            with track_phase("redis"):
                await client.delete(_memory_key(whatsapp_id))
        except Exception as exc:
            print(
                "[ConversationHistoryService] clear error: "
                f"{type(exc).__name__}: {exc}"
            )

_MEMORY_APPEND_SCRIPT = """
local key = KEYS[1]
local message_id = ARGV[1]
local payload = ARGV[2]
local max_turns = tonumber(ARGV[3])
local ttl_seconds = tonumber(ARGV[4])
local existing = redis.call('LRANGE', key, -max_turns, -1)
if message_id ~= '' then
  for _, entry in ipairs(existing) do
    local ok, decoded = pcall(cjson.decode, entry)
if type(decoded) == 'table' and type(decoded['user']) == 'table' and decoded['user']['id'] == message_id then
      return 0
    end
  end
end
redis.call('RPUSH', key, payload)
redis.call('LTRIM', key, -max_turns, -1)
redis.call('EXPIRE', key, ttl_seconds)
return 1
"""

class ConversationService:
    """Own Redis client and published navigation; no financial pending state."""
    _client = None
    _loop_id = None
    _state_cache = _state_cache

    @classmethod
    def reset_cache(cls) -> None:
        """Reinicia la caché de estado de la corrutina/contexto actual."""
        _state_cache.set(None)

    @classmethod
    async def _get_client(cls) -> redis.Redis:
        loop_id = id(asyncio.get_running_loop())
        if cls._client is None or cls._loop_id != loop_id:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            cls._client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            cls._loop_id = loop_id
            try:
                with track_phase("redis"):
                    await cls._client.ping()
            except Exception as e:
                print(f"[ConversationService] Redis ping failed: {e}")
        return cls._client

    @classmethod
    async def get_pending_conversation_flow(
        cls,
        whatsapp_id: str,
    ) -> PendingConversationFlow | None:
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                raw = await client.get(_conversation_flow_key(whatsapp_id))
            if raw is None:
                return None
            return PendingConversationFlow.from_dict(json.loads(raw))
        except Exception as exc:
            raise ConversationStateUnavailable(
                "No se pudo leer el recorrido pendiente."
            ) from exc

    @classmethod
    async def set_pending_conversation_flow(
        cls,
        whatsapp_id: str,
        pending: PendingConversationFlow,
    ) -> None:
        try:
            client = await cls._get_client()
            payload = json.dumps(pending.to_dict())
            with track_phase("redis"):
                stored = await client.set(
                    _conversation_flow_key(whatsapp_id),
                    payload,
                    ex=int(CONVERSATION_FLOW_TTL.total_seconds()),
                )
            if stored is False:
                raise RuntimeError("Redis did not store the conversation flow")
        except Exception as exc:
            raise ConversationStateUnavailable(
                "No se pudo guardar el recorrido pendiente."
            ) from exc

    @classmethod
    async def clear_pending_conversation_flow(cls, whatsapp_id: str) -> None:
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                await client.delete(_conversation_flow_key(whatsapp_id))
        except Exception as exc:
            raise ConversationStateUnavailable(
                "No se pudo limpiar el recorrido pendiente."
            ) from exc
