"""Staging-only ingress: outbox de mensajes capturados y endpoints de prueba.

Se habilita solo con `APP_ENV=staging` (o `TEST_INGRESS_ENABLED=1`) y un
`TEST_INGRESS_TOKEN` no vacío. En producción los endpoints responden 404.
"""
from __future__ import annotations

import json
import logging
import os
import secrets

logger = logging.getLogger(__name__)

OUTBOX_TTL_SECONDS = 3600
TEST_TOKEN_HEADER = "x-test-token"


def _staging_enabled() -> bool:
    if os.getenv("APP_ENV", "").strip().lower() == "staging":
        return True
    return os.getenv("TEST_INGRESS_ENABLED", "0").strip() == "1"


def ingress_token() -> str:
    return os.getenv("TEST_INGRESS_TOKEN", "").strip()


def capture_phones() -> set[str]:
    raw = os.getenv("TEST_CAPTURE_PHONES", "")
    return {phone.strip() for phone in raw.split(",") if phone.strip()}


def ingress_enabled() -> bool:
    return bool(ingress_token()) and (_staging_enabled() or bool(capture_phones()))


def capture_enabled() -> bool:
    transport = os.getenv("WHATSAPP_TRANSPORT", "").strip().lower()
    return transport == "capture" and ingress_enabled()


def capture_for_phone(phone: str | None) -> bool:
    """Captura global en staging o solo los teléfonos listados en producción."""
    if capture_enabled():
        return True
    if not phone or not ingress_enabled():
        return False
    return phone.strip() in capture_phones()


def startup_warning() -> str | None:
    """Mensaje para el arranque: deja visible si la captura o el ingreso están activos."""
    if not ingress_enabled():
        return None
    if capture_enabled():
        return "staging_capture_global_activo: las respuestas no se envian por Meta"
    phones = sorted(capture_phones())
    if phones:
        masked = ", ".join(f"***{phone[-4:]}" for phone in phones)
        return f"staging_capture_activo phones={len(phones)} ({masked})"
    return "staging_ingress_activo: endpoints de prueba habilitados sin captura"


def token_valid(supplied: str | None) -> bool:
    token = ingress_token()
    return bool(token) and supplied is not None and secrets.compare_digest(supplied, token)


def outbox_key(phone: str) -> str:
    return f"luka:test:outbox:{phone}"


async def record_outbound(phone: str, payload: dict) -> None:
    """Guarda el payload que se habría enviado a Meta, sin tocar la red."""
    from app.services.conversation import ConversationService

    try:
        client = await ConversationService._get_client()
        if client is None:
            return
        key = outbox_key(phone)
        await client.rpush(key, json.dumps(payload, ensure_ascii=False))
        await client.expire(key, OUTBOX_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - la captura no debe romper el flujo
        logger.warning("staging_outbox_capture_failed error=%s", type(exc).__name__)


async def read_outbox(client, phone: str) -> list[dict]:
    raw = await client.lrange(outbox_key(phone), 0, -1)
    return [json.loads(item) for item in raw]


async def clear_outbox(client, phone: str) -> int:
    return await client.delete(outbox_key(phone))
