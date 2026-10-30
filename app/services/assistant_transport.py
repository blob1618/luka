"""Assemble protected backend results, outgoing media and published navigation."""
from dataclasses import replace
from app.api.whatsapp import WhatsAppCTAURL, WhatsAppText, WhatsAppReplyButton, WhatsAppReplyButtons
from app.services.dispatch_contract import DispatchResult
from app.services.conversation_flow_runtime import ConversationFlowRuntime
from app.services.assistant_capabilities.movement_effects import apply_movement_feedback

_DASHBOARD_EVENTS = {'dashboard.link.sent', 'movements.query_result'}


async def assemble_reply(sender_phone, answer):
    """Transport/editor adapter shared by text, native buttons and bound actions."""
    configured = None
    if answer.event_key:
        configured = await ConversationFlowRuntime.render_event(sender_phone=sender_phone,
            event_key=answer.event_key, variables=answer.event_variables, backend_binding=answer.action_binding)

    outbound = list(answer.outbound)
    primary = None
    if answer.event_key in _DASHBOARD_EVENTS:
        url = answer.event_variables.get('login_url') or answer.event_variables.get('dashboard_url')
        candidates = ([configured] if configured is not None else []) + outbound
        link = next((item for item in candidates
                     if isinstance(item, WhatsAppCTAURL) and item.url == url), None)
        if link is not None:
            # The tool and the configured flow must not both emit the same link.
            outbound = [item for item in outbound
                        if not (isinstance(item, WhatsAppCTAURL) and item.url == url)]
            if isinstance(configured, WhatsAppCTAURL) and configured.url == url:
                configured = None
            if answer.event_key == 'dashboard.link.sent':
                # The link message is the whole answer: it becomes the primary message.
                primary = link
            else:
                outbound.append(link)

    result = DispatchResult(primary.body if primary is not None else answer.text,
                            service_invoked='assistant',
                            protected_presentation=True,
                            assistant_preview_delivery=answer.preview_delivery)
    result.tool_trace = answer.trace
    result.clear_memory = answer.clear_memory
    result.event_key, result.event_variables = answer.event_key, answer.event_variables
    if primary is not None:
        result.reply_message = primary
    elif outbound:
        result.reply_message = WhatsAppText(answer.text)
        result.followup_messages.extend(outbound)
    if answer.confirmation_action:
        buttons = (
            WhatsAppReplyButton(id=answer.confirmation_action, title='Confirmar'),
            WhatsAppReplyButton(id=answer.confirmation_action.replace(':confirm:', ':cancel:'), title='Cancelar'),
        )
        if len(answer.text) <= 1024:
            result.reply_message = WhatsAppReplyButtons(body=answer.text, buttons=buttons)
        else:
            result.reply_message = WhatsAppText(answer.text)
            result.followup_messages.append(WhatsAppReplyButtons(
                body='Confirmá solo si revisaste la propuesta completa del mensaje anterior.', buttons=buttons))
    # Domain feedback is adapted after commit; a new operation may have no movement.
    if answer.committed and not answer.replayed:
        await apply_movement_feedback(sender_phone, result, answer.committed)
        if isinstance(result.reply_message, WhatsAppText):
            result.reply_message = WhatsAppText(result.reply_text)
        elif isinstance(result.reply_message, WhatsAppReplyButtons) and result.reply_message.body != result.reply_text:
            if len(result.reply_text) <= 1024:
                result.reply_message = replace(result.reply_message, body=result.reply_text)
            else:
                result.followup_messages.insert(0, replace(result.reply_message,
                    body='Revisá la respuesta completa del mensaje anterior antes de elegir.'))
                result.reply_message = WhatsAppText(result.reply_text)
    if configured is not None:
        # Configured navigation supplements, and cannot replace, protected facts
        # or the complete operation preview whose delivery authorizes confirmation.
        result.followup_messages.append(configured)
    return result
