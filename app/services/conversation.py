"""
Conversation state management for multi-turn WhatsApp interactions.

Uses Redis to track pending movements and dialog steps per user,
enabling the category confirmation flow.
"""

import asyncio
import contextvars
import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

import redis.asyncio as redis

from app.services.telemetry import track_phase

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PendingMovement:
    """Datos de un movimiento financiero pendiente de confirmación de categoría."""
    sender_phone: str
    whatsapp_message_id: str | None
    original_text: str
    movement_type: str          # "ingreso" | "egreso"
    amount: Decimal
    currency: str
    description: str
    inferred_category: str | None
    llm_result_extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = str(d["amount"])  # Decimal → str para JSON
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PendingMovement":
        raw = dict(d)
        raw["amount"] = Decimal(str(raw["amount"]))
        return cls(**raw)


@dataclass
class PendingReminder:
    """Datos parciales de un recordatorio pendiente de completar (multi-turno)."""
    sender_phone: str
    reminder_concept: str | None
    reminder_day: int | None
    reminder_amount: Decimal | None
    reminder_currency: str = "ARS"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reminder_amount"] = str(d["reminder_amount"]) if d["reminder_amount"] is not None else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PendingReminder":
        raw = dict(d)
        if raw.get("reminder_amount") is not None:
            raw["reminder_amount"] = Decimal(str(raw["reminder_amount"]))
        return cls(**raw)


@dataclass
class PendingLimit:
    """Datos parciales de un límite de gasto pendiente de completar/confirmar (multi-turno)."""
    sender_phone: str
    category: str | None
    amount: Decimal | None
    month: int | None
    year: int | None
    currency: str = "ARS"
    is_edit: bool = False
    limit_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = str(d["amount"]) if d["amount"] is not None else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PendingLimit":
        raw = dict(d)
        raw["amount"] = Decimal(str(raw["amount"])) if raw.get("amount") is not None else None
        return cls(**raw)


@dataclass
class PendingCompensation:
    """Propuesta de compensación de presupuesto pendiente de confirmar (multi-turno)."""
    sender_phone: str
    proposal: dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingCompensation":
        return cls(**dict(d))


@dataclass
class PendingMovementChart:
    """Chart request waiting for a validated choice or missing detail."""

    sender_phone: str
    request: dict = field(default_factory=dict)
    reason: str = ""
    question: str = ""
    options: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingMovementChart":
        return cls(**dict(d))


@dataclass
class LastCreatedLimit:
    """
    Datos del último límite creado, para permitir editarlo sin diálogo previo
    ("¿No te convence algo? Indícame y lo cambiamos.").
    """
    limit_id: str
    sender_phone: str
    category_name: str
    amount: Decimal
    month: int
    year: int
    currency: str = "ARS"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = str(d["amount"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LastCreatedLimit":
        raw = dict(d)
        raw["amount"] = Decimal(str(raw["amount"]))
        return cls(**raw)


@dataclass
class PendingLimitDelete:
    """Contexto de una eliminación de límite cuando hay que elegir el mes."""
    sender_phone: str
    category_name: str | None
    candidates: list[dict] = field(default_factory=list)
    month: int | None = None
    year: int | None = None
    currency: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candidates"] = [
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in c.items()}
            for c in self.candidates
        ]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PendingLimitDelete":
        return cls(**dict(d))


