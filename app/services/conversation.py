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
class ConversationState:
    """Estado de conversación de un usuario."""
    # step puede ser: "none" | "awaiting_category_confirmation" | "awaiting_reminder_data"
    step: str
    pending_movement: PendingMovement | None = None
    pending_reminder: PendingReminder | None = None

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "pending_movement": self.pending_movement.to_dict() if self.pending_movement else None,
            "pending_reminder": self.pending_reminder.to_dict() if self.pending_reminder else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationState":
        pm = None
        if d.get("pending_movement"):
            pm = PendingMovement.from_dict(d["pending_movement"])
        pr = None
        if d.get("pending_reminder"):
            pr = PendingReminder.from_dict(d["pending_reminder"])
        return cls(step=d.get("step", "none"), pending_movement=pm, pending_reminder=pr)

    @classmethod
    def empty(cls) -> "ConversationState":
        return cls(step="none", pending_movement=None, pending_reminder=None)


@dataclass
class LastRegisteredMovement:
    """
    Datos del último movimiento registrado, para permitir cambio de categoría
    sin necesidad de un diálogo de confirmación previo (STK-39 v2).
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


# ---------------------------------------------------------------------------
# Keys y TTL
# ---------------------------------------------------------------------------

CONVERSATION_TTL = timedelta(minutes=30)
LAST_MOVEMENT_TTL = timedelta(minutes=60)


def _key(whatsapp_id: str) -> str:
    return f"conversation:{whatsapp_id}"


def _last_movement_key(whatsapp_id: str) -> str:
    return f"last_movement:{whatsapp_id}"


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

    # ------------------------------------------------------------------
    # STK-39 v2: Último movimiento registrado (para cambio de categoría)
    # ------------------------------------------------------------------

    @classmethod
    async def set_last_movement(cls, whatsapp_id: str, movement: LastRegisteredMovement) -> None:
        """Guarda el último movimiento registrado para permitir cambio de categoría."""
        try:
            client = await cls._get_client()
            raw = json.dumps(movement.to_dict())
            await client.setex(_last_movement_key(whatsapp_id), LAST_MOVEMENT_TTL, raw)
        except Exception as exc:
            print(f"[ConversationService] set_last_movement error: {type(exc).__name__}: {exc}")

    @classmethod
    async def get_last_movement(cls, whatsapp_id: str) -> LastRegisteredMovement | None:
        """Obtiene el último movimiento registrado."""
        try:
            client = await cls._get_client()
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
            await client.delete(_last_movement_key(whatsapp_id))
        except Exception as exc:
            print(f"[ConversationService] clear_last_movement error: {type(exc).__name__}: {exc}")

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