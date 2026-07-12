import os
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation

import redis.asyncio as redis
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

# Cargar variables de entorno desde .env ANTES de importar submodulos
load_dotenv()

from app.api.whatsapp import send_whatsapp_message  # noqa: E402
from app.scheduler import start_scheduler  # noqa: E402
from app.services.finance import FinanceService, MovementRegistrationResult  # noqa: E402
from app.services.llm import LLMService  # noqa: E402

# Cliente Redis global
redis_client = None
REDIS_CONNECT_TIMEOUT_SECONDS = 3


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Inicializar el pool de conexiones de Redis
    redis_client = redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
    )

    # Prueba basica para verificar que la conexion funciona al arrancar
    try:
        await redis_client.ping()
        print("Conexion a Redis exitosa.")
    except Exception as e:
        # Redis no es necesario para servir el health check ni el webhook actual.
        # No bloquear el arranque si el servicio aun no esta disponible.
        print(f"Fallo al conectar con Redis tras {REDIS_CONNECT_TIMEOUT_SECONDS}s: {e}")

    start_scheduler()
    yield
    # Logica de apagado
    if redis_client:
        await redis_client.close()


app = FastAPI(title="Luka WhatsApp FinBot", lifespan=lifespan)

# En produccion, cargar esto de forma segura desde el entorno
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "fallback_token")


@app.get("/")
def read_root():
    return {"message": "Luka API is running"}


@app.get("/redis-test")
async def test_redis():
    """
    Endpoint de prueba basico para verificar la conectividad con Redis desde Render.
    """
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis client not initialized")
    try:
        await redis_client.set("test_key", "works", ex=60)
        value = await redis_client.get("test_key")
        return {"status": "ok", "redis_value": value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Redis connection error: {str(e)}")


def _is_financial_movement(extracted_data: dict) -> bool:
    return extracted_data.get("intent") == "expense"


def _movement_description(extracted_data: dict) -> str:
    return (
        extracted_data.get("description")
        or extracted_data.get("expense")
        or "movimiento"
    )


def _format_amount(amount) -> str:
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return str(amount)

    if decimal_amount == decimal_amount.to_integral_value():
        return str(decimal_amount.quantize(Decimal("1")))
    return str(decimal_amount.normalize())


def _registered_reply(extracted_data: dict) -> str:
    movement_type = extracted_data.get("movement_type") or "movimiento"
    description = _movement_description(extracted_data)
    amount = _format_amount(extracted_data.get("amount"))
    currency = str(extracted_data.get("currency") or "ARS").upper()
    return f"✅ Registré tu {movement_type}: {description} por ${amount} {currency}."


def _registration_reply(
    result: MovementRegistrationResult,
    extracted_data: dict,
) -> str:
    if result.status == "registered":
        return _registered_reply(extracted_data)

    if result.status == "duplicate":
        return "Este movimiento ya había sido registrado, no lo dupliqué."

    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp. No pude registrar el movimiento."

    if result.status == "invalid_data":
        return (
            "No pude registrar el movimiento porque me faltan datos claros. "
            "¿Podés reenviarlo con monto, descripción y si es ingreso o egreso?"
        )

    if result.status == "persistence_error":
        return "Hubo un problema registrando el movimiento. Por favor, intentá nuevamente en unos minutos."

    if result.status == "not_a_movement":
        return (
            "No identifiqué un movimiento financiero para registrar. "
            "Podés escribir algo como: 'Gasté 5000 en supermercado'."
        )

    return extracted_data.get("reply_text") or "No pude interpretar ese mensaje como un movimiento financiero."


def _safe_non_stk35_reply(extracted_data: dict) -> str:
    intent = extracted_data.get("intent")
    reply_text = extracted_data.get("reply_text") or ""

    if intent in {"reminder", "budget_query", "expense_summary"}:
        return (
            "Esta función todavía no está disponible en esta versión. "
            "Por ahora puedo ayudarte a registrar ingresos y egresos por texto."
        )

    return reply_text or "No pude interpretar tu mensaje. ¿Podés reformularlo?"


@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Requerido para la verificacion del webhook de Meta WhatsApp.
    """
    query_params = request.query_params
    hub_mode = query_params.get("hub.mode") or query_params.get("hub_mode")
    hub_challenge = query_params.get("hub.challenge") or query_params.get("hub_challenge")
    hub_verify_token = query_params.get("hub.verify_token") or query_params.get("hub_verify_token")

    print(
        "[WEBHOOK VERIFY] ",
        f"path={request.url.path}",
        f"mode={hub_mode}",
        f"challenge={hub_challenge}",
        f"token={hub_verify_token}",
        f"expected={VERIFY_TOKEN}",
    )

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN and hub_challenge is not None:
        return PlainTextResponse(content=str(hub_challenge), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Maneja los mensajes entrantes de la API de Meta WhatsApp.
    """
    data = await request.json()
    print("Evento de webhook recibido")

    if data.get("object") == "whatsapp_business_account":
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                statuses = value.get("statuses", [])

                for status_event in statuses:
                    print(
                        "WhatsApp status update",
                        f"message_id={status_event.get('id')}",
                        f"status={status_event.get('status')}",
                    )

                for message in messages:
                    sender_phone = message.get("from")
                    message_type = message.get("type")

                    if message_type != "text":
                        continue

                    whatsapp_message_id = message.get("id")
                    text_body = message.get("text", {}).get("body", "")
                    extracted_data = await LLMService.process_message(text_body)

                    if _is_financial_movement(extracted_data):
                        registration_result = FinanceService.register_movement_from_whatsapp_text(
                            sender_phone=sender_phone,
                            whatsapp_message_id=whatsapp_message_id,
                            original_text=text_body,
                            llm_result=extracted_data,
                        )
                        print(
                            "[MOVEMENT_REGISTRATION]",
                            f"user={sender_phone}",
                            f"message_id={whatsapp_message_id}",
                            f"status={registration_result.status}",
                        )
                        reply_text = _registration_reply(registration_result, extracted_data)
                    else:
                        intent = extracted_data.get("intent", "out_of_scope")
                        print(f"[{str(intent).upper()}] User {sender_phone}: {text_body}")
                        reply_text = _safe_non_stk35_reply(extracted_data)

                    await send_whatsapp_message(sender_phone, reply_text)

    return {"status": "ok"}
