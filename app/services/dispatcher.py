"""
Message dispatcher — routes incoming user messages to the appropriate service.

Extracted from app/main.py so both the WhatsApp webhook and the testing
environment can invoke the same logic.
"""

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.sql import func

from app.api.whatsapp import OutboundWhatsAppMessage
from app.models.database import Categoria, SessionLocal, Usuario
from app.services.conversation import (
    ConversationService,
    ConversationStateUnavailable,
    LastCreatedLimit,
    LastRegisteredMovement,
    PendingLimit,
    PendingLimitDelete,
    PendingMovement,
    PendingReminder,
    PendingSelection,
    RecentItems,
)
from app.services.conversation_flow_runtime import ConversationFlowRuntime
from app.services.budget import BudgetEvaluation, BudgetService, BudgetStatus
from app.services.compensation import (
    BudgetCompensationService,
    CompensationApplyResult,
    CompensationProposal,
)
from app.services.dashboard_link import DashboardLinkDecision, DashboardLinkService
from app.services.finance import (
    FinanceService,
    MovementQueryResult,
    MovementRegistrationResult,
)
from app.services.intent_routing import (
    normalize_limit_intent,
    normalize_movement_query_intent,
    normalize_movement_action,
    references_recent_limit,
)
from app.services.limit import LimitService
from app.services.llm import LLMService
from app.services.llm_contract import resolve_relative_date
from app.services.onboarding import OnboardingDecision, OnboardingService
from app.services.reminder import ReminderListResult, ReminderResult, ReminderService
from app.services.telemetry import track_phase
from app.services.reference_resolution import (
    named_movement_targets, normalize_text, recent_count, select_items,
    select_named_months, selects_all, selects_recent,
)


ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    """Result of processing an incoming message."""
    reply_text: str
    raw_llm_response: dict | None = None
    service_invoked: str | None = None
    intent: str | None = None
    debug_info: dict = field(default_factory=dict)
    event_key: str | None = None
    event_variables: dict[str, Any] = field(default_factory=dict)
    reply_message: OutboundWhatsAppMessage | None = None
    clear_memory: bool = False


# ---------------------------------------------------------------------------
# Reply helpers
# ---------------------------------------------------------------------------


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


def _category_hint_reply() -> str:
    return (
        "¿No estás de acuerdo con la categoría? Indicame y lo cambiamos."
    )


def _category_confirmation_reply(category_name: str) -> str:
    return (
        f"📁 Detecté la categoría *{category_name}*. "
        "¿Confirmás que es correcta? Respondé 'sí' para confirmar o decime la categoría correcta."
    )


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


def _registration_dispatch_reply(
    result: MovementRegistrationResult,
    extracted_data: dict,
) -> str:
    """
    Responde al registro. Un duplicado (mismo whatsapp_message_id ya persistido)
    es un reintento/reenvío de Meta de un mensaje ya procesado: la confirmación ya
    se envió en la primera entrega, así que se suprime la respuesta visible.
    """
    if result.status == "duplicate":
        return ""
    return _registration_reply(result, extracted_data)


def _multiop_registration_reply(
    results: list[MovementRegistrationResult],
    extracted_data: dict,
    movements: list | None = None,
) -> str:
    """Resume el registro de uno o varios movimientos por mensaje."""
    registered = sum(1 for r in results if r.status == "registered")
    total = len(results)

    if registered == total:
        if total == 1:
            single = (movements or [extracted_data])[0]
            return f"{_registered_reply(single)}\n{_category_hint_reply()}"
        return f"✅ Registré los {total} movimientos."

    if registered == 0:
        if total == 1:
            return _registration_dispatch_reply(results[0], extracted_data)
        return (
            "No pude registrar ningún movimiento porque me faltan datos. "
            "¿Podés reenviarlos con monto, descripción y si es ingreso o egreso?"
        )

    return (
        f"✅ Registré {registered} de {total} movimientos. "
        "Algunos faltan datos (monto, descripción o tipo). ¿Los reenvías?"
    )


def _safe_non_persisted_reply(extracted_data: dict) -> str:
    intent = extracted_data.get("intent")
    reply_text = extracted_data.get("reply_text") or ""

    # Estos intents se manejan aparte en el flujo de gestión de categorías
    if intent in {"confirm_category", "reject_category", "delete_category", "list_categories"}:
        return reply_text

    return reply_text or "No pude interpretar tu mensaje. ¿Podés reformularlo?"


def build_user_context(sender_phone: str) -> str:
    """Contexto para el LLM: fecha actual + categorías activas del usuario.

    Degrada silenciosamente (solo fecha) si la BD falla, nunca rompe el flujo.
    """
    date_line = f"FECHA ACTUAL: {date.today().isoformat()}."  # noqa: DTZ011
    session = None
    try:
        session = SessionLocal()
        user = session.query(Usuario).filter(Usuario.whatsapp_id == sender_phone).first()
        if user is None:
            return date_line
        categorias = (
            session.query(Categoria)
            .filter(Categoria.usuario_id == user.id)
            .filter(Categoria.esta_eliminado.is_(False))
            .order_by(Categoria.nombre)
            .all()
        )
    except Exception as exc:
        print(f"[BUILD_USER_CONTEXT] Error: {type(exc).__name__}: {exc}")
        return date_line
    finally:
        if session is not None:
            session.close()

    if not categorias:
        return date_line
    nombres = ", ".join(c.nombre for c in categorias)
    return f"{date_line}\nCATEGORÍAS DISPONIBLES DEL USUARIO: {nombres}"


def _movement_context_items(movements) -> list[dict]:
    return [
        {"id": movement.id, "label": movement.descripcion or "movimiento",
         "description": movement.descripcion,
         "amount": str(movement.cantidad), "currency": movement.moneda,
         "date": movement.fecha_movimiento.isoformat()}
        for movement in movements
    ]


async def _movement_budget_after_change(sender_phone: str, *movements) -> str:
    try:
        user_id = await asyncio.to_thread(_user_id_by_phone, sender_phone)
    except Exception as exc:
        logger.warning("movement_budget_lookup_failed error=%s", type(exc).__name__)
        return ""
    if user_id is None:
        return ""
    replies = []
    seen = set()
    evaluated: list[BudgetStatus] = []
    for movement in movements:
        if movement is None or movement.tipo != "egreso" or not movement.categoria_nombre:
            continue
        key = (movement.categoria_nombre, movement.fecha_movimiento.year,
               movement.fecha_movimiento.month, movement.moneda)
        if key in seen:
            continue
        seen.add(key)
        try:
            result = await asyncio.to_thread(
                BudgetService.get_status, user_id, movement.categoria_nombre,
                movement.fecha_movimiento, movement.moneda,
            )
        except Exception as exc:
            logger.warning("movement_budget_status_failed error=%s", type(exc).__name__)
            continue
        if result.status == "ok" and result.budget is not None:
            replies.append(_budget_status_reply(result.budget))
            evaluated.append(result.budget)

    exceeded = [budget for budget in evaluated if budget.state == "exceeded"]
    for budget in exceeded:
        auto_compensation = await _auto_compensation_reply(
            sender_phone, budget, user_id
        )
        if auto_compensation:
            replies.append(auto_compensation)
            break
    return "\n\n".join(replies)


async def _auto_compensation_reply(
    sender_phone: str, budget: BudgetStatus, user_id=None
) -> str:
    try:
        state = await ConversationService.get_state(sender_phone)
    except Exception as exc:
        logger.warning("movement_budget_state_failed error=%s", type(exc).__name__)
        return ""
    if state.step != "none":
        return ""
    if user_id is None:
        try:
            user_id = await asyncio.to_thread(_user_id_by_phone, sender_phone)
        except Exception as exc:
            logger.warning("movement_budget_lookup_failed error=%s", type(exc).__name__)
            return ""
    if user_id is None:
        return ""
    try:
        result = await asyncio.to_thread(
            BudgetCompensationService.build_proposal,
            user_id,
            target_category=budget.category_name,
            reference_date=budget.period_start,
            currency=budget.currency,
        )
    except Exception as exc:
        logger.warning("movement_budget_proposal_failed error=%s", type(exc).__name__)
        return ""
    if result.status != "ok" or result.proposal is None:
        return ""
    try:
        await ConversationService.set_pending_compensation(
            sender_phone, result.proposal.to_dict()
        )
        stored = await ConversationService.get_pending_compensation(sender_phone)
    except Exception as exc:
        logger.warning("movement_budget_pending_failed error=%s", type(exc).__name__)
        return ""
    if (
        stored is None
        or stored.proposal.get("proposal_id") != result.proposal.proposal_id
    ):
        return ""
    return _compensation_reply(result.proposal, auto=True)


async def _apply_movement_action(
    sender_phone: str, intent: str, target_id: str, changes: dict,
    event_data: dict | None = None, expected: dict | None = None,
) -> str:
    if intent == "update_movement":
        if not changes:
            if expected is not None:
                await ConversationService.set_recent_items(
                    sender_phone, RecentItems("movement", [expected])
                )
            return "¿Qué dato querés corregir del movimiento?"
        result = await asyncio.to_thread(
            FinanceService.update_movement, sender_phone, target_id, changes, expected
        )
    else:
        result = await asyncio.to_thread(
            FinanceService.annul_movement, sender_phone, target_id, expected
        )
    logger.info("movement_mutation intent=%s reference=shown_id candidates=1 status=%s",
                intent, result.status)
    if result.status == "not_found":
        return "Ese movimiento ya no está disponible."
    if result.status == "already_annulled":
        return "Ese movimiento ya estaba eliminado."
    if result.status == "stale_context":
        return "Ese movimiento cambió desde que lo mostramos. Consultá /movimientos otra vez."
    if result.status == "category_not_found":
        return "No encontré esa categoría activa. Indicame una de tus categorías."
    if result.status not in {"updated", "annulled"}:
        return "No pude modificar ese movimiento. Revisá los datos e intentá de nuevo."
    before = result.before
    if result.status == "updated":
        after = result.after
        reply = (
            f"✅ Corregí {before.descripcion}: ahora es ${_format_amount(after.cantidad)} "
            f"{after.moneda}. Categoría: {after.categoria_nombre or 'sin categoría'}."
        )
        await ConversationService.set_recent_items(
            sender_phone, RecentItems("movement", _movement_context_items([after]))
        )
        await ConversationService.set_last_movement(
            sender_phone, LastRegisteredMovement(
                movement_id=after.id, sender_phone=sender_phone,
                movement_type=after.tipo, amount=after.cantidad,
                currency=after.moneda, description=after.descripcion or "movimiento",
                category_name=after.categoria_nombre,
            ),
        )
        budget = await _movement_budget_after_change(sender_phone, before, after)
        if event_data is not None:
            event_data["_conversation_event_key"] = "movement.updated"
            event_data["_conversation_event_variables"] = {
                "description": after.descripcion or "movimiento",
                "amount": _format_amount(after.cantidad), "currency": after.moneda,
                "category": after.categoria_nombre or "sin categoría",
            }
    else:
        reply = f"✅ Eliminé {before.descripcion} por ${_format_amount(before.cantidad)} {before.moneda}."
        await ConversationService.set_recent_items(sender_phone, RecentItems("movement", []))
        last = await ConversationService.get_last_movement(sender_phone)
        if last is not None and last.movement_id == target_id:
            await ConversationService.clear_last_movement(sender_phone)
        budget = await _movement_budget_after_change(sender_phone, before)
        if event_data is not None:
            event_data["_conversation_event_key"] = "movement.annulled"
            event_data["_conversation_event_variables"] = {
                "description": before.descripcion or "movimiento",
                "amount": _format_amount(before.cantidad), "currency": before.moneda,
            }
    return f"{reply}\n\n{budget}" if budget else reply


async def _apply_movement_batch_action(sender_phone: str, items: list[dict]) -> str:
    result = await asyncio.to_thread(FinanceService.annul_movements, sender_phone, items)
    logger.info("movement_mutation intent=delete_movement candidates=%s status=%s",
                len(items), result.status)
    if result.status == "not_found":
        return "Alguno de esos movimientos ya no está disponible. Consultá /movimientos otra vez."
    if result.status == "already_annulled":
        return "Alguno de esos movimientos ya estaba eliminado. Consultá /movimientos otra vez."
    if result.status == "stale_context":
        return "Alguno de esos movimientos cambió desde que lo mostramos. Consultá /movimientos otra vez."
    if result.status != "annulled":
        return "No pude eliminar esos movimientos. No se modificó ninguno."
    await ConversationService.set_recent_items(sender_phone, RecentItems("movement", []))
    last = await ConversationService.get_last_movement(sender_phone)
    if last is not None and last.movement_id in {item.id for item in result.movements}:
        await ConversationService.clear_last_movement(sender_phone)
    details = ", ".join(
        f"{item.descripcion} (${_format_amount(item.cantidad)} {item.moneda})"
        for item in result.movements
    )
    reply = f"✅ Eliminé {len(result.movements)} movimientos: {details}."
    budget = await _movement_budget_after_change(sender_phone, *result.movements)
    return f"{reply}\n\n{budget}" if budget else reply


