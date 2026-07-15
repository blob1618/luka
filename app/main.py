import os
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation

import redis.asyncio as redis
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.sql import func

# Cargar variables de entorno desde .env ANTES de importar submodulos
load_dotenv()

from app.api.whatsapp import send_whatsapp_message  # noqa: E402
from app.scheduler import start_scheduler  # noqa: E402
from app.services.finance import FinanceService, MovementRegistrationResult  # noqa: E402
from app.services.llm import LLMService  # noqa: E402
from app.services.onboarding import (  # noqa: E402
    OnboardingDecision,
    OnboardingService,
)
from app.services.reminder import ReminderService, ReminderResult  # noqa: E402
from app.services.conversation import (  # noqa: E402
    ConversationService,
    PendingMovement,
)

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


def _registered_reply_from_pending(pending: PendingMovement) -> str:
    movement_type = pending.movement_type or "movimiento"
    description = pending.description or "movimiento"
    amount = _format_amount(pending.amount)
    currency = pending.currency.upper()
    return f"✅ Registré tu {movement_type}: {description} por ${amount} {currency}."


def _category_confirmation_reply(category_name: str) -> str:
    return f"¿La categoría *{category_name}* es correcta? (respondé sí/no o decime otra)"


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

    # Estos intents se manejan aparte en el flujo STK-39
    if intent in {"confirm_category", "reject_category", "delete_category", "list_categories"}:
        return reply_text

    if intent in {"reminder", "budget_query", "expense_summary"}:
        return (
            "Esta función todavía no está disponible en esta versión. "
            "Por ahora puedo ayudarte a registrar ingresos y egresos por texto."
        )

    return reply_text or "No pude interpretar tu mensaje. ¿Podés reformularlo?"


def _update_ultimo_mensaje(sender_phone: str) -> None:
    """Update usuario.ultimo_mensaje_en for WhatsApp 24h window tracking."""
    from app.models.database import SessionLocal, Usuario
    session = SessionLocal()
    try:
        session.query(Usuario).filter(
            Usuario.whatsapp_id == sender_phone
        ).update({"ultimo_mensaje_en": func.now()})
        session.commit()
    except Exception as exc:
        session.rollback()
        print(f"[UPDATE_ULTIMO_MENSAJE] Error: {type(exc).__name__}: {exc}")
    finally:
        session.close()


def _is_create_reminder(extracted_data: dict) -> bool:
    return extracted_data.get("intent") == "create_reminder"


def _reminder_creation_reply(
    result: ReminderResult,
    extracted_data: dict,
) -> str:
    if result.status == "created":
        concept = extracted_data.get("reminder_concept") or "tu pago"
        day = extracted_data.get("reminder_day")
        return f"✅ Listo, te voy a recordar {concept} el día {day} de cada mes."

    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp."

    if result.status == "invalid_data":
        return result.message

    if result.status == "persistence_error":
        return "Hubo un problema. Intentá nuevamente en unos minutos."

    return "No pude procesar tu solicitud de recordatorio."


def _onboarding_invitation_reply(registration_url: str, ttl_minutes: int) -> str:
    return (
        "Para usar Luka, primero registrate y vinculá este WhatsApp:\n\n"
        f"{registration_url}\n\n"
        f"El enlace vence en {ttl_minutes} minutos."
    )


# ---------------------------------------------------------------------------
# STK-39: Handlers de gestión de categorías
# ---------------------------------------------------------------------------


def _category_deleted_reply(category_name: str) -> str:
    return f"✅ Categoría '{category_name}' eliminada. Los movimientos de esa categoría quedaron sin categoría."


def _category_not_found_reply(category_name: str) -> str:
    return f"No encontré una categoría '{category_name}'."


def _format_categories_list(categories_result) -> str:
    """Formatea la lista de categorías con totales para enviar por WhatsApp."""
    cats = categories_result.categories
    if not cats:
        return "No tenés categorías todavía. Cuando registres movimientos se irán creando."

    lines = ["📊 *Tus categorías:*"]
    for c in cats:
        ingreso = _format_amount(c.total_ingresos)
        egreso = _format_amount(c.total_egresos)
        default_tag = " (por defecto)" if c.es_default else ""
        lines.append(
            f"• {c.category_name}{default_tag}: "
            f"💰 ${ingreso} ingreso | 💸 ${egreso} egreso"
        )
    return "\n".join(lines)


