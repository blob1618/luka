"""
Conversation state management for multi-turn WhatsApp interactions.

Uses Redis to track pending movements and dialog steps per user,
enabling the category confirmation flow (STK-39).
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

import redis.asyncio as redis

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
class ConversationState:
    """Estado de conversación de un usuario."""
    step: str                     # "none" | "awaiting_category_confirmation" | "awaiting_category_name"
    pending_movement: PendingMovement | None = None

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "pending_movement": self.pending_movement.to_dict() if self.pending_movement else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationState":
        pm = None
        if d.get("pending_movement"):
            pm = PendingMovement.from_dict(d["pending_movement"])
        return cls(step=d.get("step", "none"), pending_movement=pm)

    @classmethod
    def empty(cls) -> "ConversationState":
        return cls(step="none", pending_movement=None)


# ---------------------------------------------------------------------------
# Keys y TTL
# ---------------------------------------------------------------------------

CONVERSATION_TTL = timedelta(minutes=30)


def _key(whatsapp_id: str) -> str:
    return f"conversation:{whatsapp_id}"


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------


class ConversationService:
    """Maneja el estado de conversación multi-turno vía Redis."""

    _client: redis.Redis | None = None

    @classmethod
    async def _get_client(cls) -> redis.Redis:
        if cls._client is None:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            cls._client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            try:
                await cls._client.ping()
            except Exception as e:
                print(f"[ConversationService] Redis ping failed: {e}")
        return cls._client

    @classmethod
    async def get_state(cls, whatsapp_id: str) -> ConversationState:
        """Recupera el estado de conversación de un usuario."""
        try:
            client = await cls._get_client()
            raw = await client.get(_key(whatsapp_id))
            if raw is None:
                return ConversationState.empty()
            d = json.loads(raw)
            return ConversationState.from_dict(d)
        except Exception as exc:
            print(f"[ConversationService] get_state error: {type(exc).__name__}: {exc}")
            return ConversationState.empty()

    @classmethod
    async def set_state(cls, whatsapp_id: str, state: ConversationState) -> None:
        """Persiste el estado de conversación con TTL."""
        try:
            client = await cls._get_client()
            raw = json.dumps(state.to_dict())
            await client.setex(_key(whatsapp_id), CONVERSATION_TTL, raw)
        except Exception as exc:
            print(f"[ConversationService] set_state error: {type(exc).__name__}: {exc}")

    @classmethod
    async def clear_state(cls, whatsapp_id: str) -> None:
        """Elimina el estado de conversación."""
        try:
            client = await cls._get_client()
            await client.delete(_key(whatsapp_id))
        except Exception as exc:
            print(f"[ConversationService] clear_state error: {type(exc).__name__}: {exc}")

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