async def _handle_movement_action(
    sender_phone: str, text_body: str, extracted_data: dict
) -> str:
    intent = extracted_data["intent"]
    changes = extracted_data.get("changes") or {}
    reference = extracted_data.get("reference")
    recent = await ConversationService.get_recent_items(sender_phone)
    items = recent.items if recent is not None and recent.entity == "movement" else []
    if intent == "delete_movement":
        count = recent_count(text_body)
        if count is None and re.search(r"\b(?:ultimos|ultimas|recientes)\b", normalize_text(text_body)):
            selection = extracted_data.get("selection")
            raw_count = selection.get("recent_count") if isinstance(selection, dict) else None
            if isinstance(raw_count, int) and 2 <= raw_count <= 5:
                count = raw_count
            else:
                return "Indicame entre 2 y 5 movimientos recientes para eliminar."
        if count is not None:
            if len(items) < count:
                candidates = await asyncio.to_thread(
                    FinanceService.find_movement_candidates, sender_phone, limit=count
                )
                items = _movement_context_items(candidates)
            if len(items) < count:
                return f"Encontré menos de {count} movimientos para eliminar."
            return await _apply_movement_batch_action(sender_phone, items[:count])
        names = named_movement_targets(text_body)
        coordinated = " y " in normalize_text(text_body)
        if (not names and coordinated and isinstance(reference, dict)
                and isinstance(reference.get("descriptions"), list)):
            names = [str(name) for name in reference["descriptions"] if str(name).strip()]
        if len(names) >= 2:
            selected = []
            for name in names:
                matches = [item for item in items
                           if normalize_text(str(item.get("description") or item.get("label") or ""))
                           == normalize_text(name)]
                if not matches:
                    candidates = await asyncio.to_thread(
                        FinanceService.find_movement_candidates, sender_phone,
                        description=name, limit=6,
                    )
                    matches = _movement_context_items([
                        item for item in candidates
                        if normalize_text(item.descripcion or "") == normalize_text(name)
                    ])
                if len(matches) != 1:
                    return (f"No pude identificar un único movimiento de {name}. "
                            "Consultá /movimientos e indicame cuáles querés eliminar.")
                selected.append(matches[0])
            if len({item["id"] for item in selected}) != len(selected):
                return "No pude distinguir esos movimientos. Consultá /movimientos otra vez."
            return await _apply_movement_batch_action(sender_phone, selected)
        if coordinated:
            return "No pude distinguir todos los movimientos que querés eliminar. Indicame sus descripciones."
    description = reference.get("description") if isinstance(reference, dict) else None
    if not description:
        match = re.search(r"\b(?:movimiento|gasto|compra|el) de (.+)$", text_body, re.IGNORECASE)
        if match:
            description = match.group(1).strip(" .!?")
    if description:
        try:
            candidates = await asyncio.to_thread(
                FinanceService.find_movement_candidates, sender_phone, description=description
            )
        except Exception as exc:
            print(f"[MOVEMENT_SELECTION] {type(exc).__name__}")
            return "No pude consultar tus movimientos. Intentá nuevamente."
        items = _movement_context_items(candidates)
        reference_type = "description"
    elif reference == "last_registered" and not items:
        last = await ConversationService.get_last_movement(sender_phone)
        if last is not None:
            items = [{"id": last.movement_id, "label": last.description,
                      "description": last.description, "amount": str(last.amount),
                      "currency": last.currency}]
        reference_type = "last_registered"
    elif not selects_recent(text_body) and not items:
        return "¿Qué movimiento querés modificar? Indicame la descripción o consultá /movimientos."
    else:
        reference_type = "recent_list"
    logger.info("movement_resolution intent=%s reference=%s candidates=%s",
                intent, reference_type, len(items))
    if not items:
        return "No encontré ese movimiento. Podés consultar /movimientos para identificarlo."
    if len(items) > 1:
        selected = select_items(text_body, items)
        if len(selected) == 1:
            items = selected
        else:
            await ConversationService.set_pending_selection(
                sender_phone, PendingSelection(intent, "movement", items, changes)
            )
            options = "\n".join(
                f"{index}. {item['label']} — ${item['amount']} {item['currency']}"
                for index, item in enumerate(items, 1)
            )
            return f"Encontré varios movimientos. ¿Cuál querés elegir?\n{options}"
    return await _apply_movement_action(
        sender_phone, intent, items[0]["id"], changes, extracted_data, items[0]
    )


def _update_ultimo_mensaje(sender_phone: str) -> None:
    """Update usuario.ultimo_mensaje_en for WhatsApp 24h window tracking."""
    from app.models.database import Usuario
    with track_phase("db"):
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


_CONCEPT_EXTRACTOR = re.compile(
    r'(?:recordatorio|avis(?:ar|ame?)|record(?:ar|ame?)|crea(?:r|me)?|quiero)\s+'
    r'(?:(?:de|para|el|la|los|las|un|una|del|al|pagar|crear|hacer|recordatorio)\s+)*'
    r'(\w[\w\s]{0,30}?\w)'
    r'(?=\s+(?:el\s+)?(?:d[ií]a|\d)|$)',
    re.IGNORECASE,
)

_VERBOS_ACCION = re.compile(r'\b(pagar|crear|quiero|crees|hacer|avisar|recordar)\b', re.IGNORECASE)


def _extract_concept_from_text(text: str) -> str | None:
    m = _CONCEPT_EXTRACTOR.search(text)
    if m:
        candidate = m.group(1).strip()
        if 2 <= len(candidate) <= 32:
            return candidate
    return None


def _validate_reminder_concept(llm_concept: str | None, text_body: str) -> str | None:
    if not llm_concept:
        return _extract_concept_from_text(text_body)
    cleaned = llm_concept.strip()
    # force regex if concept smells like a sentence (contains verbs or > 3 words)
    if _VERBOS_ACCION.search(cleaned):
        return _extract_concept_from_text(text_body)
    if len(cleaned) > 32 or len(cleaned.split()) > 3:
        return _extract_concept_from_text(text_body)
    return cleaned


def _reminder_creation_reply(
    result: ReminderResult,
    extracted_data: dict,
) -> str:
    if result.status == "created":
        concept = extracted_data.get("reminder_concept") or "tu pago"
        day = extracted_data.get("reminder_day")
        amount = extracted_data.get("reminder_amount")
        currency = str(extracted_data.get("reminder_currency") or "ARS").upper()
        amount_text = ""
        if amount:
            amount_text = f" (${_format_amount(amount)} {currency})"
        return f"✅ Dale, te aviso que pagués {concept}{amount_text} el día {day} de cada mes."

    if result.status == "duplicate_title":
        return result.message

    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp."

    if result.status == "invalid_data":
        return result.message

    if result.status == "persistence_error":
        return "Hubo un problema. Intentá nuevamente en unos minutos."

    return "No pude procesar tu solicitud de recordatorio."


def _reminder_list_reply(result: ReminderListResult) -> str:
    reminders = result.reminders or []
    if not reminders:
        return "No tenés recordatorios activos por ahora."

    lines = ["📌 *Tus recordatorios:*"]
    for reminder in reminders:
        amount = reminder.get("monto")
        currency = str(reminder.get("moneda") or "ARS").upper()
        amount_text = ""
        if amount is not None:
            amount_text = f" — ${_format_amount(amount)} {currency}"
        estado = reminder.get("estado", "activo")
        estado_icon = "⏸️" if estado == "pausado" else ""
        lines.append(
            f"{estado_icon}• *{reminder.get('titulo')}* — día {reminder.get('dia_del_mes')}{amount_text}"
        )
    return "\n".join(lines)


def _reminder_update_reply(result: ReminderResult) -> str:
    if result.status == "updated":
        return "✅ Listo, actualicé el recordatorio."
    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp."
    if result.status in {"not_found", "not_owned"}:
        return "No encontré ese recordatorio. Chequeá el nombre con *mis recordatorios*."
    if result.status == "invalid_data":
        return result.message
    if result.status == "persistence_error":
        return "Hubo un problema. Intentá nuevamente en unos minutos."
    return "No pude procesar la edición del recordatorio."


def _reminder_state_reply(result: ReminderResult, action: str) -> str:
    if result.status == action:
        if action == "paused":
            return "✅ Dale, pausé ese recordatorio. Aviáme si querés reactivarlo."
        return "✅ Listo, reactivé el recordatorio."
    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp."
    if result.status in {"not_found", "not_owned"}:
        return "No encontré ese recordatorio. Chequeá el nombre con *mis recordatorios*."
    if result.status == "invalid_data":
        return result.message
    if result.status == "persistence_error":
        return "Hubo un problema. Intentá nuevamente en unos minutos."
    return "No pude procesar el cambio de estado del recordatorio."


def _proactive_prompts_reply(sender_phone: str, enabled: bool) -> str:
    result = ReminderService.set_proactive_prompts(sender_phone, enabled=enabled)
    if result.status == "updated":
        return result.message
    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp."
    if result.status == "persistence_error":
        return "Hubo un problema. Intentá nuevamente en unos minutos."
    return "No pude procesar tu solicitud."


def _reminder_delete_reply(result: ReminderResult) -> str:
    if result.status == "deleted":
        return "✅ Listo, eliminé el recordatorio."
    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp."
    if result.status in {"not_found", "not_owned"}:
        return "No encontré ese recordatorio. Chequeá el nombre con *mis recordatorios*."
    if result.status == "invalid_data":
        return result.message
    if result.status == "persistence_error":
        return "Hubo un problema. Intentá nuevamente en unos minutos."
    return "No pude procesar la eliminación del recordatorio."


async def _handle_list_reminders(sender_phone: str) -> str:
    from app.models.database import SessionLocal, Usuario

    session = SessionLocal()
    try:
        user = session.query(Usuario).filter(Usuario.whatsapp_id == sender_phone).first()
        if user is None:
            return "No encontré tu cuenta."

        result = ReminderService.list_reminders_all(user.id)
        return _reminder_list_reply(result)
    except Exception as exc:
        print(f"[REMINDER_LIST] Error: {type(exc).__name__}: {exc}")
        return "Hubo un problema consultando tus recordatorios."
    finally:
        session.close()


def _onboarding_invitation_reply(registration_url: str, ttl_minutes: int) -> str:
    return (
        "Para usar Luka, primero registrate y vinculá este WhatsApp:\n\n"
        f"{registration_url}\n\n"
        f"El enlace vence en {ttl_minutes} minutos."
    )


def _dashboard_link_reply(login_url: str, ttl_minutes: int) -> str:
    return (
        "Accedé a tu dashboard acá:\n\n"
        f"{login_url}\n\n"
        f"El enlace vence en {ttl_minutes} minutos y sólo se puede usar una vez."
    )


_DASHBOARD_LINK_NOT_ELIGIBLE_REPLY = (
    "Todavía no tenés una cuenta vinculada. Escribime cualquier mensaje para empezar."
)


# ---------------------------------------------------------------------------
# Handlers de gestión de categorías
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


async def _handle_reset_context(sender_phone: str) -> DispatchResult:
    """Borra el estado multi-turno y la memoria conversacional del usuario."""
    await ConversationService.clear_state(sender_phone)
    await ConversationService.clear_pending_selection(sender_phone)
    await ConversationService.clear_last_limit(sender_phone)
    await ConversationService.clear_last_movement(sender_phone)
    await ConversationService.clear_recent_items(sender_phone)
    with contextlib.suppress(ConversationStateUnavailable):
        await ConversationService.clear_pending_conversation_flow(sender_phone)
    return DispatchResult(
        reply_text="Listo, arrancamos de cero. Olvidé lo anterior.",
        service_invoked="conversation",
        intent="reset_context",
        clear_memory=True,
    )


async def _register_and_reply_with_hint(
    sender_phone: str,
    whatsapp_message_id: str | None,
    text_body: str,
    extracted_data: dict,
) -> str:
    """
    Registra el movimiento inmediatamente con la categoría inferida,
    guarda el último movimiento en Redis y devuelve el mensaje con hint.
    Soporta varios movimientos por mensaje (multiop).
    """
    movements = extracted_data.get("movements") or [extracted_data]

    # Confirmación de categoría SOLO con exactamente 1 movimiento con categoría inferida.
    if len(movements) == 1 and movements[0].get("category"):
        return await _register_single_with_hint(
            sender_phone, whatsapp_message_id, text_body, extracted_data, movements[0]
        )

    return await _register_multiop(
        sender_phone, whatsapp_message_id, text_body, extracted_data, movements
    )


