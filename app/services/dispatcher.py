"""Unified ingress: onboarding/activity and the native assistant turn."""
import asyncio

from app.services.dispatch_contract import DispatchResult
from app.services.onboarding import OnboardingDecision, OnboardingService, record_activity
from app.services.conversation_flow_runtime import ConversationFlowRuntime
from app.services.assistant_transport import assemble_reply
from app.services.assistant_runtime import run_for_phone
from app.services.assistant_actions import process_interaction
from app.services.telemetry import track_phase


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