@dataclass
class ConversationState:
    """Estado de conversación de un usuario."""
    # step puede ser: "none" | "awaiting_category_confirmation" | "awaiting_reminder_data"
    #               | "awaiting_limit_year_confirmation" | "awaiting_limit_category_confirmation"
    #               | "awaiting_limit_data"
    #               | "awaiting_limit_month_selection" | "awaiting_limit_delete_category"
    #               | "awaiting_compensation_confirmation"
    step: str
    pending_movement: PendingMovement | None = None
    pending_reminder: PendingReminder | None = None
    pending_limit: PendingLimit | None = None
    pending_limit_delete: PendingLimitDelete | None = None
    pending_compensation: PendingCompensation | None = None
    pending_movement_chart: PendingMovementChart | None = None

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "pending_movement": self.pending_movement.to_dict() if self.pending_movement else None,
            "pending_reminder": self.pending_reminder.to_dict() if self.pending_reminder else None,
            "pending_limit": self.pending_limit.to_dict() if self.pending_limit else None,
            "pending_limit_delete": self.pending_limit_delete.to_dict() if self.pending_limit_delete else None,
            "pending_compensation": self.pending_compensation.to_dict() if self.pending_compensation else None,
            "pending_movement_chart": (
                self.pending_movement_chart.to_dict()
                if self.pending_movement_chart else None
            ),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationState":
        pm = None
        if d.get("pending_movement"):
            pm = PendingMovement.from_dict(d["pending_movement"])
        pr = None
        if d.get("pending_reminder"):
            pr = PendingReminder.from_dict(d["pending_reminder"])
        pl = None
        if d.get("pending_limit"):
            pl = PendingLimit.from_dict(d["pending_limit"])
        pld = None
        if d.get("pending_limit_delete"):
            pld = PendingLimitDelete.from_dict(d["pending_limit_delete"])
        pc = None
        if d.get("pending_compensation"):
            pc = PendingCompensation.from_dict(d["pending_compensation"])
        pmc = None
        if d.get("pending_movement_chart"):
            pmc = PendingMovementChart.from_dict(d["pending_movement_chart"])
        return cls(
            step=d.get("step", "none"),
            pending_movement=pm,
            pending_reminder=pr,
            pending_limit=pl,
            pending_limit_delete=pld,
            pending_compensation=pc,
            pending_movement_chart=pmc,
        )

    @classmethod
    def empty(cls) -> "ConversationState":
        return cls(
            step="none",
            pending_movement=None,
            pending_reminder=None,
            pending_limit=None,
            pending_limit_delete=None,
            pending_compensation=None,
            pending_movement_chart=None,
        )


@dataclass(frozen=True)
class PendingConversationFlow:
    flow_id: str
    version_id: str
    event_key: str
    node_id: str
    variables: dict[str, str] = field(default_factory=dict)

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
        )