async def _register_single_with_hint(
    sender_phone: str,
    whatsapp_message_id: str | None,
    text_body: str,
    extracted_data: dict,
    mov: dict,
) -> str:
    category_name = mov.get("category")
    llm_result = {**extracted_data, **mov}
    with track_phase("db"):
        result = await asyncio.to_thread(
            FinanceService.register_movement_with_category,
            sender_phone=sender_phone,
            whatsapp_message_id=whatsapp_message_id,
            original_text=text_body,
            movement_type=llm_result.get("movement_type", "egreso"),
            amount=Decimal(str(llm_result.get("amount") or 0)),
            currency=llm_result.get("currency", "ARS"),
            description=_movement_description(llm_result),
            category_name=category_name,
            create_category_if_missing=True,
            fecha_movimiento=resolve_relative_date(mov.get("fecha"), date.today()),  # noqa: DTZ011
        )

    print(
        "[MOVEMENT_REGISTRATION]",
        f"user={sender_phone}",
        f"message_id={whatsapp_message_id}",
        f"status={result.status}",
    )

    if result.status == "needs_category_confirmation":
        extracted_data["_conversation_event_key"] = (
            "category.confirmation_required"
        )
        extracted_data["_conversation_event_variables"] = {
            "category": category_name or "",
        }
        return await _route_needs_category_confirmation(
            sender_phone, whatsapp_message_id, text_body, llm_result, category_name
        )

    if result.status != "registered":
        if result.status == "invalid_data":
            extracted_data["_conversation_event_key"] = "movement.invalid_data"
            extracted_data["_conversation_event_variables"] = {
                "reason": result.message or "datos invalidos",
            }
        elif result.status == "persistence_error":
            extracted_data["_conversation_event_key"] = (
                "movement.persistence_error"
            )
        return _registration_dispatch_reply(result, llm_result)

    # Guardar el último movimiento en Redis para posible cambio de categoría
    last = LastRegisteredMovement(
        movement_id=result.movement_id,
        sender_phone=sender_phone,
        movement_type=llm_result.get("movement_type", "egreso"),
        amount=Decimal(str(llm_result.get("amount") or 0)),
        currency=llm_result.get("currency", "ARS"),
        description=_movement_description(llm_result),
        category_name=category_name,
    )
    await ConversationService.set_last_movement(sender_phone, last)
    await ConversationService.set_recent_items(
        sender_phone,
        RecentItems("movement", [{
            "id": result.movement_id, "label": last.description,
            "description": last.description, "amount": str(last.amount),
            "currency": last.currency,
        }]),
    )

    movement_type = llm_result.get("movement_type") or "movimiento"
    description = _movement_description(llm_result)
    amount = _format_amount(llm_result.get("amount"))
    currency = str(llm_result.get("currency") or "ARS").upper()
    extracted_data["_conversation_event_key"] = "movement.registered"
    extracted_data["_conversation_event_variables"] = {
        "movement_type": movement_type,
        "description": description,
        "amount": amount,
        "currency": currency,
    }

    reply = f"✅ Registré tu {movement_type}: {description} por ${amount} {currency}."
    reply += f"\n📁 Categoría: {category_name}."
    reply += f"\n{_category_hint_reply()}"
    evaluation = await asyncio.to_thread(
        BudgetService.evaluate_movement,
        result.movement_id,
    )
    budget_feedback = _budget_feedback_reply(evaluation)
    parts = [reply]
    if budget_feedback:
        parts.append(budget_feedback)
    if evaluation.budget is not None and evaluation.budget.state == "exceeded":
        auto_compensation = await _auto_compensation_reply(
            sender_phone, evaluation.budget, result.user_id
        )
        if auto_compensation:
            parts.append(auto_compensation)
    return "\n\n".join(parts)


async def _route_needs_category_confirmation(
    sender_phone: str,
    whatsapp_message_id: str | None,
    text_body: str,
    extracted_data: dict,
    category_name: str | None,
) -> str:
    """Deriva una categoría no resuelta al flujo existente de confirmación."""
    pending = PendingMovement(
        sender_phone=sender_phone,
        whatsapp_message_id=whatsapp_message_id,
        original_text=text_body,
        movement_type=extracted_data.get("movement_type", "egreso"),
        amount=Decimal(str(extracted_data.get("amount") or 0)),
        currency=extracted_data.get("currency", "ARS"),
        description=_movement_description(extracted_data),
        inferred_category=category_name,
        llm_result_extra=extracted_data,
    )
    await ConversationService.set_pending_movement(sender_phone, pending)
    return _category_confirmation_reply(category_name or "")


async def _register_multiop(
    sender_phone: str,
    whatsapp_message_id: str | None,
    text_body: str,
    extracted_data: dict,
    movements: list,
) -> str:
    results = []
    for index, mov in enumerate(movements):
        llm_result = {**extracted_data, **mov}
        item_message_id = (
            f"{whatsapp_message_id}:movement:{index}"
            if whatsapp_message_id and len(movements) > 1 else whatsapp_message_id
        )
        with track_phase("db"):
            result = await asyncio.to_thread(
                FinanceService.register_movement_from_whatsapp_text,
                sender_phone=sender_phone,
                whatsapp_message_id=item_message_id,
                original_text=text_body,
                llm_result=llm_result,
                fecha_movimiento=resolve_relative_date(mov.get("fecha"), date.today()),  # noqa: DTZ011
            )
        print(
            "[MOVEMENT_REGISTRATION]",
            f"user={sender_phone}",
            f"message_id={whatsapp_message_id}",
            f"status={result.status}",
        )
        results.append(result)
    reply = _multiop_registration_reply(results, extracted_data, movements)
    registered_ids = [
        result.movement_id
        for result in results
        if result.status == "registered" and result.movement_id
    ]
    if registered_ids:
        if len(movements) > 1:
            await ConversationService.clear_last_movement(sender_phone)
        elif len(movements) == 1:
            mov = movements[0]
            await ConversationService.set_last_movement(
                sender_phone, LastRegisteredMovement(
                    movement_id=registered_ids[0], sender_phone=sender_phone,
                    movement_type=mov.get("movement_type") or extracted_data.get("movement_type") or "egreso",
                    amount=Decimal(str(mov.get("amount"))),
                    currency=mov.get("currency") or "ARS",
                    description=_movement_description(mov),
                    category_name=mov.get("category"),
                ),
            )
        await ConversationService.set_recent_items(
            sender_phone,
            RecentItems("movement", [
                {"id": result.movement_id, "label": _movement_description(mov),
                 "description": _movement_description(mov),
                 "amount": str(mov.get("amount")), "currency": mov.get("currency") or "ARS"}
                for result, mov in zip(results, movements)
                if result.status == "registered" and result.movement_id
            ]),
        )
        evaluations = await asyncio.to_thread(
            BudgetService.evaluate_movements,
            registered_ids,
        )
        budget_feedback = _unique_budget_feedback(evaluations)
        if budget_feedback:
            reply = f"{reply}\n\n" + "\n\n".join(budget_feedback)
        user_id = next(
            (result.user_id for result in results if result.user_id), None
        )
        seen_limits = set()
        for evaluation in evaluations:
            budget = evaluation.budget
            if budget is None or budget.limit_id in seen_limits:
                continue
            seen_limits.add(budget.limit_id)
            if budget.state != "exceeded":
                continue
            auto_compensation = await _auto_compensation_reply(
                sender_phone, budget, user_id
            )
            if auto_compensation:
                reply = f"{reply}\n\n{auto_compensation}"
                break
    return reply


# ---------------------------------------------------------------------------
# Helpers de límites de gasto por categoría
# ---------------------------------------------------------------------------


def _format_limit_amount(amount) -> str:
    """Formatea un monto con separador de miles '.' y decimales ',' (es-AR)."""
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return str(amount)

    if decimal_amount == decimal_amount.to_integral_value():
        decimal_amount = decimal_amount.quantize(Decimal("1"))
    else:
        decimal_amount = decimal_amount.quantize(Decimal("0.01"))

    formatted = f"{decimal_amount:,.2f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def _current_year() -> int:
    from datetime import date

    return date.today().year


def _limit_month_label(month: int, year: int) -> str:
    return LimitService.month_label(month, year, _current_year())


def _budget_status_reply(status: BudgetStatus) -> str:
    label = _limit_month_label(status.period_start.month, status.period_start.year)
    spent = _format_limit_amount(status.spent_amount)
    limit = _format_limit_amount(status.limit_amount)
    if status.state == "exceeded":
        exceeded = _format_limit_amount(status.exceeded_amount)
        remaining = _format_limit_amount(status.remaining_amount)
        return (
            f"⚠️ *{status.category_name} — {label}*\n"
            f"Gastaste ${spent} {status.currency} de ${limit} {status.currency}.\n"
            f"Te quedan ${remaining} {status.currency} "
            f"({status.percentage}% usado).\n"
            f"Superaste el límite en ${exceeded} {status.currency}."
        )
    if status.state == "reached":
        remaining = _format_limit_amount(status.remaining_amount)
        return (
            f"🎯 *{status.category_name} — {label}*\n"
            f"Gastaste ${spent} {status.currency} de ${limit} {status.currency}.\n"
            f"Te quedan ${remaining} {status.currency} "
            f"({status.percentage}% usado).\n"
            "Alcanzaste tu límite."
        )
    remaining = _format_limit_amount(status.remaining_amount)
    return (
        f"📊 *{status.category_name} — {label}*\n"
        f"Gastaste ${spent} {status.currency} de ${limit} {status.currency}.\n"
        f"Te quedan ${remaining} {status.currency} ({status.percentage}% usado)."
    )


def _compensation_allocation_line(allocation, currency: str) -> str:
    return (
        f"{allocation.category_name}: "
        f"${_format_amount(allocation.before_limit)} → "
        f"${_format_amount(allocation.after_limit)} {currency}"
    )


def _compensation_summary(proposal: CompensationProposal) -> str:
    return (
        f"Compensación de ${_format_amount(proposal.amount)} {proposal.currency} "
        f"para {proposal.target.category_name}"
    )


def _compensation_reply(proposal: CompensationProposal, *, auto: bool = False) -> str:
    currency = proposal.currency
    if auto:
        allocations = "".join(
            f"• {_compensation_allocation_line(allocation, currency)}\n"
            for allocation in (*proposal.donors, proposal.target)
        )
        return (
            f"Detecté que *{proposal.target.category_name}* superó su límite.\n\n"
            f"💡 Podés compensarlo moviendo ${_format_amount(proposal.amount)} "
            f"{currency}:\n"
            f"{allocations}"
            "El total se mantiene. Vence en 30 minutos.\n"
            "Respondé *confirmar compensación* o *no por ahora*."
        )
    donors = "\n".join(
        f"• {_compensation_allocation_line(donor, currency)}"
        for donor in proposal.donors
    )
    return (
        "💡 *Compensación de presupuesto*\n\n"
        f"Muevo ${_format_amount(proposal.amount)} {currency} de otras categorías "
        f"a *{proposal.target.category_name}*.\n\n"
        f"Donantes:\n{donors}\n\n"
        f"*{_compensation_allocation_line(proposal.target, currency)}*\n\n"
        "El total de tus límites se mantiene: no se modifica ningún movimiento.\n"
        "La propuesta vence en 30 minutos.\n"
        "Respondé *confirmar compensación* o *no por ahora*."
    )


def _compensation_applied_reply(proposal: CompensationProposal) -> str:
    currency = proposal.currency
    allocations = "\n".join(
        f"• {_compensation_allocation_line(allocation, currency)}"
        for allocation in (proposal.target, *proposal.donors)
    )
    return (
        f"✅ Compensé *{proposal.target.category_name}* con "
        f"${_format_amount(proposal.amount)} {currency}.\n\n"
        f"{allocations}\n\n"
        "No se modificó ningún movimiento."
    )


def _compensation_apply_reply(apply_result: CompensationApplyResult) -> str:
    if apply_result.status == "applied" and apply_result.proposal is not None:
        return _compensation_applied_reply(apply_result.proposal)
    if apply_result.status == "stale":
        return (
            "Los saldos cambiaron desde que armé la propuesta. "
            "Pedime un nuevo cálculo."
        )
    if apply_result.status == "expired":
        return "La propuesta venció. Pedime un nuevo cálculo."
    return "No pude aplicar la compensación. No se modificó ningún límite."


def _budget_feedback_reply(evaluation: BudgetEvaluation) -> str:
    """Devuelve el estado de un límite aplicable, haya exceso o no."""
    budget = evaluation.budget
    print(
        "[BUDGET_EVALUATION]",
        f"movement_id={evaluation.movement_id}",
        f"status={evaluation.status}",
        f"has_limit={evaluation.has_limit}",
        f"limit_id={budget.limit_id if budget is not None else None}",
        f"state={budget.state if budget is not None else None}",
        f"percentage={budget.percentage if budget is not None else None}",
        f"should_alert={evaluation.should_alert}",
    )
    if (
        evaluation.status != "ok"
        or not evaluation.has_limit
        or budget is None
    ):
        return ""
    return _budget_status_reply(budget)


def _unique_budget_feedback(evaluations: list[BudgetEvaluation]) -> list[str]:
    feedback: list[str] = []
    seen: set[str] = set()
    for evaluation in evaluations:
        rendered = _budget_feedback_reply(evaluation)
        if not rendered or evaluation.budget is None:
            continue
        if evaluation.budget.limit_id in seen:
            continue
        seen.add(evaluation.budget.limit_id)
        feedback.append(rendered)
    return feedback


def _limit_registered_reply(
    category_name: str,
    amount,
    month: int,
    year: int,
    currency: str = "ARS",
    *,
    edit: bool = False,
) -> str:
    label = _limit_month_label(month, year)
    amount_text = _format_limit_amount(amount)
    if edit:
        reply = (
            f"✅ Listo, se Registró tu límite para {label}. "
            f"📁 Categoría: {category_name}. 🎯 Límite a gastar: ${amount_text} {currency}."
        )
    else:
        reply = (
            f"✅ Registré tu límite para {label}. "
            f"📁 Categoría: {category_name}. 🎯 Límite a gastar: ${amount_text} {currency}."
        )
        reply += "\n¿No te convence algo? Indícame y lo cambiamos."
    return reply


