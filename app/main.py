import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from uuid import UUID

import redis.asyncio as redis
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

# Cargar variables de entorno desde .env ANTES de importar submodulos
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from app.api.whatsapp import (  # noqa: E402
    close_whatsapp_client,
    parse_interactive_reply,
    send_whatsapp_message,
    send_whatsapp_typing_indicator,
)
from app.scheduler import start_scheduler  # noqa: E402
from app.services.telemetry import (  # noqa: E402
    finish_message_telemetry,
    start_message_telemetry,
    track_phase,
)
from app.services.conversation_flow import (  # noqa: E402
    ConversationFlowConflict,
    ConversationFlowNotFound,
    ConversationFlowService,
)
from app.services.conversation_flow_contract import (  # noqa: E402
    ConversationFlowDefinitionInvalid,
    CreateConversationFlowRequest,
    SaveConversationFlowDraftRequest,
    ValidateConversationFlowRequest,
    available_contract,
    validate_flow_definition,
)
from app.services.dispatcher import (  # noqa: E402
    process_incoming_interactive_reply,
    process_incoming_message,
)
from app.services.webhook_idempotency import (  # noqa: E402
    IdempotencyUnavailable,
    process_interactive_message_once,
    process_text_message_once,
)

# Cliente Redis global
redis_client = None
REDIS_CONNECT_TIMEOUT_SECONDS = 3
_pending_typing_tasks: set[asyncio.Task] = set()


def _dispatch_whatsapp_typing_indicator(
    message_id: str,
) -> asyncio.Task:
    """Despacha el typing indicator en background, reteniendo referencia y manejando excepciones."""
    async def _runner():
        try:
            sent = await send_whatsapp_typing_indicator(message_id)
            if not sent:
                logger.warning(
                    "[BACKGROUND_MESSAGE] message_id=%s typing_failed error=send_returned_false",
                    message_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[BACKGROUND_MESSAGE] message_id=%s typing_failed error=%s",
                message_id,
                type(exc).__name__,
            )
        finally:
            current = asyncio.current_task()
            if current is not None:
                _pending_typing_tasks.discard(current)

    task = asyncio.create_task(_runner())
    _pending_typing_tasks.add(task)
    task.add_done_callback(_pending_typing_tasks.discard)
    return task


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

    try:
        start_scheduler()
        yield
    finally:
        # Logica de apagado protegida
        try:
            if _pending_typing_tasks:
                tasks = list(_pending_typing_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                _pending_typing_tasks.clear()
        finally:
            try:
                await close_whatsapp_client()
            finally:
                if redis_client:
                    await redis_client.close()


app = FastAPI(title="Luka WhatsApp FinBot", lifespan=lifespan)

# En produccion, cargar esto de forma segura desde el entorno
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "fallback_token")


def _require_flow_admin(request: Request) -> None:
    expected_key = os.getenv("FLOW_ADMIN_API_KEY", "").strip()
    if not expected_key:
        raise HTTPException(
            status_code=503,
            detail="Conversation flow administration is not configured",
        )
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied_key = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not secrets.compare_digest(supplied_key, expected_key)
    ):
        raise HTTPException(status_code=401, detail="Invalid administrative credential")


def _raise_flow_http_error(exc: Exception):
    if isinstance(exc, ConversationFlowNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ConversationFlowConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ConversationFlowDefinitionInvalid):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_flow_definition",
                "errors": [
                    {"path": issue.path, "message": issue.message}
                    for issue in exc.issues
                ],
            },
        ) from exc
    raise exc


@app.get("/")
def read_root():
    return {"message": "Luka API is running"}


@app.get("/admin/conversation-flows/contracts")
def get_conversation_flow_contract(request: Request):
    _require_flow_admin(request)
    return available_contract()


@app.get("/admin/conversation-flows")
def list_conversation_flows(request: Request):
    _require_flow_admin(request)
    return ConversationFlowService.list()


@app.post("/admin/conversation-flows", status_code=201)
def create_conversation_flow(
    payload: CreateConversationFlowRequest,
    request: Request,
):
    _require_flow_admin(request)
    try:
        return ConversationFlowService.create(**payload.model_dump())
    except (
        ConversationFlowConflict,
        ConversationFlowDefinitionInvalid,
    ) as exc:
        _raise_flow_http_error(exc)


@app.post("/admin/conversation-flows/validate")
def validate_conversation_flow(
    payload: ValidateConversationFlowRequest,
    request: Request,
):
    _require_flow_admin(request)
    try:
        normalized = validate_flow_definition(payload.event_key, payload.definition)
        return {"valid": True, "definition": normalized}
    except ConversationFlowDefinitionInvalid as exc:
        _raise_flow_http_error(exc)


@app.get("/admin/conversation-flows/{flow_id}")
def get_conversation_flow(flow_id: UUID, request: Request):
    _require_flow_admin(request)
    try:
        return ConversationFlowService.get(flow_id)
    except ConversationFlowNotFound as exc:
        _raise_flow_http_error(exc)


@app.put("/admin/conversation-flows/{flow_id}/draft")
def save_conversation_flow_draft(
    flow_id: UUID,
    payload: SaveConversationFlowDraftRequest,
    request: Request,
):
    _require_flow_admin(request)
    try:
        return ConversationFlowService.save_draft(
            flow_id,
            **payload.model_dump(),
        )
    except (
        ConversationFlowConflict,
        ConversationFlowDefinitionInvalid,
        ConversationFlowNotFound,
    ) as exc:
        _raise_flow_http_error(exc)


