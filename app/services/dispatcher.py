"""Unified ingress: onboarding/activity, tools and structured user callbacks."""
import asyncio
import logging
import re
import unicodedata

from app.api.whatsapp import WhatsAppCTAURL
from app.services.dispatch_contract import DispatchResult
from app.services.onboarding import OnboardingDecision, OnboardingService, record_activity
from app.services.conversation_flow_runtime import ConversationFlowRuntime
from app.services.assistant_transport import assemble_reply
from app.services.assistant_runtime import run_for_phone
from app.services.assistant_actions import process_interaction
from app.services.google_calendar import CalendarConfigurationError, authorization_url_for_phone
from app.services.telemetry import track_phase

logger = logging.getLogger(__name__)


async def _handle_calendar_connect(sender_phone: str) -> DispatchResult:
    try:
        url = await asyncio.to_thread(authorization_url_for_phone, sender_phone)
    except CalendarConfigurationError:
        logger.warning("[CALENDAR_AUTH] integration_not_configured")
        url = None
    except Exception as exc:
        logger.warning("[CALENDAR_AUTH] link_error=%s", type(exc).__name__)
        url = None
    if not url:
        return DispatchResult(
            reply_text="No pude preparar la conexión con Google Calendar. Intentá más tarde.",
            service_invoked="google_calendar",
            intent="calendar_connect",
        )
    message = WhatsAppCTAURL(
        body=(
            "📅 Tocá el botón para autorizar a Luka a gestionar eventos de pagos en "
            "tu calendario de Google. El enlace vence en 10 minutos y se usa una sola vez."
        ),
        display_text="Conectar calendario",
        url=url,
    )
    return DispatchResult(
        reply_text=message.body,
        reply_message=message,
        service_invoked="google_calendar",
        intent="calendar_connect",
    )


def _is_calendar_connect(text: str) -> bool:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return bool(
        re.search(r"\b(conectar|conecta|conectame|vincular|vincula|sincronizar|sincroniza)\b", normalized)
        and re.search(r"\b(calendario|calendar)\b", normalized)
    )


async def _enter(sender_phone):
    with track_phase('db'):
        result = await asyncio.to_thread(OnboardingService.prepare_whatsapp_message, sender_phone)
    if result.decision == OnboardingDecision.KNOWN_USER:
        with track_phase('db'):
            await asyncio.to_thread(record_activity, sender_phone)
        return None
    if result.decision == OnboardingDecision.SEND_INVITATION:
        text = (f'Para usar Luka, completá tu registro: {result.registration_url}\n'
                f'El enlace vence en {result.invitation_ttl_minutes} minutos.')
        response = DispatchResult(text, service_invoked='onboarding', event_key='onboarding.invitation',
            event_variables={'registration_url': result.registration_url, 'ttl_minutes': result.invitation_ttl_minutes})
    elif result.decision == OnboardingDecision.SUPPRESS_RESPONSE:
        response = DispatchResult('', service_invoked='onboarding')
    else:
        response = DispatchResult('No pude verificar tu cuenta. Intentá nuevamente en unos minutos.',
            service_invoked='onboarding', event_key='onboarding.error')
    if response.event_key:
        response.reply_message = await ConversationFlowRuntime.render_event(sender_phone=sender_phone,
            event_key=response.event_key, variables=response.event_variables)
    return response


async def process_incoming_message(sender_phone: str, text_body: str,
    whatsapp_message_id: str | None = None,
    conversation_history: list[dict[str, str]] | None = None) -> DispatchResult:
    gate = await _enter(sender_phone)
    if gate is not None:
        return gate
    if _is_calendar_connect(text_body):
        return await _handle_calendar_connect(sender_phone)
    answer = await run_for_phone(sender_phone, whatsapp_message_id, text_body, history=conversation_history)
    return await assemble_reply(sender_phone, answer)


async def process_incoming_interactive_reply(*, sender_phone: str, option_id: str,
    reply_type: str, whatsapp_message_id: str | None = None) -> DispatchResult:
    gate = await _enter(sender_phone)
    if gate is not None:
        return gate
    if option_id.startswith('assistant:'):
        return await assemble_reply(sender_phone, await run_for_phone(sender_phone, whatsapp_message_id, action=option_id))
    return await process_interaction(sender_phone, whatsapp_message_id, option_id, reply_type)