def _year_confirmation_reply(month: int, year: int) -> str:
    name = LimitService.month_label(month, year, _current_year()).split()[0]
    return f"⏩ ¿Quieres crear un límite de gastos para {name} de {year}?"


def _limit_missing_reply(result) -> str:
    if result.status == "needs_category":
        return "¿A qué categoría querés aplicar el límite?"
    if result.status == "needs_amount":
        if result.category_name:
            return f"¿Cuál es el monto máximo del límite para {result.category_name}?"
        return "¿Cuál es el monto máximo del límite?"
    if result.status == "needs_month":
        return "¿Para qué mes querés definir el límite?"
    return "Necesito que me completes la categoría o el monto del límite."


def _limit_list_reply(result) -> str:
    limits = result.limits or []
    if not limits:
        return "No tenés límites de gasto definidos por ahora."
    lines = ["🎯 *Tus límites de gasto:*"]
    for entry in limits:
        amount = _format_limit_amount(entry.amount)
        label = _limit_month_label(entry.month, entry.year)
        lines.append(f"• {entry.category_name} — ${amount} {entry.currency} — {label}")
    return "\n".join(lines)


def _limit_selection_reply(category_name: str, candidates: list[dict]) -> str:
    lines = [f"Tengo varios límites de {category_name}. ¿A cuál te referís?"]
    for candidate in candidates:
        label = _limit_month_label(candidate["month"], candidate["year"])
        amount = _format_limit_amount(candidate["amount"])
        currency = candidate.get("currency", "ARS")
        lines.append(f"• {label} — ${amount} {currency}")
    return "\n".join(lines)


def _limit_delete_reply(result, category_name: str) -> str:
    if result.status == "deleted":
        label = _limit_month_label(result.month, result.year)
        return f"✅ Listo, eliminé el límite de {result.category_name or category_name} de {label}."
    if result.status == "not_found":
        return f"No encontré un límite de {category_name} para eliminar."
    if result.status == "needs_month_selection":
        return _limit_selection_reply(category_name, result.candidates)
    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp."
    if result.status == "persistence_error":
        return "Hubo un problema eliminando el límite. Intentá nuevamente en unos minutos."
    return "No pude procesar la eliminación del límite."


_CANCEL_PATTERNS = re.compile(
    r'\b(?:cancel(?:ar|á|alo|ela)?|dej(?:a|á|alo|elo)?|olvid(?:a|á|alo|elo)?'
    r'|anul(?:a|á|alo|ela)?|ningun[oa]|no quiero|no me interesa|para nada)\b',
    re.IGNORECASE,
)

_MONTH_NAMES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def _is_cancel_request(text: str) -> bool:
    if not text:
        return False
    return _CANCEL_PATTERNS.search(text) is not None


_CONFIRM_PATTERNS = re.compile(
    r"\b(?:sí|si|dale|ok|okey|confirmo|confirmame|afirmativo|de acuerdo|claro|"
    r"genial|perfecto|listo|bárbaro|barbaro|bueno|yes)\b",
    re.IGNORECASE,
)

_CATEGORY_CREATION_CONFIRM_PATTERNS = re.compile(
    r"\b(?:cre(?:a|á)la|cre(?:a|á)\s+(?:esa|la)\s+categor[ií]a|"
    r"us(?:a|á)la|agreg(?:a|á)la)\b",
    re.IGNORECASE,
)

_CATEGORY_ALTERNATIVE_PATTERN = re.compile(
    r"^(?:mejor\s+)?(?:us(?:a|á)|utiliz(?:a|á)|pon(?:e|é))\s+"
    r"(?:(?:la|otra)\s+categor[ií]a\s+)?(?P<category>[\wáéíóúüñ\s-]{1,100})$",
    re.IGNORECASE,
)


def _is_confirm_request(text: str) -> bool:
    """Detecta una respuesta afirmativa clara (sí/dale/ok) al confirmar un límite.

    Se usa como respaldo cundo el LLM clasifica el 'sí' como out_of_scope/greeting
    porque el mensaje suelto no trae contexto de la pregunta previa.
    """
    if not text:
        return False
    return _CONFIRM_PATTERNS.search(text) is not None


def _is_category_creation_confirmation(text: str) -> bool:
    if not text:
        return False
    return (
        _is_confirm_request(text)
        or _CATEGORY_CREATION_CONFIRM_PATTERNS.search(text) is not None
    )


def _extract_category_alternative(text: str) -> str | None:
    match = _CATEGORY_ALTERNATIVE_PATTERN.match(text.strip()) if text else None
    if match is None:
        return None
    category = match.group("category").strip(" .,!?:;¡¿")
    return category or None