async def _handle_category_confirmation(
    sender_phone: str,
    extracted_data: dict,
) -> str:
    """
    Maneja la confirmación o rechazo de categoría cuando hay un movimiento pendiente.
    """
    intent = extracted_data.get("intent")

    # Verificar si hay movimiento pendiente
    pending = await ConversationService.get_pending_movement(sender_phone)
    if pending is None:
        return "No encontré un movimiento pendiente para confirmar."

    if intent == "confirm_category":
        # El usuario confirmó la categoría inferida
        category_name = pending.inferred_category
        result = FinanceService.register_movement_with_category(
            sender_phone=sender_phone,
            whatsapp_message_id=pending.whatsapp_message_id,
            original_text=pending.original_text,
            movement_type=pending.movement_type,
            amount=pending.amount,
            currency=pending.currency,
            description=pending.description,
            category_name=category_name,
            create_category_if_missing=True,
        )
        await ConversationService.clear_state(sender_phone)

        if result.status == "registered":
            if category_name:
                return f"{_registered_reply_from_pending(pending)}\n📁 Categoría: {category_name}"
            return _registered_reply_from_pending(pending)
        else:
            return _registration_reply(result, pending.llm_result_extra)

    elif intent == "reject_category":
        # El usuario rechazó la categoría, ver si propuso una nueva
        user_category = extracted_data.get("category")
        if user_category:
            # Usuario propuso una categoría específica
            result = FinanceService.register_movement_with_category(
                sender_phone=sender_phone,
                whatsapp_message_id=pending.whatsapp_message_id,
                original_text=pending.original_text,
                movement_type=pending.movement_type,
                amount=pending.amount,
                currency=pending.currency,
                description=pending.description,
                category_name=user_category,
                create_category_if_missing=True,
            )
            await ConversationService.clear_state(sender_phone)

            if result.status == "registered":
                return f"{_registered_reply_from_pending(pending)}\n📁 Categoría: {user_category}"
            else:
                return _registration_reply(result, pending.llm_result_extra)
        else:
            # No propuso categoría, preguntar cuál quiere
            return "¿A qué categoría querés asignar este movimiento? Decime el nombre."

    return "No pude procesar tu respuesta. ¿La categoría es correcta o querés cambiarla?"


async def _handle_delete_category(sender_phone: str, extracted_data: dict) -> str:
    """Maneja la eliminación de una categoría."""
    from app.models.database import SessionLocal, Usuario

    category_name = extracted_data.get("category")
    if not category_name:
        return "¿Qué categoría querés eliminar? Decime el nombre."

    session = SessionLocal()
    try:
        user = session.query(Usuario).filter(Usuario.whatsapp_id == sender_phone).first()
        if user is None:
            return "No encontré tu cuenta."
        user_id = user.id
    finally:
        session.close()

    result = FinanceService.delete_category(user_id, category_name)
    if result.status == "deleted":
        return _category_deleted_reply(result.category_name or category_name)
    elif result.status == "not_found":
        return _category_not_found_reply(category_name)
    else:
        return "Hubo un problema eliminando la categoría. Intentá de nuevo."


async def _handle_list_categories(sender_phone: str) -> str:
    """Maneja la solicitud de listar categorías con totales."""
    from app.models.database import SessionLocal, Usuario

    session = SessionLocal()
    try:
        user = session.query(Usuario).filter(Usuario.whatsapp_id == sender_phone).first()
        if user is None:
            return "No encontré tu cuenta."
        user_id = user.id
    finally:
        session.close()

    result = FinanceService.get_categories_with_totals(user_id)
    if result.status == "ok":
        return _format_categories_list(result)
    else:
        return "Hubo un problema consultando las categorías."