@dataclass
class LastRegisteredMovement:
    """
    Datos del último movimiento registrado, para permitir cambio de categoría
    sin necesidad de un diálogo de confirmación previo.
    """
    movement_id: str
    sender_phone: str
    movement_type: str
    amount: Decimal
    currency: str
    description: str
    category_name: str | None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = str(d["amount"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LastRegisteredMovement":
        raw = dict(d)
        raw["amount"] = Decimal(str(raw["amount"]))
        return cls(**raw)


@dataclass
class RecentItems:
    """Bounded references to objects displayed or created in the last exchange."""

    entity: str
    items: list[dict[str, Any]]


@dataclass
class PendingSelection:
    """An operation waiting for the user to choose one or more shown IDs."""

    intent: str
    entity: str
    items: list[dict[str, Any]]
    changes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationMessage:
    """A bounded, provider-safe message from the recent conversation."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


# ---------------------------------------------------------------------------
# Keys y TTL
# ---------------------------------------------------------------------------

CONVERSATION_TTL = timedelta(minutes=30)
LAST_MOVEMENT_TTL = timedelta(minutes=60)
LAST_LIMIT_TTL = timedelta(minutes=60)
CONVERSATION_FLOW_TTL = timedelta(minutes=30)

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


class ConversationStateUnavailable(RuntimeError):
    pass


def _key(whatsapp_id: str) -> str:
    return f"conversation:{whatsapp_id}"


def _last_movement_key(whatsapp_id: str) -> str:
    return f"last_movement:{whatsapp_id}"


def _recent_items_key(whatsapp_id: str) -> str:
    return f"recent_items:{whatsapp_id}"


def _pending_selection_key(whatsapp_id: str) -> str:
    return f"pending_selection:{whatsapp_id}"


def _last_limit_key(whatsapp_id: str) -> str:
    return f"last_limit:{whatsapp_id}"


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


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------


_state_cache: contextvars.ContextVar[dict[str, ConversationState] | None] = (
    contextvars.ContextVar("conversation_state_cache", default=None)
)


class ConversationService:
    """Maneja el estado de conversación multi-turno vía Redis."""

    _client: redis.Redis | None = None
    _loop_id: int | None = None
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
    async def get_state(cls, whatsapp_id: str) -> ConversationState:
        """Recupera el estado de conversación de un usuario."""
        cache = _state_cache.get()
        if cache is not None and whatsapp_id in cache:
            return cache[whatsapp_id]

        try:
            client = await cls._get_client()
            with track_phase("redis"):
                raw = await client.get(_key(whatsapp_id))
            if raw is None:
                state = ConversationState.empty()
            else:
                d = json.loads(raw)
                state = ConversationState.from_dict(d)

            # Copy-on-write para evitar mutar diccionarios compartidos entre contextos
            new_cache = dict(_state_cache.get() or {})
            new_cache[whatsapp_id] = state
            _state_cache.set(new_cache)

            return state
        except Exception as exc:
            print(f"[ConversationService] get_state error: {type(exc).__name__}: {exc}")
            return ConversationState.empty()

    @classmethod
    async def set_state(cls, whatsapp_id: str, state: ConversationState) -> None:
        """Persiste el estado de conversación con TTL."""
        try:
            client = await cls._get_client()
            raw = json.dumps(state.to_dict())
            with track_phase("redis"):
                await client.setex(_key(whatsapp_id), CONVERSATION_TTL, raw)

            # Actualizar caché SOLO tras persistir con éxito en Redis (Copy-on-write)
            new_cache = dict(_state_cache.get() or {})
            new_cache[whatsapp_id] = state
            _state_cache.set(new_cache)
        except Exception as exc:
            print(f"[ConversationService] set_state error: {type(exc).__name__}: {exc}")

    @classmethod
    async def clear_state(cls, whatsapp_id: str) -> None:
        """Elimina el estado de conversación."""
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                await client.delete(_key(whatsapp_id))

            # Invalidar solo esa entrada SOLO tras eliminar con éxito en Redis (Copy-on-write)
            current_cache = _state_cache.get()
            if current_cache is not None and whatsapp_id in current_cache:
                new_cache = dict(current_cache)
                del new_cache[whatsapp_id]
                _state_cache.set(new_cache)
        except Exception as exc:
            print(f"[ConversationService] clear_state error: {type(exc).__name__}: {exc}")

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

    @classmethod
    async def set_pending_movement(cls, whatsapp_id: str, pending: PendingMovement) -> None:
        """Fija el estado en 'awaiting_category_confirmation' con el movimiento pendiente."""
        state = ConversationState(
            step="awaiting_category_confirmation",
            pending_movement=pending,
        )
        await cls.set_state(whatsapp_id, state)

    @classmethod
    async def is_awaiting_category_confirmation(cls, whatsapp_id: str) -> bool:
        """Consulta si el usuario está esperando confirmar una categoría."""
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_category_confirmation"

    @classmethod
    async def get_pending_movement(cls, whatsapp_id: str) -> PendingMovement | None:
        """Obtiene el movimiento pendiente si existe."""
        state = await cls.get_state(whatsapp_id)
        return state.pending_movement

    # ------------------------------------------------------------------
    # Último movimiento registrado (para cambio de categoría)
    # ------------------------------------------------------------------

    @classmethod
    async def set_last_movement(cls, whatsapp_id: str, movement: LastRegisteredMovement) -> None:
        """Guarda el último movimiento registrado para permitir cambio de categoría."""
        try:
            client = await cls._get_client()
            raw = json.dumps(movement.to_dict())
            with track_phase("redis"):
                await client.setex(_last_movement_key(whatsapp_id), LAST_MOVEMENT_TTL, raw)
        except Exception as exc:
            print(f"[ConversationService] set_last_movement error: {type(exc).__name__}: {exc}")

    @classmethod
    async def get_last_movement(cls, whatsapp_id: str) -> LastRegisteredMovement | None:
        """Obtiene el último movimiento registrado."""
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                raw = await client.get(_last_movement_key(whatsapp_id))
            if raw is None:
                return None
            d = json.loads(raw)
            return LastRegisteredMovement.from_dict(d)
        except Exception as exc:
            print(f"[ConversationService] get_last_movement error: {type(exc).__name__}: {exc}")
            return None

    @classmethod
    async def clear_last_movement(cls, whatsapp_id: str) -> None:
        """Elimina el último movimiento registrado."""
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                await client.delete(_last_movement_key(whatsapp_id))
        except Exception as exc:
            print(f"[ConversationService] clear_last_movement error: {type(exc).__name__}: {exc}")

    @classmethod
    async def set_recent_items(cls, whatsapp_id: str, recent: RecentItems) -> None:
        try:
            client = await cls._get_client()
            payload = json.dumps(asdict(recent))
            with track_phase("redis"):
                await client.setex(
                    _recent_items_key(whatsapp_id), CONVERSATION_TTL,
                    payload,
                )
        except Exception as exc:
            print(f"[ConversationService] set_recent_items error: {type(exc).__name__}: {exc}")

    @classmethod
    async def get_recent_items(cls, whatsapp_id: str) -> RecentItems | None:
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                raw = await client.get(_recent_items_key(whatsapp_id))
            return RecentItems(**json.loads(raw)) if raw else None
        except Exception as exc:
            print(f"[ConversationService] get_recent_items error: {type(exc).__name__}: {exc}")
            return None

    @classmethod
    async def clear_recent_items(cls, whatsapp_id: str) -> None:
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                await client.delete(_recent_items_key(whatsapp_id))
        except Exception as exc:
            print(f"[ConversationService] clear_recent_items error: {type(exc).__name__}: {exc}")

    @classmethod
    async def set_pending_selection(cls, whatsapp_id: str, pending: PendingSelection) -> None:
        try:
            client = await cls._get_client()
            payload = json.dumps(asdict(pending))
            with track_phase("redis"):
                await client.setex(
                    _pending_selection_key(whatsapp_id), CONVERSATION_TTL,
                    payload,
                )
        except Exception as exc:
            print(f"[ConversationService] set_pending_selection error: {type(exc).__name__}: {exc}")

    @classmethod
    async def get_pending_selection(cls, whatsapp_id: str) -> PendingSelection | None:
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                raw = await client.get(_pending_selection_key(whatsapp_id))
            return PendingSelection(**json.loads(raw)) if raw else None
        except Exception as exc:
            print(f"[ConversationService] get_pending_selection error: {type(exc).__name__}: {exc}")
            return None

    @classmethod
    async def clear_pending_selection(cls, whatsapp_id: str) -> None:
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                await client.delete(_pending_selection_key(whatsapp_id))
        except Exception as exc:
            print(f"[ConversationService] clear_pending_selection error: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Recordatorio pendiente (multi-turno cuando falta el día)
    # ------------------------------------------------------------------

    @classmethod
    async def set_pending_reminder(cls, whatsapp_id: str, pending: PendingReminder) -> None:
        """Fija el estado en 'awaiting_reminder_data' con el recordatorio incompleto."""
        state = ConversationState(
            step="awaiting_reminder_data",
            pending_reminder=pending,
        )
        await cls.set_state(whatsapp_id, state)

    @classmethod
    async def is_awaiting_reminder_data(cls, whatsapp_id: str) -> bool:
        """Consulta si el usuario está en medio de crear un recordatorio (falta info)."""
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_reminder_data"

    @classmethod
    async def get_pending_reminder(cls, whatsapp_id: str) -> PendingReminder | None:
        """Obtiene el recordatorio pendiente de completar."""
        state = await cls.get_state(whatsapp_id)
        return state.pending_reminder

    # ------------------------------------------------------------------
    # Renombrar recordatorio (multi-turno cuando el título ya existe)
    # ------------------------------------------------------------------

    @classmethod
    async def set_pending_rename(cls, whatsapp_id: str, pending: PendingReminder) -> None:
        """Fija el estado en 'awaiting_rename' con los datos del recordatorio original."""
        state = ConversationState(
            step="awaiting_rename",
            pending_reminder=pending,
        )
        await cls.set_state(whatsapp_id, state)

    @classmethod
    async def is_awaiting_rename(cls, whatsapp_id: str) -> bool:
        """Consulta si el usuario está dando un nombre alternativo por título duplicado."""
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_rename"

    @classmethod
    async def get_pending_rename(cls, whatsapp_id: str) -> PendingReminder | None:
        """Obtiene los datos del recordatorio original a renombrar."""
        state = await cls.get_state(whatsapp_id)
        return state.pending_reminder

    # ------------------------------------------------------------------
    # Límites de gasto pendientes
    # ------------------------------------------------------------------

    @classmethod
    async def set_pending_limit(
        cls,
        whatsapp_id: str,
        pending: PendingLimit,
        step: str,
    ) -> None:
        """Fija el estado de límite pendiente con el paso indicado."""
        state = ConversationState(step=step, pending_limit=pending)
        await cls.set_state(whatsapp_id, state)

    @classmethod
    async def get_pending_limit(cls, whatsapp_id: str) -> PendingLimit | None:
        """Obtiene el límite pendiente si existe."""
        state = await cls.get_state(whatsapp_id)
        return state.pending_limit

    @classmethod
    async def is_awaiting_limit_year_confirmation(cls, whatsapp_id: str) -> bool:
        """El usuario debe confirmar si aplica el límite al año siguiente."""
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_limit_year_confirmation"

    @classmethod
    async def is_awaiting_limit_category_confirmation(cls, whatsapp_id: str) -> bool:
        """El usuario debe confirmar la creación de una categoría canónica."""
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_limit_category_confirmation"

    @classmethod
    async def is_awaiting_limit_data(cls, whatsapp_id: str) -> bool:
        """El usuario debe completar categoría y/o monto del límite."""
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_limit_data"

    @classmethod
    async def is_awaiting_limit_month_selection(cls, whatsapp_id: str) -> bool:
        """El usuario debe elegir a qué mes de límite se refiere (delete)."""
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_limit_month_selection"

    @classmethod
    async def set_pending_limit_delete(
        cls,
        whatsapp_id: str,
        pending: PendingLimitDelete,
    ) -> None:
        """Fija el estado de selección de mes para eliminar un límite."""
        state = ConversationState(
            step="awaiting_limit_month_selection",
            pending_limit_delete=pending,
        )
        await cls.set_state(whatsapp_id, state)

    @classmethod
    async def set_pending_limit_delete_category(
        cls,
        whatsapp_id: str,
        pending: PendingLimitDelete,
    ) -> None:
        """Fija el estado esperando la categoría del límite a eliminar."""
        state = ConversationState(
            step="awaiting_limit_delete_category",
            pending_limit_delete=pending,
        )
        await cls.set_state(whatsapp_id, state)

    @classmethod
    async def is_awaiting_limit_delete_category(cls, whatsapp_id: str) -> bool:
        """Consulta si el usuario debe indicar la categoría del límite a eliminar."""
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_limit_delete_category"

    @classmethod
    async def get_pending_limit_delete(
        cls,
        whatsapp_id: str,
    ) -> PendingLimitDelete | None:
        """Obtiene el contexto de eliminación pendiente."""
        state = await cls.get_state(whatsapp_id)
        return state.pending_limit_delete

    # ------------------------------------------------------------------
    # Compensación de presupuesto pendiente
    # ------------------------------------------------------------------

    @classmethod
    async def set_pending_compensation(
        cls,
        whatsapp_id: str,
        proposal: dict,
        step: str = "awaiting_compensation_confirmation",
    ) -> None:
        """Fija el estado con la propuesta de compensación pendiente de confirmar."""
        state = ConversationState(
            step=step,
            pending_compensation=PendingCompensation(
                sender_phone=whatsapp_id,
                proposal=proposal,
            ),
        )
        await cls.set_state(whatsapp_id, state)

    @classmethod
    async def is_awaiting_compensation_confirmation(cls, whatsapp_id: str) -> bool:
        """Consulta si el usuario está esperando confirmar una compensación."""
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_compensation_confirmation"

    @classmethod
    async def get_pending_compensation(
        cls,
        whatsapp_id: str,
    ) -> PendingCompensation | None:
        """Obtiene la propuesta de compensación pendiente si existe."""
        state = await cls.get_state(whatsapp_id)
        return state.pending_compensation

    # ------------------------------------------------------------------
    # Grafico de movimientos pendiente
    # ------------------------------------------------------------------

    @classmethod
    async def set_pending_movement_chart(
        cls,
        whatsapp_id: str,
        pending: PendingMovementChart,
    ) -> None:
        state = ConversationState(
            step="awaiting_movement_chart_details",
            pending_movement_chart=pending,
        )
        await cls.set_state(whatsapp_id, state)

    @classmethod
    async def is_awaiting_movement_chart_details(cls, whatsapp_id: str) -> bool:
        state = await cls.get_state(whatsapp_id)
        return state.step == "awaiting_movement_chart_details"

    @classmethod
    async def get_pending_movement_chart(
        cls,
        whatsapp_id: str,
    ) -> PendingMovementChart | None:
        state = await cls.get_state(whatsapp_id)
        return state.pending_movement_chart

    @classmethod
    async def get_last_chart(cls, whatsapp_id: str) -> dict | None:
        try:
            client = await cls._get_client()
            raw = await client.get(f"last_chart:{whatsapp_id}")
            result = json.loads(raw) if raw else None
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    @classmethod
    async def set_last_chart(cls, whatsapp_id: str, request: dict) -> None:
        try:
            client = await cls._get_client()
            await client.setex(f"last_chart:{whatsapp_id}", CONVERSATION_TTL, json.dumps(request))
        except Exception as exc:
            print(f"[ConversationService] chart state unavailable: {type(exc).__name__}")

    @classmethod
    async def clear_last_chart(cls, whatsapp_id: str) -> None:
        try:
            client = await cls._get_client()
            await client.delete(f"last_chart:{whatsapp_id}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Último límite creado (para editarlo sin diálogo previo)
    # ------------------------------------------------------------------

    @classmethod
    async def set_last_limit(cls, whatsapp_id: str, limit: LastCreatedLimit) -> None:
        """Guarda el último límite creado para permitir su edición."""
        try:
            client = await cls._get_client()
            raw = json.dumps(limit.to_dict())
            with track_phase("redis"):
                await client.setex(_last_limit_key(whatsapp_id), LAST_LIMIT_TTL, raw)
        except Exception as exc:
            print(f"[ConversationService] set_last_limit error: {type(exc).__name__}: {exc}")

    @classmethod
    async def get_last_limit(cls, whatsapp_id: str) -> LastCreatedLimit | None:
        """Obtiene el último límite creado."""
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                raw = await client.get(_last_limit_key(whatsapp_id))
            if raw is None:
                return None
            d = json.loads(raw)
            return LastCreatedLimit.from_dict(d)
        except Exception as exc:
            print(f"[ConversationService] get_last_limit error: {type(exc).__name__}: {exc}")
            return None

    @classmethod
    async def clear_last_limit(cls, whatsapp_id: str) -> None:
        """Elimina el último límite creado."""
        try:
            client = await cls._get_client()
            with track_phase("redis"):
                await client.delete(_last_limit_key(whatsapp_id))
        except Exception as exc:
            print(f"[ConversationService] clear_last_limit error: {type(exc).__name__}: {exc}")