def _extract_amount_from_text(text: str) -> float | None:
    """Extrae un monto numérico del texto (con o sin separadores de miles)."""
    if not text:
        return None
    match = re.search(r'\d[\d.,]*', text)
    if not match:
        return None
    raw = match.group(0)
    # "1.234.567,89" / "1234,56" / "100000" -> número plano con decimal '.'
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    elif "." in raw:
        parts = raw.split(".")
        if len(parts) == 2 and len(parts[1]) == 2:
            raw = raw.replace(".", ".")
        else:
            raw = raw.replace(".", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _extract_category_from_text(text: str) -> str | None:
    """Usa el texto plano como categoría cuando el LLM no la detecta."""
    candidate = text.strip().strip("!?.")
    if not candidate or len(candidate) > 100:
        return None
    if _extract_amount_from_text(candidate) is not None:
        return None
    return candidate.lower()


def _extract_month_from_text(text: str) -> int | None:
    """Detecta el número de mes por su nombre dentro del texto."""
    if not text:
        return None
    lowered = text.lower()
    for name, month in _MONTH_NAMES.items():
        if name in lowered:
            return month
    return None


def _limit_base_data(pending: PendingLimit) -> dict:
    """Convierte un límite pendiente a dict de datos del LLM."""
    return {
        "limit_category": pending.category,
        "limit_amount": float(pending.amount) if pending.amount is not None else None,
        "limit_month": pending.month,
        "limit_year": pending.year,
        "limit_currency": pending.currency,
    }


def _last_limit_from_pending(pending: PendingLimit) -> LastCreatedLimit | None:
    """Reconstruye el último límite creado desde un pending de edición."""
    if pending.limit_id is None:
        return None
    return LastCreatedLimit(
        limit_id=pending.limit_id,
        sender_phone=pending.sender_phone,
        category_name=pending.category,
        amount=pending.amount,
        month=pending.month,
        year=pending.year,
        currency=pending.currency,
    )


async def _handle_create_limit(
    sender_phone: str,
    extracted_data: dict,
    last_limit: LastCreatedLimit | None = None,
    edit: bool | None = None,
    allow_category_creation: bool = False,
) -> str:
    """Crea o edita un límite de gasto, orquestando los pasos multi-turno."""
    if edit is None:
        edit = last_limit is not None

    with track_phase("db"):
        result = await asyncio.to_thread(
            LimitService.create_limit,
            sender_phone,
            extracted_data,
            last_limit=last_limit,
            allow_category_creation=allow_category_creation,
        )

    if result.status in ("created", "updated"):
        await ConversationService.set_last_limit(
            sender_phone,
            LastCreatedLimit(
                limit_id=result.limit_id,
                sender_phone=sender_phone,
                category_name=result.category_name,
                amount=result.amount,
                month=result.month,
                year=result.year,
                currency=result.currency or "ARS",
            ),
        )
        await ConversationService.clear_state(sender_phone)
        reply = _limit_registered_reply(
            result.category_name,
            result.amount,
            result.month,
            result.year,
            result.currency or "ARS",
            edit=edit,
        )
        budget_result = await asyncio.to_thread(
            BudgetService.get_status_for_limit,
            result.limit_id,
        )
        if budget_result.status == "ok" and budget_result.budget is not None:
            if budget_result.budget.state in {"reached", "exceeded"}:
                reply += f"\n\n{_budget_status_reply(budget_result.budget)}"
        return reply

    if result.status == "needs_year_confirmation":
        pending = PendingLimit(
            sender_phone=sender_phone,
            category=result.category_name,
            amount=result.amount,
            month=result.proposed_month,
            year=result.proposed_year,
            currency=result.currency or "ARS",
            is_edit=edit,
            limit_id=last_limit.limit_id if last_limit is not None else None,
        )
        await ConversationService.set_pending_limit(
            sender_phone,
            pending,
            step="awaiting_limit_year_confirmation",
        )
        return _year_confirmation_reply(result.proposed_month, result.proposed_year)

    if result.status == "needs_category_confirmation":
        pending = PendingLimit(
            sender_phone=sender_phone,
            category=result.category_name,
            amount=result.amount,
            month=result.month,
            year=result.year,
            currency=result.currency or "ARS",
            is_edit=edit,
            limit_id=last_limit.limit_id if last_limit is not None else None,
        )
        await ConversationService.set_pending_limit(
            sender_phone,
            pending,
            step="awaiting_limit_category_confirmation",
        )
        return (
            f"No tenés la categoría {result.category_name}. "
            "¿Querés crearla y aplicar el límite?"
        )

    if result.status in ("needs_category", "needs_amount", "needs_month"):
        month = extracted_data.get("limit_month")
        year = extracted_data.get("limit_year")
        if month is None and last_limit is not None:
            month = last_limit.month
        if year is None and last_limit is not None:
            year = last_limit.year
        pending = PendingLimit(
            sender_phone=sender_phone,
            category=result.category_name,
            amount=result.amount,
            month=month,
            year=year,
            currency=extracted_data.get("limit_currency")
            or getattr(last_limit, "currency", "ARS"),
            is_edit=edit,
            limit_id=last_limit.limit_id if last_limit is not None else None,
        )
        await ConversationService.set_pending_limit(
            sender_phone,
            pending,
            step="awaiting_limit_data",
        )
        return _limit_missing_reply(result)

    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp."
    if result.status == "persistence_error":
        return "Hubo un problema guardando tu límite. Intentá nuevamente en unos minutos."
    if result.status == "invalid_category":
        return "Esa categoría no pertenece a tu lista ni a las categorías disponibles."
    if result.status in {"invalid_month", "invalid_year", "invalid_currency"}:
        return "El mes, año o moneda del límite no es válido. ¿Podés revisarlo?"
    if result.status == "expired_period":
        return "No puedo crear un límite para un período que ya terminó."
    if result.status == "stale_context":
        await ConversationService.clear_last_limit(sender_phone)
        return "Ese límite ya no existe. Podés crear uno nuevo indicando categoría y monto."
    if result.status == "conflict":
        return "Ya existe un límite para esa categoría, mes y moneda."
    return "No pude procesar tu solicitud de límite."


async def _change_limit_candidate(sender_phone: str, candidate: dict, data: dict) -> str:
    last_limit = LastCreatedLimit(
        limit_id=candidate["limit_id"], sender_phone=sender_phone,
        category_name=candidate["category"], amount=Decimal(candidate["amount"]),
        month=candidate["month"], year=candidate["year"],
        currency=candidate["currency"],
    )
    changes = data.get("changes") or {}
    patch = {
        "limit_category": changes.get("category"),
        "limit_amount": changes.get("amount"),
        "limit_month": changes.get("target_month", data.get("limit_month")),
        "limit_year": changes.get("target_year", data.get("limit_year")),
        "limit_currency": changes.get("currency"),
    }
    if all(value is None for value in patch.values()):
        await ConversationService.set_recent_items(
            sender_phone, RecentItems("limit", [candidate])
        )
        return "¿Qué querés modificar de ese límite?"
    return await _handle_create_limit(sender_phone, patch, last_limit=last_limit)


async def _handle_change_limit(
    sender_phone: str, extracted_data: dict, text_body: str = ""
) -> str:
    """Edit an explicitly selected existing limit or a recent unambiguous one."""
    reference = extracted_data.get("reference")
    reference = reference if isinstance(reference, dict) else {}
    category = reference.get("category") or extracted_data.get("limit_category")
    source_month = reference.get("source_month")
    source_year = reference.get("source_year")
    source_currency = reference.get("source_currency")
    last_limit = await ConversationService.get_last_limit(sender_phone)
    refers_to_last = bool(re.search(r"\b(?:el mes|que sea|en vez de)\b", text_body.lower()))
    if (
        last_limit is not None
        and (not category or refers_to_last)
        and source_month is None and source_year is None
    ):
        if extracted_data.get("changes"):
            return await _change_limit_candidate(sender_phone, {
                "limit_id": last_limit.limit_id, "category": last_limit.category_name,
                "amount": str(last_limit.amount), "month": last_limit.month,
                "year": last_limit.year, "currency": last_limit.currency,
            }, extracted_data)
        patch = dict(extracted_data)
        if refers_to_last:
            patch["limit_category"] = None
        return await _handle_create_limit(sender_phone, patch, last_limit=last_limit)
    recent = await ConversationService.get_recent_items(sender_phone)
    if (
        recent is not None and recent.entity == "limit" and len(recent.items) == 1
        and source_month is None and source_year is None
        and (not category or category.casefold() == str(recent.items[0].get("category", "")).casefold())
    ):
        category = recent.items[0].get("category")
        source_month = recent.items[0].get("month")
        source_year = recent.items[0].get("year")
    if not category:
        return "¿Qué límite querés modificar? Indicame la categoría."
    try:
        candidates = await asyncio.to_thread(
            LimitService.find_limit_candidates, sender_phone, category=category,
            month=source_month, year=source_year, currency=source_currency,
        )
    except Exception as exc:
        print(f"[LIMIT_SELECTION] {type(exc).__name__}")
        return "No pude consultar tus límites. Intentá nuevamente."
    if not candidates:
        return f"No encontré un límite vigente de {category}."
    logger.info("limit_resolution intent=change_limit reference=category candidates=%s",
                len(candidates))
    if len(candidates) > 1:
        await ConversationService.set_pending_selection(
            sender_phone, PendingSelection("change_limit", "limit", candidates, extracted_data)
        )
        return _limit_selection_reply(category, candidates)
    return await _change_limit_candidate(sender_phone, candidates[0], extracted_data)


async def _handle_list_limits(sender_phone: str) -> str:
    from app.models.database import SessionLocal, Usuario

    session = SessionLocal()
    try:
        user = session.query(Usuario).filter(Usuario.whatsapp_id == sender_phone).first()
        if user is None:
            return "No encontré tu cuenta."
        result = await asyncio.to_thread(LimitService.list_limits, user.id)
        if result.status == "error":
            return "Hubo un problema consultando tus límites."
        await ConversationService.set_recent_items(
            sender_phone,
            RecentItems("limit", [
                {"id": entry.id, "label": entry.category_name,
                 "category": entry.category_name, "amount": str(entry.amount),
                 "month": entry.month, "year": entry.year, "currency": entry.currency}
                for entry in result.limits if entry.id
            ]),
        )
        return _limit_list_reply(result)
    except Exception as exc:
        print(f"[LIMIT_LIST] Error: {type(exc).__name__}: {exc}")
        return "Hubo un problema consultando tus límites."
    finally:
        session.close()


def _user_id_by_phone(sender_phone: str):
    session = SessionLocal()
    try:
        user = session.query(Usuario).filter(Usuario.whatsapp_id == sender_phone).first()
        return user.id if user is not None else None
    finally:
        session.close()


def _budget_reference_date(extracted_data: dict) -> date | None:
    today = datetime.now(ARGENTINA_TZ).date()
    month = extracted_data.get("limit_month")
    year = extracted_data.get("limit_year")
    if year is not None and month is None:
        return None
    month = month or today.month
    year = year or today.year
    try:
        return date(int(year), int(month), 1)
    except (TypeError, ValueError):
        return None


async def _handle_budget_query(sender_phone: str, extracted_data: dict) -> str:
    reference_date = _budget_reference_date(extracted_data)
    if reference_date is None:
        return "¿Para qué mes querés consultar el presupuesto?"
    user_id = await asyncio.to_thread(_user_id_by_phone, sender_phone)
    if user_id is None:
        return "No encontré tu cuenta."
    category_name = extracted_data.get("limit_category")
    currency = extracted_data.get("limit_currency") or "ARS"
    if category_name:
        result = await asyncio.to_thread(
            BudgetService.get_status,
            user_id,
            category_name,
            reference_date,
            currency,
        )
        if result.status == "ok" and result.budget is not None:
            return _budget_status_reply(result.budget)
        if result.status == "category_not_found":
            return f"No encontré una categoría activa llamada {category_name}."
        if result.status == "not_found":
            label = _limit_month_label(reference_date.month, reference_date.year)
            return f"No tenés un límite de {category_name} para {label} en {currency}."
        if result.status == "invalid_data":
            return "La categoría o moneda de la consulta no es válida."
        return "Hubo un problema consultando tu presupuesto. Intentá nuevamente."

    result = await asyncio.to_thread(
        BudgetService.list_statuses,
        user_id,
        reference_date,
        currency,
    )
    if result.status != "ok":
        return "Hubo un problema consultando tus presupuestos. Intentá nuevamente."
    if not result.budgets:
        label = _limit_month_label(reference_date.month, reference_date.year)
        return f"No tenés presupuestos definidos para {label} en {currency}."
    return "\n\n".join(_budget_status_reply(status) for status in result.budgets)


async def _handle_budget_compensation(sender_phone: str, extracted_data: dict) -> str:
    user_id = await asyncio.to_thread(_user_id_by_phone, sender_phone)
    if user_id is None:
        return "No encontré tu cuenta."
    raw_amount = extracted_data.get("compensation_amount")
    requested_amount = Decimal(str(raw_amount)) if raw_amount is not None else None
    result = await asyncio.to_thread(
        BudgetCompensationService.build_proposal,
        user_id,
        target_category=extracted_data.get("compensation_target"),
        source_category=extracted_data.get("compensation_source"),
        requested_amount=requested_amount,
        currency=extracted_data.get("limit_currency"),
    )
    if result.status == "ok" and result.proposal is not None:
        await ConversationService.set_pending_compensation(
            sender_phone, result.proposal.to_dict()
        )
        extracted_data["_conversation_event_key"] = "budget.compensation_proposed"
        extracted_data["_conversation_event_variables"] = {
            "summary": _compensation_summary(result.proposal)
        }
        return _compensation_reply(result.proposal)
    if result.status == "no_excess":
        return "No veo categorías excedidas en el período actual."
    if result.status == "no_funds":
        return (
            "No hay otras categorías con saldo disponible para compensar. "
            "Podés ajustar el límite de la categoría excedida para darle más margen."
        )
    return "No pude calcular la compensación. Intentá nuevamente."


def _format_query_movements_reply(
    result: MovementQueryResult,
    filters: dict[str, Any],
    dashboard_link_url: str | None = None,
    link_ttl_minutes: int = 10,
) -> str:
    if result.status == "user_not_found":
        return "No encontré una cuenta vinculada a este WhatsApp."
    if result.status == "invalid_filters":
        return f"No pude realizar la consulta: {result.message}."
    if result.status != "ok":
        return "Hubo un problema al consultar tus movimientos. Por favor, intentá nuevamente."

    if not result.movements:
        movement_type = filters.get("movement_type")
        category_name = filters.get("category_name")
        if movement_type == "egreso" and category_name:
            return f"No encontré gastos registrados en la categoría *{category_name}*."
        elif movement_type == "egreso":
            return "No encontré gastos registrados."
        elif movement_type == "ingreso" and category_name:
            return f"No encontré ingresos registrados en la categoría *{category_name}*."
        elif movement_type == "ingreso":
            return "No encontré ingresos registrados."
        elif category_name:
            return f"No encontré movimientos registrados en la categoría *{category_name}*."
        return "No tenés movimientos registrados todavía."

    movement_type = filters.get("movement_type")
    category_name = filters.get("category_name")
    if movement_type == "egreso" and category_name:
        title = f"📋 *Tus últimos gastos en {category_name}:*"
    elif movement_type == "egreso":
        title = "📋 *Tus últimos gastos:*"
    elif movement_type == "ingreso" and category_name:
        title = f"📋 *Tus últimos ingresos en {category_name}:*"
    elif movement_type == "ingreso":
        title = "📋 *Tus últimos ingresos:*"
    elif category_name:
        title = f"📋 *Tus últimos movimientos en {category_name}:*"
    else:
        title = "📋 *Tus últimos movimientos:*"

    lines = [title, ""]
    movements_to_display = result.movements[:5]
    for mov in movements_to_display:
        sign = "+" if mov.tipo == "ingreso" else "-"
        date_str = mov.fecha_movimiento.strftime("%d/%m/%Y")
        desc = mov.descripcion or mov.tipo.capitalize()
        cat_suffix = f" ({mov.categoria_nombre})" if mov.categoria_nombre and not category_name else ""
        lines.append(f"• {date_str} - {desc}: {sign}${_format_amount(mov.cantidad)} {mov.moneda}{cat_suffix}")

    if result.total_found > len(movements_to_display):
        lines.append("")
        lines.append(f"Mostrando los últimos {len(movements_to_display)} de {result.total_found} movimientos.")

    if dashboard_link_url:
        lines.append("")
        lines.append("🔗 *Ver este período en tu dashboard:*")
        lines.append(dashboard_link_url)
        lines.append(f"_(El enlace vence en {link_ttl_minutes} minutos y sólo se puede usar una vez)_")

    return "\n".join(lines)


async def _handle_query_movements(sender_phone: str, extracted_data: dict) -> str:
    user_id = await asyncio.to_thread(_user_id_by_phone, sender_phone)
    if user_id is None:
        return "No encontré una cuenta vinculada a este WhatsApp."

    reply_text = str(extracted_data.get("reply_text") or "").strip()
    has_question = ("?" in reply_text or "¿" in reply_text)
    has_filters = any([
        extracted_data.get("movement_type"),
        extracted_data.get("category"),
        extracted_data.get("date_from"),
        extracted_data.get("date_to"),
    ])
    if has_question and not has_filters:
        return reply_text

    movement_type = extracted_data.get("movement_type")
    category_name = extracted_data.get("category")

    raw_start = extracted_data.get("date_from") or extracted_data.get("start_date")
    start_date = None
    if raw_start:
        try:
            start_date = date.fromisoformat(str(raw_start).strip())
        except ValueError:
            return "No pude interpretar la fecha inicial de la consulta. ¿Podrías indicarme el período nuevamente?"

    raw_end = extracted_data.get("date_to") or extracted_data.get("end_date")
    end_date = None
    if raw_end:
        try:
            end_date = date.fromisoformat(str(raw_end).strip())
        except ValueError:
            return "No pude interpretar la fecha final de la consulta. ¿Podrías indicarme el período nuevamente?"

    limit_val = extracted_data.get("limit")
    limit = 5
    if limit_val is not None:
        try:
            limit = min(int(limit_val), 5)
        except (ValueError, TypeError):
            limit = 5

    filters = {
        "movement_type": movement_type,
        "category_name": category_name,
        "start_date": start_date,
        "end_date": end_date,
        "limit": limit,
    }

    try:
        with track_phase("db"):
            result = await asyncio.to_thread(
                FinanceService.query_movements,
                user_id,
                movement_type=movement_type,
                category_name=category_name,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
    except Exception as exc:
        print(f"[QUERY_MOVEMENTS_DISPATCHER] Error: {type(exc).__name__}: {exc}")
        return "Hubo un problema al consultar tus movimientos. Por favor, intentá nuevamente."

    if result.status == "ok":
        await ConversationService.set_recent_items(
            sender_phone, RecentItems("movement", _movement_context_items(result.movements[:5]))
        )

    dashboard_link_url = None
    link_ttl_minutes = 10
    cantidad_mostrada = len(result.movements[:5])
    has_date_filter = bool(start_date or end_date)
    if result.status == "ok" and result.total_found > cantidad_mostrada and has_date_filter:
        try:
            link_result = await asyncio.to_thread(
                DashboardLinkService.generate_or_reuse,
                sender_phone,
                date_from=start_date,
                date_to=end_date,
            )
            if link_result.decision == DashboardLinkDecision.SEND_LINK and link_result.login_url:
                dashboard_link_url = link_result.login_url
                link_ttl_minutes = link_result.link_ttl_minutes
        except Exception as exc:
            print(f"[DASHBOARD_LINK_QUERY] Controlled error: {type(exc).__name__}: {exc}")
            dashboard_link_url = None

    return _format_query_movements_reply(
        result,
        filters,
        dashboard_link_url=dashboard_link_url,
        link_ttl_minutes=link_ttl_minutes,
    )


async def _handle_delete_limit(sender_phone: str, extracted_data: dict) -> str:
    category_name = extracted_data.get("limit_category")
    month = extracted_data.get("limit_month")
    year = extracted_data.get("limit_year")
    currency = extracted_data.get("limit_currency")
    if not category_name:
        await ConversationService.set_pending_limit_delete_category(
            sender_phone,
            PendingLimitDelete(
                sender_phone=sender_phone,
                category_name=None,
                month=month,
                year=year,
                currency=currency,
            ),
        )
        return "¿Qué límite querés eliminar? Indicame la categoría."
    result = await asyncio.to_thread(
        LimitService.delete_limit,
        sender_phone,
        category_name,
        month=month,
        year=year,
        currency=currency,
    )
    if result.status == "needs_month_selection":
        await ConversationService.set_pending_limit_delete(
            sender_phone,
            PendingLimitDelete(
                sender_phone=sender_phone,
                category_name=result.category_name or category_name,
                candidates=result.candidates,
            ),
        )
    elif result.status == "deleted":
        await _clear_deleted_last_limit(sender_phone, result.limit_id)
    return _limit_delete_reply(result, category_name)


async def _clear_deleted_last_limit(sender_phone: str, deleted_limit_id: str | None) -> None:
    if not deleted_limit_id:
        return
    last_limit = await ConversationService.get_last_limit(sender_phone)
    if last_limit is not None and last_limit.limit_id == deleted_limit_id:
        await ConversationService.clear_last_limit(sender_phone)


async def _delete_selected_limits(
    sender_phone: str, pending: PendingLimitDelete, selected: list[dict]
) -> DispatchResult:
    candidates = [{**item, "category": pending.category_name} for item in selected]
    result = await asyncio.to_thread(
        LimitService.delete_limits_by_ids, sender_phone, candidates
    )
    logger.info("limit_mutation intent=delete_limit reference=shown_ids candidates=%s status=%s",
                len(candidates), result.status)
    if result.status == "deleted":
        await ConversationService.clear_state(sender_phone)
        for item in result.deleted:
            await _clear_deleted_last_limit(sender_phone, item["limit_id"])
        periods = ", ".join(
            _limit_month_label(item["month"], item["year"])
            for item in result.deleted
        )
        reply = (
            f"✅ Listo, eliminé el límite de {pending.category_name}: {periods}."
            if len(result.deleted) == 1 else
            f"✅ Eliminé los {len(result.deleted)} límites de "
            f"{pending.category_name}: {periods}."
        )
        variables = {"category": pending.category_name, "periods": periods,
                     "count": len(result.deleted)}
    elif result.status == "stale_context":
        await ConversationService.clear_state(sender_phone)
        reply = "La lista de límites cambió. Volvé a consultar cuáles querés borrar."
        variables = {}
    else:
        reply = "No pude eliminar esos límites. No borré ninguno."
        variables = {}
    return DispatchResult(
        reply_text=reply, service_invoked="limit", intent="delete_limit",
        event_key="limit.bulk_deleted" if result.status == "deleted" else None,
        event_variables=variables,
    )


# ---------------------------------------------------------------------------
# Dispatch pipeline
# ---------------------------------------------------------------------------


async def _dispatch_incoming_message(
    sender_phone: str,
    text_body: str,
    whatsapp_message_id: str | None = None,
    conversation_history: list[dict[str, str]] | None = None,
) -> DispatchResult:
    """
    Process an incoming text message through the full dispatch pipeline.

    This includes:
    1. Onboarding check
    2. /link command interception
    3. Multi-turn state checks (rename, reminder data)
    4. LLM processing
    5. Intent dispatch (financial movement, reminders, categories, etc.)

    Args:
        sender_phone: The sender's WhatsApp phone number.
        text_body: The raw text body of the message.
        whatsapp_message_id: Optional WhatsApp message ID for dedup.
        conversation_history: Up to four previous visible turns (eight messages).

    Returns:
        DispatchResult with reply_text and debug metadata.
    """
    with track_phase("db"):
        onboarding_result = OnboardingService.prepare_whatsapp_message(sender_phone)
    if onboarding_result.decision == OnboardingDecision.SEND_INVITATION:
        return DispatchResult(
            reply_text=_onboarding_invitation_reply(
                onboarding_result.registration_url,
                onboarding_result.invitation_ttl_minutes,
            ),
            service_invoked="onboarding",
            event_key="onboarding.invitation",
            event_variables={
                "registration_url": onboarding_result.registration_url,
                "ttl_minutes": onboarding_result.invitation_ttl_minutes,
            },
        )
    if onboarding_result.decision == OnboardingDecision.SUPPRESS_RESPONSE:
        return DispatchResult(reply_text="", service_invoked="onboarding")
    if onboarding_result.decision == OnboardingDecision.ERROR:
        return DispatchResult(
            reply_text="No pude verificar tu cuenta. Intentá nuevamente en unos minutos.",
            service_invoked="onboarding",
            event_key="onboarding.error",
        )

    # ------------------------------------------------------------------
    # Comando exacto para pedir un enlace de acceso al dashboard.
    # ------------------------------------------------------------------
    if text_body.strip().lower() == "/link":
        dashboard_link_result = DashboardLinkService.generate_or_reuse(sender_phone)
        if dashboard_link_result.decision == DashboardLinkDecision.SEND_LINK:
            reply_text = _dashboard_link_reply(
                dashboard_link_result.login_url,
                dashboard_link_result.link_ttl_minutes,
            )
            event_key = "dashboard.link.sent"
            event_variables = {
                "login_url": dashboard_link_result.login_url,
                "ttl_minutes": dashboard_link_result.link_ttl_minutes,
            }
        elif dashboard_link_result.decision == DashboardLinkDecision.NOT_ELIGIBLE:
            reply_text = _DASHBOARD_LINK_NOT_ELIGIBLE_REPLY
            event_key = "dashboard.link.not_eligible"
            event_variables = {}
        elif dashboard_link_result.decision == DashboardLinkDecision.ERROR:
            reply_text = "No pude generar tu enlace. Intentá nuevamente en unos minutos."
            event_key = "dashboard.link.error"
            event_variables = {}
        else:  # SUPPRESS_RESPONSE
            reply_text = ""
            event_key = None
            event_variables = {}
        return DispatchResult(
            reply_text=reply_text,
            service_invoked="dashboard_link",
            event_key=event_key,
            event_variables=event_variables,
        )

    # Track last message time for 24h window
    _update_ultimo_mensaje(sender_phone)

    command = text_body.strip().lower()
    if command in {"/movimientos", "/egresos"}:
        await ConversationService.clear_pending_selection(sender_phone)
        query = {"intent": "query_movements"}
        if command == "/egresos":
            query["movement_type"] = "egreso"
        reply = await _handle_query_movements(sender_phone, query)
        return DispatchResult(reply, service_invoked="finance", intent="query_movements")

    selection_reply = re.search(
        r"\b(?:primero|primera|segundo|segunda|tercero|tercera|ambos|"
        r"los dos|todos|ninguno|cancelar|el de)\b",
        text_body.lower(),
    )
    explicit_new_request = re.search(
        r"\b(?:compr\w*|gast\w*|pagu\w*|registr\w*|crea\w*|"
        r"borr\w*|elimin\w*|modific\w*|cambi\w*|mostr\w*|list\w*)\b",
        text_body.lower(),
    )
    pending_selection = (
        await ConversationService.get_pending_selection(sender_phone)
        if selection_reply or explicit_new_request
        or _extract_month_from_text(text_body) is not None else None
    )
    if pending_selection is not None:
        if text_body.strip().lower() in {"cancelar", "cancelalo", "ninguno", "ninguna"}:
            await ConversationService.clear_pending_selection(sender_phone)
            return DispatchResult("Listo, no hice ningún cambio.", service_invoked="conversation")
        selected = select_items(
            text_body, pending_selection.items,
            allow_all=(pending_selection.entity == "movement"
                       and pending_selection.intent == "delete_movement"),
        )
        if (len(selected) > 1 and pending_selection.entity == "movement"
                and pending_selection.intent == "delete_movement"):
            await ConversationService.clear_pending_selection(sender_phone)
            reply = await _apply_movement_batch_action(sender_phone, selected)
            return DispatchResult(reply, service_invoked="finance", intent="delete_movement")
        if len(selected) == 1 and pending_selection.entity == "movement":
            await ConversationService.clear_pending_selection(sender_phone)
            event_data = {}
            reply = await _apply_movement_action(
                sender_phone, pending_selection.intent, selected[0]["id"],
                pending_selection.changes, event_data, selected[0],
            )
            return DispatchResult(
                reply, service_invoked="finance", intent=pending_selection.intent,
                event_key=event_data.get("_conversation_event_key"),
                event_variables=event_data.get("_conversation_event_variables", {}),
            )
        if len(selected) == 1 and pending_selection.entity == "limit":
            await ConversationService.clear_pending_selection(sender_phone)
            reply = await _change_limit_candidate(
                sender_phone, selected[0], pending_selection.changes
            )
            return DispatchResult(reply, service_invoked="limit", intent="change_limit")
        if explicit_new_request and not selected:
            await ConversationService.clear_pending_selection(sender_phone)
            pending_selection = None
    if pending_selection is not None:
        if len(text_body.split()) <= 5:
            options = "\n".join(
                f"{index}. {item['label']} — ${item['amount']} {item['currency']}"
                for index, item in enumerate(pending_selection.items, 1)
            )
            return DispatchResult(
                f"¿Cuál querés elegir?\n{options}", service_invoked="conversation"
            )
        await ConversationService.clear_pending_selection(sender_phone)

    # Un mensaje puede abandonar un flujo multi-turno y continuar por el
    # dispatcher general. Memorizar la clasificación garantiza que ese
    # fall-through no vuelva a facturar ni reintentar el mismo mensaje.
    llm_result_cache: dict | None = None
    last_limit_cache: LastCreatedLimit | None = None
    last_limit_loaded = False

    async def get_last_limit_once() -> LastCreatedLimit | None:
        nonlocal last_limit_cache, last_limit_loaded
        if not last_limit_loaded:
            last_limit_cache = await ConversationService.get_last_limit(sender_phone)
            last_limit_loaded = True
        return last_limit_cache

    async def extract_message_once() -> dict:
        nonlocal llm_result_cache
        if llm_result_cache is None:
            context = build_user_context(sender_phone)
            recent_limit = (
                await get_last_limit_once()
                if references_recent_limit(text_body)
                else None
            )
            if recent_limit is not None:
                context += (
                    "\nÚLTIMO LÍMITE CREADO: "
                    f"categoría={recent_limit.category_name}; "
                    f"monto={recent_limit.amount}; "
                    f"mes={recent_limit.month}; año={recent_limit.year}; "
                    f"moneda={recent_limit.currency}."
                )
            if re.search(r"\b(era|fue|en realidad|ese|esa|último|ultimo|borr|elimin|modific|cambi)", text_body.lower()):
                last_movement = await ConversationService.get_last_movement(sender_phone)
                if last_movement is not None:
                    context += (
                        "\nÚLTIMO MOVIMIENTO REGISTRADO: "
                        f"descripción={last_movement.description}; "
                        f"monto={last_movement.amount}; "
                        f"categoría={last_movement.category_name}; "
                        f"moneda={last_movement.currency}."
                    )
                recent_items = await ConversationService.get_recent_items(sender_phone)
                if recent_items is not None and recent_items.items:
                    summary = "; ".join(
                        f"{item.get('label')} ({item.get('amount')} {item.get('currency')}, "
                        f"mes {item.get('month', 'sin especificar')})"
                        for item in recent_items.items[:5]
                    )
                    context += f"\nELEMENTOS MOSTRADOS ({recent_items.entity}): {summary}."
            with track_phase("llm"):
                llm_result_cache = await LLMService.process_message(
                    text_body,
                    context=context,
                    history=conversation_history,
                )
        return llm_result_cache

    # ----------------------------------------------------------
    # Multi-turn: renombrar recordatorio por título duplicado
    # ----------------------------------------------------------
    is_awaiting_rename = await ConversationService.is_awaiting_rename(sender_phone)

    if is_awaiting_rename:
        pending = await ConversationService.get_pending_rename(sender_phone)
        if pending is None:
            await ConversationService.clear_state(sender_phone)
            reply_text = "Se perdió el contexto. Podés volver a crear el recordatorio."
        else:
            extracted_data = await extract_message_once()
            if extracted_data.get("intent") == "reset_context":
                return await _handle_reset_context(sender_phone)
            new_concept = text_body.strip()
            if not new_concept:
                reply_text = "¿Qué nombre querés usar para el recordatorio?"
            else:
                llm_data = {
                    "reminder_concept": new_concept,
                    "reminder_day": pending.reminder_day,
                    "reminder_amount": float(pending.reminder_amount) if pending.reminder_amount else None,
                    "reminder_currency": pending.reminder_currency,
                }
                reminder_result = ReminderService.create_reminder(
                    sender_phone=sender_phone,
                    llm_result=llm_data,
                )
                if reminder_result.status != "duplicate_title":
                    await ConversationService.clear_state(sender_phone)
                reply_text = _reminder_creation_reply(reminder_result, llm_data)
        return DispatchResult(reply_text=reply_text, service_invoked="conversation")

    # ----------------------------------------------------------
    # Multi-turn: si estamos esperando datos de recordatorio
    # ----------------------------------------------------------
    is_awaiting_reminder = await ConversationService.is_awaiting_reminder_data(sender_phone)

    if is_awaiting_reminder:
        pending = await ConversationService.get_pending_reminder(sender_phone)
        if pending is None:
            await ConversationService.clear_state(sender_phone)
            reply_text = "Se perdió el contexto. Podés volver a crear el recordatorio."
        else:
            # Extraer día del texto usando LLM
            extracted_data = await extract_message_once()
            if extracted_data.get("intent") == "reset_context":
                return await _handle_reset_context(sender_phone)
            new_day = extracted_data.get("reminder_day")
            # Fallback: extraer número del texto
            if new_day is None:
                match = re.search(r'\b(\d{1,2})\b', text_body)
                if match:
                    candidate = int(match.group(1))
                    if 1 <= candidate <= 31:
                        new_day = candidate

            if new_day is None:
                reply_text = "Necesito un día del mes (1 al 31). ¿Qué día vence?"
            else:
                llm_data = {
                    "reminder_concept": pending.reminder_concept,
                    "reminder_day": new_day,
                    "reminder_amount": float(pending.reminder_amount) if pending.reminder_amount else None,
                    "reminder_currency": pending.reminder_currency,
                }
                reminder_result = ReminderService.create_reminder(
                    sender_phone=sender_phone,
                    llm_result=llm_data,
                )
                await ConversationService.clear_state(sender_phone)
                reply_text = _reminder_creation_reply(reminder_result, llm_data)
        return DispatchResult(reply_text=reply_text, service_invoked="conversation")

    # ----------------------------------------------------------
    # Multi-turn: confirmar el año de un límite para un mes pasado
    # ----------------------------------------------------------
    is_awaiting_limit_year = await ConversationService.is_awaiting_limit_year_confirmation(sender_phone)

    if is_awaiting_limit_year:
        pending = await ConversationService.get_pending_limit(sender_phone)
        if pending is None:
            await ConversationService.clear_state(sender_phone)
            return DispatchResult(
                reply_text="Se perdió el contexto. Podés volver a crear el límite.",
                service_invoked="conversation",
            )
        extracted_data = await extract_message_once()
        if extracted_data.get("intent") == "reset_context":
            return await _handle_reset_context(sender_phone)
        if extracted_data.get("error"):
            return DispatchResult(
                reply_text=extracted_data.get("reply_text") or (
                    "No he podido analizar tu mensaje en este momento."
                ),
                raw_llm_response=extracted_data,
                service_invoked="llm",
                intent=extracted_data.get("intent", "out_of_scope"),
            )
        intent = extracted_data.get("intent", "out_of_scope")
        if intent == "reject_limit" or _is_cancel_request(text_body):
            await ConversationService.clear_state(sender_phone)
            return DispatchResult(
                reply_text="Listo, no creé ningún límite de gasto.",
                service_invoked="conversation",
            )
        if intent == "confirm_limit" or (
            intent in ("out_of_scope", "greeting") and _is_confirm_request(text_body)
        ):
            reply_text = await _handle_create_limit(
                sender_phone,
                _limit_base_data(pending),
                last_limit=_last_limit_from_pending(pending),
                edit=pending.is_edit,
            )
            return DispatchResult(reply_text=reply_text, service_invoked="conversation")
        # El mensaje no responde la confirmación de año (saludo, gasto, otro tema):
        # el flujo del límite quedó abandonado. Limpiar el estado para que no
        # secuestre los mensajes siguientes y procesar normalmente (fall-through).
        await ConversationService.clear_state(sender_phone)

    # ----------------------------------------------------------
    # Multi-turn: confirmar creación de categoría canónica
    # ----------------------------------------------------------
    is_awaiting_limit_category = (
        await ConversationService.is_awaiting_limit_category_confirmation(sender_phone)
    )

    if is_awaiting_limit_category:
        pending = await ConversationService.get_pending_limit(sender_phone)
        if pending is None:
            await ConversationService.clear_state(sender_phone)
            return DispatchResult(
                reply_text="Se perdió el contexto. Podés volver a crear el límite.",
                service_invoked="conversation",
            )
        extracted_data = await extract_message_once()
        if extracted_data.get("intent") == "reset_context":
            return await _handle_reset_context(sender_phone)
        if extracted_data.get("error"):
            return DispatchResult(
                reply_text=extracted_data.get("reply_text") or (
                    "No he podido analizar tu mensaje en este momento."
                ),
                raw_llm_response=extracted_data,
                service_invoked="llm",
                intent=extracted_data.get("intent", "out_of_scope"),
            )
        intent = extracted_data.get("intent", "out_of_scope")
        if intent == "reject_limit" or _is_cancel_request(text_body):
            await ConversationService.clear_state(sender_phone)
            return DispatchResult(
                reply_text="Listo, no creé la categoría ni el límite.",
                service_invoked="conversation",
            )
        if intent in {"confirm_limit", "confirm_category"} or (
            _is_category_creation_confirmation(text_body)
        ):
            reply_text = await _handle_create_limit(
                sender_phone,
                _limit_base_data(pending),
                last_limit=_last_limit_from_pending(pending),
                edit=pending.is_edit,
                allow_category_creation=True,
            )
            return DispatchResult(reply_text=reply_text, service_invoked="conversation")
        alternative_category = (
            extracted_data.get("limit_category")
            or _extract_category_alternative(text_body)
        )
        if (
            isinstance(alternative_category, str)
            and alternative_category.strip()
            and alternative_category.strip().casefold()
            != (pending.category or "").strip().casefold()
        ):
            replacement_data = _limit_base_data(pending)
            replacement_data["limit_category"] = alternative_category.strip()
            reply_text = await _handle_create_limit(
                sender_phone,
                replacement_data,
                last_limit=_last_limit_from_pending(pending),
                edit=pending.is_edit,
            )
            return DispatchResult(
                reply_text=reply_text,
                service_invoked="conversation",
            )
        await ConversationService.clear_state(sender_phone)

    # ----------------------------------------------------------
    # Multi-turn: completar categoría y/o monto del límite
    # ----------------------------------------------------------
    is_awaiting_limit_data = await ConversationService.is_awaiting_limit_data(sender_phone)

    if is_awaiting_limit_data:
        pending = await ConversationService.get_pending_limit(sender_phone)
        if pending is None:
            await ConversationService.clear_state(sender_phone)
            reply_text = "Se perdió el contexto. Podés volver a crear el límite."
        else:
            extracted_data = await extract_message_once()
            if extracted_data.get("intent") == "reset_context":
                return await _handle_reset_context(sender_phone)
            intent = extracted_data.get("intent", "out_of_scope")

            if intent == "reject_limit" or _is_cancel_request(text_body):
                await ConversationService.clear_state(sender_phone)
                reply_text = "Listo, cancelé la configuración del límite de gasto."
            else:
                base = _limit_base_data(pending)
                if base["limit_category"] is None:
                    base["limit_category"] = (
                        extracted_data.get("limit_category")
                        or _extract_category_from_text(text_body)
                    )
                if base["limit_amount"] is None:
                    base["limit_amount"] = (
                        extracted_data.get("limit_amount")
                        or _extract_amount_from_text(text_body)
                    )
                if extracted_data.get("limit_month") is not None:
                    base["limit_month"] = extracted_data.get("limit_month")
                if extracted_data.get("limit_year") is not None:
                    base["limit_year"] = extracted_data.get("limit_year")
                reply_text = await _handle_create_limit(
                    sender_phone,
                    base,
                    last_limit=_last_limit_from_pending(pending),
                    edit=pending.is_edit,
                )
        return DispatchResult(reply_text=reply_text, service_invoked="conversation")

    # ----------------------------------------------------------
    # Multi-turn: el usuario debe indicar la categoría a eliminar
    # ----------------------------------------------------------
    is_awaiting_delete_category = await ConversationService.is_awaiting_limit_delete_category(sender_phone)

    if is_awaiting_delete_category:
        pending_delete = await ConversationService.get_pending_limit_delete(sender_phone)
        if pending_delete is None:
            await ConversationService.clear_state(sender_phone)
            reply_text = "Se perdió el contexto de la eliminación. Volvé a indicarme qué límite querés eliminar."
        else:
            extracted_data = await extract_message_once()
            if extracted_data.get("intent") == "reset_context":
                return await _handle_reset_context(sender_phone)
            if _is_cancel_request(text_body):
                await ConversationService.clear_state(sender_phone)
                return DispatchResult(
                    reply_text="Listo, cancelé la eliminación del límite.",
                    service_invoked="conversation",
                )
            category_name = extracted_data.get("limit_category") or _extract_category_from_text(text_body)
            if not category_name:
                reply_text = "¿Qué límite querés eliminar? Indicame la categoría."
            else:
                result = await asyncio.to_thread(
                    LimitService.delete_limit,
                    sender_phone,
                    category_name,
                    month=pending_delete.month,
                    year=pending_delete.year,
                    currency=pending_delete.currency,
                )
                if result.status == "needs_month_selection":
                    await ConversationService.set_pending_limit_delete(
                        sender_phone,
                        PendingLimitDelete(
                            sender_phone=sender_phone,
                            category_name=result.category_name or category_name,
                            candidates=result.candidates,
                        ),
                    )
                else:
                    await ConversationService.clear_state(sender_phone)
                    if result.status == "deleted":
                        await _clear_deleted_last_limit(sender_phone, result.limit_id)
                reply_text = _limit_delete_reply(result, category_name)
        return DispatchResult(reply_text=reply_text, service_invoked="conversation")

    # ----------------------------------------------------------
    # Multi-turn: elegir el mes del límite a eliminar
    # ----------------------------------------------------------
    is_awaiting_limit_month = await ConversationService.is_awaiting_limit_month_selection(sender_phone)
    if (
        is_awaiting_limit_month and explicit_new_request
        and not selects_all(text_body)
        and _extract_month_from_text(text_body) is None
        and not re.search(r"\b(?:primero|segundo|tercero)\b", text_body.lower())
    ):
        await ConversationService.clear_state(sender_phone)
        is_awaiting_limit_month = False

    if is_awaiting_limit_month:
        pending_delete = await ConversationService.get_pending_limit_delete(sender_phone)
        if pending_delete is None:
            await ConversationService.clear_state(sender_phone)
            reply_text = "Se perdió el contexto de la eliminación. Volvé a indicarme qué límite querés eliminar."
        else:
            extracted_data = await extract_message_once()
            if extracted_data.get("intent") == "reset_context":
                return await _handle_reset_context(sender_phone)
            if _is_cancel_request(text_body):
                await ConversationService.clear_state(sender_phone)
                return DispatchResult(
                    reply_text="Listo, cancelé la eliminación del límite.",
                    service_invoked="conversation",
                )
            if selects_all(text_body):
                return await _delete_selected_limits(
                    sender_phone, pending_delete, pending_delete.candidates
                )
            named_months = select_named_months(text_body, pending_delete.candidates)
            if len(named_months) > 1:
                return await _delete_selected_limits(sender_phone, pending_delete, named_months)
            month = extracted_data.get("limit_month")
            if month is None:
                month = _extract_month_from_text(text_body)
            if month is None:
                reply_text = _limit_selection_reply(
                    pending_delete.category_name,
                    pending_delete.candidates,
                )
            else:
                selected_year = extracted_data.get("limit_year")
                selected_currency = extracted_data.get("limit_currency")
                if isinstance(selected_currency, str):
                    selected_currency = selected_currency.strip().upper() or None
                matching_candidates = [
                    candidate
                    for candidate in pending_delete.candidates
                    if candidate.get("month") == month
                    and (
                        selected_year is None
                        or candidate.get("year") == selected_year
                    )
                    and (
                        selected_currency is None
                        or candidate.get("currency", "ARS") == selected_currency
                    )
                ]
                if len(matching_candidates) != 1:
                    reply_text = _limit_selection_reply(
                        pending_delete.category_name,
                        pending_delete.candidates,
                    )
                    return DispatchResult(
                        reply_text=reply_text,
                        service_invoked="conversation",
                    )
                return await _delete_selected_limits(
                    sender_phone, pending_delete, matching_candidates
                )
        return DispatchResult(reply_text=reply_text, service_invoked="conversation")

    # ----------------------------------------------------------
    # Multi-turn: confirmar una compensación de presupuesto
    # ----------------------------------------------------------
    is_awaiting_compensation = (
        await ConversationService.is_awaiting_compensation_confirmation(sender_phone)
    )

    if is_awaiting_compensation:
        pending = await ConversationService.get_pending_compensation(sender_phone)
        if pending is None:
            await ConversationService.clear_state(sender_phone)
            return DispatchResult(
                reply_text=(
                    "Se perdió el contexto. Podés pedirme una nueva compensación."
                ),
                service_invoked="conversation",
            )
        extracted_data = await extract_message_once()
        if extracted_data.get("intent") == "reset_context":
            return await _handle_reset_context(sender_phone)
        if extracted_data.get("error"):
            return DispatchResult(
                reply_text=extracted_data.get("reply_text") or (
                    "No he podido analizar tu mensaje en este momento."
                ),
                raw_llm_response=extracted_data,
                service_invoked="llm",
                intent=extracted_data.get("intent", "out_of_scope"),
            )
        intent = extracted_data.get("intent", "out_of_scope")
        if intent == "reject_compensation" or _is_cancel_request(text_body):
            await ConversationService.clear_state(sender_phone)
            return DispatchResult(
                reply_text="Listo, no cambié ningún límite.",
                service_invoked="conversation",
            )
        if intent == "confirm_compensation" or (
            intent in ("out_of_scope", "greeting") and _is_confirm_request(text_body)
        ):
            apply_result = await asyncio.to_thread(
                BudgetCompensationService.apply, pending.proposal
            )
            await ConversationService.clear_state(sender_phone)
            return DispatchResult(
                reply_text=_compensation_apply_reply(apply_result),
                service_invoked="compensation",
            )
        # El mensaje no responde la confirmación: la propuesta quedó abandonada.
        # Limpiar el estado para que no secuestre los mensajes siguientes y
        # procesar el mensaje normalmente (fall-through).
        await ConversationService.clear_state(sender_phone)

    # Procesar mensaje con LLM (fecha + categorías del usuario como contexto)
    extracted_data = await extract_message_once()
    if extracted_data.get("intent") == "reset_context":
        return await _handle_reset_context(sender_phone)
    if re.match(r"^(?:borra|elimina|anula)\b", normalize_text(text_body)) and (
        recent_count(text_body) is not None or named_movement_targets(text_body)
    ):
        recent_for_action = await ConversationService.get_recent_items(sender_phone)
        if recent_for_action is not None and recent_for_action.entity == "movement":
            extracted_data["intent"] = "delete_movement"
    last_limit_for_routing = None
    if references_recent_limit(text_body):
        last_limit_for_routing = await get_last_limit_once()
    extracted_data = normalize_limit_intent(
        text_body,
        extracted_data,
        last_limit=last_limit_for_routing,
        today=datetime.now(ARGENTINA_TZ).date(),
    )
    extracted_data = normalize_movement_action(text_body, extracted_data)
    extracted_data = normalize_movement_query_intent(
        text_body,
        extracted_data,
    )
    intent = extracted_data.get("intent", "out_of_scope")
    logger.info("conversation_route intent=%s", intent)

    # ----------------------------------------------------------
    # Manejar intents
    # ----------------------------------------------------------
    if intent == "create_limit":
        reply_text = await _handle_create_limit(sender_phone, extracted_data)
        service_invoked = "limit"

    elif intent == "change_limit":
        reply_text = await _handle_change_limit(sender_phone, extracted_data, text_body)
        service_invoked = "limit"

    elif intent == "list_limits":
        reply_text = await _handle_list_limits(sender_phone)
        service_invoked = "limit"

    elif intent == "delete_limit":
        reply_text = await _handle_delete_limit(sender_phone, extracted_data)
        service_invoked = "limit"

    elif intent == "budget_query":
        reply_text = await _handle_budget_query(sender_phone, extracted_data)
        service_invoked = "budget"

    elif intent == "compensate_budget":
        reply_text = await _handle_budget_compensation(sender_phone, extracted_data)
        service_invoked = "compensation"

    elif intent == "query_movements":
        reply_text = await _handle_query_movements(sender_phone, extracted_data)
        service_invoked = "finance"

    elif intent in {"update_movement", "delete_movement"}:
        reply_text = await _handle_movement_action(sender_phone, text_body, extracted_data)
        service_invoked = "finance"

    elif intent == "delete_category":
        reply_text = await _handle_delete_category(sender_phone, extracted_data)
        service_invoked = "finance"

    elif intent == "list_categories":
        reply_text = await _handle_list_categories(sender_phone)
        service_invoked = "finance"

    elif intent == "list_reminders":
        reply_text = await _handle_list_reminders(sender_phone)
        service_invoked = "reminder"

    elif intent == "update_reminder":
        reminder_concept = extracted_data.get("reminder_concept")
        reminder_id = extracted_data.get("reminder_id") or ""
        if reminder_concept:
            try:
                found = ReminderService.find_by_title(sender_phone, reminder_concept)
                if found:
                    reminder_id = str(found[0].id or "")
            except Exception:
                pass
        reminder_result = ReminderService.update_reminder(
            sender_phone=sender_phone,
            reminder_id=reminder_id,
            llm_result=extracted_data,
        )
        reply_text = _reminder_update_reply(reminder_result)
        service_invoked = "reminder"

    elif intent == "pause_reminder":
        concept = extracted_data.get("reminder_concept")
        if concept:
            reminder_result = ReminderService.pause_by_title(
                sender_phone=sender_phone,
                title=concept,
            )
        else:
            reminder_result = ReminderService.pause_reminder(
                sender_phone=sender_phone,
                reminder_id=extracted_data.get("reminder_id") or "",
            )
        reply_text = _reminder_state_reply(reminder_result, "paused")
        service_invoked = "reminder"

    elif intent == "activate_reminder":
        concept = extracted_data.get("reminder_concept")
        if concept:
            reminder_result = ReminderService.activate_by_title(
                sender_phone=sender_phone,
                title=concept,
            )
        else:
            reminder_result = ReminderService.activate_reminder(
                sender_phone=sender_phone,
                reminder_id=extracted_data.get("reminder_id") or "",
            )
        reply_text = _reminder_state_reply(reminder_result, "activated")
        service_invoked = "reminder"

    elif intent == "delete_reminder":
        concept = extracted_data.get("reminder_concept")
        if concept:
            reminder_result = ReminderService.delete_by_title(
                sender_phone=sender_phone,
                title=concept,
            )
        else:
            reminder_result = ReminderService.delete_reminder(
                sender_phone=sender_phone,
                reminder_id=extracted_data.get("reminder_id") or "",
            )
        reply_text = _reminder_delete_reply(reminder_result)
        service_invoked = "reminder"

    elif intent in {"enable_proactive_reminders", "disable_proactive_reminders"}:
        reply_text = _proactive_prompts_reply(
            sender_phone, enabled=intent == "enable_proactive_reminders"
        )
        service_invoked = "reminder"

    elif _is_financial_movement(extracted_data):
        # Nuevo movimiento: registrar inmediatamente con hint
        reply_text = await _register_and_reply_with_hint(
            sender_phone=sender_phone,
            whatsapp_message_id=whatsapp_message_id,
            text_body=text_body,
            extracted_data=extracted_data,
        )
        service_invoked = "finance"

    elif _is_create_reminder(extracted_data):
        validated_concept = _validate_reminder_concept(
            extracted_data.get("reminder_concept"), text_body
        )
        if validated_concept is None:
            reply_text = "¿Qué nombre querés ponerle al recordatorio?"
        elif not extracted_data.get("reminder_day"):
            pending_r = PendingReminder(
                sender_phone=sender_phone,
                reminder_concept=validated_concept,
                reminder_day=None,
                reminder_amount=(
                    Decimal(str(extracted_data["reminder_amount"]))
                    if extracted_data.get("reminder_amount") else None
                ),
                reminder_currency=extracted_data.get("reminder_currency") or "ARS",
            )
            await ConversationService.set_pending_reminder(sender_phone, pending_r)
            display_concept = validated_concept or "ese pago"
            reply_text = f"¿Qué día del mes querés que te avise de {display_concept}?"
        else:
            extracted_data["reminder_concept"] = validated_concept
            reminder_result = ReminderService.create_reminder(
                sender_phone=sender_phone,
                llm_result=extracted_data,
            )
            if reminder_result.status == "duplicate_title":
                pending_r = PendingReminder(
                    sender_phone=sender_phone,
                    reminder_concept=None,
                    reminder_day=extracted_data.get("reminder_day"),
                    reminder_amount=(
                        Decimal(str(extracted_data["reminder_amount"]))
                        if extracted_data.get("reminder_amount") else None
                    ),
                    reminder_currency=extracted_data.get("reminder_currency") or "ARS",
                )
                await ConversationService.set_pending_rename(sender_phone, pending_r)
            print(
                "[REMINDER_CREATION]",
                f"user={sender_phone}",
                f"status={reminder_result.status}",
            )
            reply_text = _reminder_creation_reply(reminder_result, extracted_data)
        service_invoked = "reminder"

    elif intent in ("confirm_category", "reject_category"):
        reply_text = "No encontré un movimiento pendiente para confirmar."
        service_invoked = "conversation"

    elif intent in ("greeting", "out_of_scope", "reminder", "expense_summary"):
        print(f"[{intent.upper()}] User {sender_phone}: {text_body}")
        reply_text = _safe_non_persisted_reply(extracted_data)
        service_invoked = "llm"

    else:
        print(f"[{str(intent).upper()}] User {sender_phone}: {text_body}")
        reply_text = _safe_non_persisted_reply(extracted_data)
        service_invoked = "llm"

    return DispatchResult(
        reply_text=reply_text,
        raw_llm_response=extracted_data,
        service_invoked=service_invoked,
        intent=intent,
        event_key=extracted_data.pop("_conversation_event_key", None),
        event_variables=extracted_data.pop(
            "_conversation_event_variables",
            {},
        ),
    )


async def process_incoming_message(
    sender_phone: str,
    text_body: str,
    whatsapp_message_id: str | None = None,
    conversation_history: list[dict[str, str]] | None = None,
) -> DispatchResult:
    """Route free text, then apply a published presentation when one exists."""
    await ConversationFlowRuntime.abandon(sender_phone)
    result = await _dispatch_incoming_message(
        sender_phone=sender_phone,
        text_body=text_body,
        whatsapp_message_id=whatsapp_message_id,
        conversation_history=conversation_history,
    )
    if result.event_key:
        configured = await ConversationFlowRuntime.render_event(
            sender_phone=sender_phone,
            event_key=result.event_key,
            variables=result.event_variables,
        )
        if configured is not None:
            result.reply_message = configured
    return result


async def process_incoming_interactive_reply(
    *,
    sender_phone: str,
    option_id: str,
    reply_type: str,
    whatsapp_message_id: str | None = None,
):
    del whatsapp_message_id

    async def handle_action(action: str) -> DispatchResult:
        return await _handle_configured_action(sender_phone, action)

    result = await ConversationFlowRuntime.handle_reply(
        sender_phone=sender_phone,
        option_id=option_id,
        reply_type=reply_type,
        action_handler=handle_action,
    )
    if isinstance(result, DispatchResult) and result.event_key:
        configured = await ConversationFlowRuntime.render_event(
            sender_phone=sender_phone,
            event_key=result.event_key,
            variables=result.event_variables,
        )
        if configured is not None:
            result.reply_message = configured
    return result


async def _handle_configured_action(
    sender_phone: str,
    action: str,
) -> DispatchResult:
    if action == "cancel_pending_operation":
        await ConversationService.clear_state(sender_phone)
        return DispatchResult(
            reply_text="Listo, cancelé la operación pendiente.",
            service_invoked="conversation_flow",
        )
    if action in {"reject_category", "reject_limit", "reject_compensation"}:
        await ConversationService.clear_state(sender_phone)
        return DispatchResult(
            reply_text="Listo, no hice ningún cambio.",
            service_invoked="conversation_flow",
        )
    if action == "request_category_change":
        return DispatchResult(
            reply_text="Decime qué categoría querés usar.",
            service_invoked="conversation_flow",
        )
    if action == "confirm_category":
        return await _confirm_pending_category_action(sender_phone)
    if action == "confirm_compensation":
        return await _confirm_pending_compensation_action(sender_phone)
    if action in {"confirm_limit_year", "confirm_limit_category"}:
        return await _confirm_pending_limit_action(sender_phone, action)
    return DispatchResult(
        reply_text="Esa opción ya no está disponible. Escribime qué querés hacer.",
        service_invoked="conversation_flow",
    )


async def _confirm_pending_category_action(sender_phone: str) -> DispatchResult:
    pending = await ConversationService.get_pending_movement(sender_phone)
    if pending is None or not pending.inferred_category:
        return DispatchResult(
            reply_text="Se perdió el contexto. Volvé a registrar el movimiento.",
            service_invoked="conversation_flow",
        )
    with track_phase("db"):
        result = await asyncio.to_thread(
            FinanceService.register_movement_with_category,
            sender_phone=sender_phone,
            whatsapp_message_id=pending.whatsapp_message_id,
            original_text=pending.original_text,
            movement_type=pending.movement_type,
            amount=pending.amount,
            currency=pending.currency,
            description=pending.description,
            category_name=pending.inferred_category,
            create_category_if_missing=True,
            category_creation_confirmed=True,
            fecha_movimiento=resolve_relative_date(
                pending.llm_result_extra.get("fecha"),
                date.today(),  # noqa: DTZ011
            ),
        )
    if result.status in {"registered", "duplicate"}:
        await ConversationService.clear_state(sender_phone)
    reply_text = _registration_dispatch_reply(
        result,
        {
            "movement_type": pending.movement_type,
            "amount": pending.amount,
            "currency": pending.currency,
            "description": pending.description,
        },
    )
    return DispatchResult(
        reply_text=reply_text,
        service_invoked="finance",
        event_key=("movement.registered" if result.status == "registered" else None),
        event_variables={
            "movement_type": pending.movement_type,
            "description": pending.description,
            "amount": _format_amount(pending.amount),
            "currency": pending.currency,
        },
    )


async def _confirm_pending_compensation_action(sender_phone: str) -> DispatchResult:
    pending = await ConversationService.get_pending_compensation(sender_phone)
    if pending is None:
        return DispatchResult(
            reply_text="No encontré una propuesta de compensación pendiente.",
            service_invoked="conversation_flow",
        )
    apply_result = await asyncio.to_thread(
        BudgetCompensationService.apply, pending.proposal
    )
    await ConversationService.clear_state(sender_phone)
    return DispatchResult(
        reply_text=_compensation_apply_reply(apply_result),
        service_invoked="compensation",
    )


async def _confirm_pending_limit_action(
    sender_phone: str,
    action: str,
) -> DispatchResult:
    pending = await ConversationService.get_pending_limit(sender_phone)
    if pending is None:
        return DispatchResult(
            reply_text="Se perdió el contexto. Volvé a crear el límite.",
            service_invoked="conversation_flow",
        )
    reply_text = await _handle_create_limit(
        sender_phone,
        _limit_base_data(pending),
        last_limit=_last_limit_from_pending(pending),
        edit=pending.is_edit,
        allow_category_creation=(action == "confirm_limit_category"),
    )
    return DispatchResult(reply_text=reply_text, service_invoked="limit")