@app.delete("/admin/conversation-flows/{flow_id}/draft")
def discard_conversation_flow_draft(flow_id: UUID, request: Request):
    _require_flow_admin(request)
    try:
        return ConversationFlowService.discard_draft(flow_id)
    except (ConversationFlowConflict, ConversationFlowNotFound) as exc:
        _raise_flow_http_error(exc)


@app.post("/admin/conversation-flows/{flow_id}/publish")
def publish_conversation_flow(flow_id: UUID, request: Request):
    _require_flow_admin(request)
    try:
        return ConversationFlowService.publish(flow_id)
    except (
        ConversationFlowConflict,
        ConversationFlowDefinitionInvalid,
        ConversationFlowNotFound,
    ) as exc:
        _raise_flow_http_error(exc)


@app.post("/admin/conversation-flows/{flow_id}/archive")
def archive_conversation_flow(flow_id: UUID, request: Request):
    _require_flow_admin(request)
    try:
        return ConversationFlowService.archive(flow_id)
    except ConversationFlowNotFound as exc:
        _raise_flow_http_error(exc)


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


async def _process_inbound_message_background(message: dict, redis_instance) -> None:
    """Procesa un mensaje entrante en segundo plano conservando la idempotencia.

    Nota de arquitectura:
    Este procesamiento corre in-process mediante BackgroundTasks y no es durable.
    Si el worker se reinicia durante la ejecución, el mensaje se perderá.
    """
    start_time = time.perf_counter()
    whatsapp_message_id = message.get("id")
    message_type = message.get("type")

    start_message_telemetry(whatsapp_message_id or "unknown")

    logger.info(
        "[BACKGROUND_MESSAGE] message_id=%s type=%s status=started",
        whatsapp_message_id,
        message_type,
    )

    status = "completed"
    try:
        if message_type == "text":
            sender_phone = message.get("from")
            text_body = message.get("text", {}).get("body", "")
            if sender_phone and whatsapp_message_id:
                with track_phase("typing"):
                    _dispatch_whatsapp_typing_indicator(whatsapp_message_id)
            status = await process_text_message_once(
                redis_client=redis_instance,
                sender_phone=sender_phone,
                text_body=text_body,
                whatsapp_message_id=whatsapp_message_id,
                process_message=process_incoming_message,
                send_message=send_whatsapp_message,
            )
        elif message_type == "interactive":
            interactive_reply = parse_interactive_reply(message)
            if interactive_reply is None:
                logger.warning(
                    "[BACKGROUND_MESSAGE] message_id=%s status=ignored_invalid_interactive",
                    whatsapp_message_id,
                )
                finish_message_telemetry(status="ignored_invalid_interactive")
                return
            if interactive_reply.sender_phone and interactive_reply.message_id:
                with track_phase("typing"):
                    _dispatch_whatsapp_typing_indicator(interactive_reply.message_id)
            status = await process_interactive_message_once(
                redis_client=redis_instance,
                interactive_reply=interactive_reply,
                process_reply=process_incoming_interactive_reply,
                send_message=send_whatsapp_message,
            )
        else:
            logger.warning(
                "[BACKGROUND_MESSAGE] message_id=%s type=%s status=unsupported_type",
                whatsapp_message_id,
                message_type,
            )
            finish_message_telemetry(status="unsupported_type")
            return

        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "[BACKGROUND_MESSAGE] message_id=%s status=completed duration_ms=%.2f",
            whatsapp_message_id,
            duration_ms,
        )
        finish_message_telemetry(status=status or "completed")
    except IdempotencyUnavailable as exc:
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.warning(
            "[BACKGROUND_MESSAGE] message_id=%s status=idempotency_unavailable error=%s duration_ms=%.2f",
            whatsapp_message_id,
            type(exc).__name__,
            duration_ms,
        )
        finish_message_telemetry(status="idempotency_unavailable")
    except Exception as exc:
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.exception(
            "[BACKGROUND_MESSAGE] message_id=%s status=error error=%s duration_ms=%.2f",
            whatsapp_message_id,
            type(exc).__name__,
            duration_ms,
        )
        finish_message_telemetry(status="error")


@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Maneja los mensajes entrantes de la API de Meta WhatsApp desacoplándolos
    en segundo plano con BackgroundTasks para responder de inmediato.

    Nota de arquitectura:
    BackgroundTasks es in-process y no durable. Si el proceso se reinicia
    o cae tras emitir HTTP 200, los mensajes encolados pendientes en memoria
    no se recuperarán (Meta no reintentará tras recibir 200). Para persistencia
    garantizada se requerirá una cola durable (ej. Celery/SQS/Outbox) en un
    ticket futuro.
    """
    data = await request.json()
    logger.info("Evento de webhook recibido")

    if data.get("object") == "whatsapp_business_account":
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                statuses = value.get("statuses", [])

                for status_event in statuses:
                    logger.info(
                        "WhatsApp status update message_id=%s status=%s",
                        status_event.get("id"),
                        status_event.get("status"),
                    )

                for message in messages:
                    message_type = message.get("type")
                    if message_type in ("text", "interactive"):
                        background_tasks.add_task(
                            _process_inbound_message_background,
                            message,
                            redis_client,
                        )

    return {"status": "ok"}