# ---------------------------------------------------------------------------
# Webhook handlers
# ---------------------------------------------------------------------------


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

                    onboarding_result = OnboardingService.prepare_whatsapp_message(
                        sender_phone
                    )
                    if onboarding_result.decision == OnboardingDecision.SEND_INVITATION:
                        reply_text = _onboarding_invitation_reply(
                            onboarding_result.registration_url,
                            onboarding_result.invitation_ttl_minutes,
                        )
                        await send_whatsapp_message(sender_phone, reply_text)
                        continue
                    if onboarding_result.decision == OnboardingDecision.SUPPRESS_RESPONSE:
                        continue
                    if onboarding_result.decision == OnboardingDecision.ERROR:
                        await send_whatsapp_message(
                            sender_phone,
                            "No pude verificar tu cuenta. Intentá nuevamente en unos minutos.",
                        )
                        continue

                    # Track last message time for 24h window
                    _update_ultimo_mensaje(sender_phone)

                    # ------------------------------------------------------------------
                    # STK-39: Verificar si hay una conversación pendiente (categoría)
                    # ------------------------------------------------------------------
                    is_awaiting = await ConversationService.is_awaiting_category_confirmation(
                        sender_phone
                    )

                    if is_awaiting:
                        # El usuario está en medio de una confirmación de categoría
                        extracted_data = await LLMService.process_message(text_body)
                        intent = extracted_data.get("intent", "out_of_scope")

                        # Si el LLM no reconoce el intent como confirmación/rechazo,
                        # forzarlo según palabras clave simples como fallback
                        if intent not in ("confirm_category", "reject_category", "out_of_scope"):
                            lower_text = text_body.strip().lower()
                            if lower_text in ("si", "sí", "dale", "ok", "okey", "de una", "correcto", "bien", "de acuerdo", "confirmo"):
                                intent = "confirm_category"
                                extracted_data["intent"] = intent
                            elif lower_text in ("no", "nop", "nel", "otra", "cambiar", "cambiala", "no esa"):
                                intent = "reject_category"
                                extracted_data["intent"] = intent

                        if intent == "confirm_category":
                            reply_text = await _handle_category_confirmation(
                                sender_phone, extracted_data
                            )
                        elif intent == "reject_category":
                            reply_text = await _handle_category_confirmation(
                                sender_phone, extracted_data
                            )
                        else:
                            # Respuesta no reconocida en medio de confirmación
                            pending = await ConversationService.get_pending_movement(sender_phone)
                            if pending and pending.inferred_category:
                                reply_text = (
                                    f"No entendí tu respuesta. ¿La categoría "
                                    f"*{pending.inferred_category}* es correcta? "
                                    f"(respondé sí/no o decime otra)"
                                )
                            else:
                                await ConversationService.clear_state(sender_phone)
                                reply_text = "No entendí tu respuesta. Cancelé la operación."
                    else:
                        # No hay conversación pendiente, procesar normalmente
                        extracted_data = await LLMService.process_message(text_body)
                        intent = extracted_data.get("intent", "out_of_scope")

                        # ----------------------------------------------------------
                        # STK-39: Manejar intents de gestión de categorías
                        # ----------------------------------------------------------
                        if intent == "delete_category":
                            reply_text = await _handle_delete_category(sender_phone, extracted_data)
                        elif intent == "list_categories":
                            reply_text = await _handle_list_categories(sender_phone)
                        elif _is_financial_movement(extracted_data):
                            # Movimiento financiero: ver si tiene categoría inferida
                            category_name = extracted_data.get("category")

                            if category_name:
                                # Guardar movimiento como pendiente para confirmar categoría
                                pending = PendingMovement(
                                    sender_phone=sender_phone,
                                    whatsapp_message_id=whatsapp_message_id,
                                    original_text=text_body,
                                    movement_type=extracted_data.get("movement_type", "egreso"),
                                    amount=Decimal(str(extracted_data.get("amount", 0))),
                                    currency=extracted_data.get("currency", "ARS"),
                                    description=_movement_description(extracted_data),
                                    inferred_category=category_name,
                                    llm_result_extra=extracted_data,
                                )
                                await ConversationService.set_pending_movement(
                                    sender_phone, pending
                                )
                                reply_text = _category_confirmation_reply(category_name)
                            else:
                                # Sin categoría inferida, registrar directamente
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
                        elif intent in ("confirm_category", "reject_category"):
                            # Estos intents no deberían llegar acá sin pending, pero por si acaso
                            reply_text = "No encontré un movimiento pendiente para confirmar."
                        elif _is_create_reminder(extracted_data):
                            reminder_result = ReminderService.create_reminder(
                                sender_phone=sender_phone,
                                llm_result=extracted_data,
                            )
                            print(
                                "[REMINDER_CREATION]",
                                f"user={sender_phone}",
                                f"status={reminder_result.status}",
                            )
                            reply_text = _reminder_creation_reply(reminder_result, extracted_data)
                        else:
                            intent_str = extracted_data.get("intent", "out_of_scope")
                            print(f"[{str(intent_str).upper()}] User {sender_phone}: {text_body}")
                            reply_text = _safe_non_stk35_reply(extracted_data)

                    await send_whatsapp_message(sender_phone, reply_text)

    return {"status": "ok"}