"""Trusted user confirmation events; never exposed as model tools."""
import unicodedata

from app.services.assistant_contracts import ConfirmationRequest
from app.services.assistant_operations import short_token

CONFIRM_WORDS = {'si', 'confirmo', 'confirmar'}
CANCEL_WORDS = {'cancelar', 'cancela'}
MODIFY_WORDS = {'modificar'}
# Conversational acknowledgements only act when a delivered proposal is pending.
SOFT_CONFIRM_WORDS = {'dale', 'ok'}


def _normalized(text):
    """Casefold and strip accents so 'Confirmá'/'confirmar' share one path."""
    decomposed = unicodedata.normalize('NFKD', text)
    plain = ''.join(char for char in decomposed if not unicodedata.combining(char))
    return plain.strip().casefold().strip(' .!¡?¿')


async def handle_user_action(context, action):
    from app.services.assistant_runtime import AssistantReply, committed_text, context_reply
    answer = _normalized(context.turn.source_text)
    if not action and answer not in CONFIRM_WORDS | CANCEL_WORDS | MODIFY_WORDS | SOFT_CONFIRM_WORDS:
        return None
    try:
        workspace = context.operations.workspace(context)
        if not action and answer in SOFT_CONFIRM_WORDS and workspace.last_confirmation is None:
            # "ok"/"dale" in ordinary conversation belong to the model, not to a confirmation.
            return None
        if not action and answer in MODIFY_WORDS:
            if workspace.active_operation_id is None:
                raise ValueError('No hay una propuesta activa para modificar')
            await context.operations.interrupt(context)
            return AssistantReply('Guardé el borrador suspendido. Indicá qué querés modificar; revisaremos una nueva propuesta.')
        request = None
        if action:
            parts = action.split(':')
            if len(parts) not in {4, 5} or parts[0] != 'assistant' or parts[1] not in {'confirm', 'cancel'}:
                raise ValueError('Esa opción no es válida')
            revision = int(parts[3]) if len(parts) == 5 else 1
            token = parts[4] if len(parts) == 5 else parts[3]
            request = ConfirmationRequest(operation_id=parts[2], revision=revision, short_token=token,
                confirmation_message_id=context.turn.inbound_message_id)
            if parts[1] == 'cancel':
                operation = workspace.operations.get(request.operation_id)
                if operation is None or short_token(operation.revision, operation.prepared_hash or '') != token:
                    raise ValueError('La propuesta cambió o ya no está vigente')
                view = await context.operations.view(context, operation)
                await context.operations.cancel(context, view.ref, expected_revision=request.revision)
                return context_reply(context, 'Cancelé la propuesta. No hice cambios.' + ('\n' + context.terminal if context.terminal else ''))
        elif answer in CANCEL_WORDS:
            operation = workspace.operations.get(workspace.active_operation_id)
            if operation is None:
                labels = context.operations.open_draft_labels(workspace)
                if labels:
                    listed = ', '.join(f'«{label}»' for label in labels)
                    raise ValueError(f'No hay una propuesta activa. Borradores abiertos: {listed}. '
                                     'Decime cuál querés cancelar')
                raise ValueError('No hay ninguna propuesta activa para cancelar')
            view = await context.operations.view(context, operation)
            await context.operations.cancel(context, view.ref, expected_revision=operation.revision)
            return context_reply(context, 'Cancelé la propuesta. No hice cambios.' + ('\n' + context.terminal if context.terminal else ''))
        else:
            pointer = workspace.last_confirmation
            if pointer is None:
                raise ValueError('No hay una propuesta entregada y vigente para confirmar; retomá y revisá la operación')
            request = ConfirmationRequest(operation_id=pointer.operation_id, revision=pointer.revision,
                short_token=short_token(pointer.revision, pointer.prepared_hash),
                confirmation_message_id=context.turn.inbound_message_id)
        result = await context.operations.confirm(context, request)
        context.trace.append({'source': 'user_action', 'action': 'confirm',
                              'outcome': 'replayed' if result.replayed else 'committed'})
        context.presenter.include(result.facts)
        text = context.presenter.render() or committed_text(result.data)
        if context.preview:
            text += '\n' + (context.terminal or context.preview_text or '')
        return context_reply(context, text, replayed=result.replayed, committed=result.data,
            event_key=result.event_key, event_variables=result.event_variables)
    except (ValueError, TypeError) as exc:
        return AssistantReply(str(exc) + '. No hice cambios.')


async def process_interaction(phone, message_id, option_id, reply_type):
    """Resolve published navigation and user callbacks without classification."""
    import asyncio
    from app.services import assistant_state
    from app.services.assistant_runtime import _owner, make_context, AssistantReply
    from app.services.conversation_flow_runtime import ConversationFlowRuntime
    from app.services.dispatch_contract import DispatchResult
    from app.services.assistant_transport import assemble_reply
    from app.services.assistant_capabilities.reminder_management import apply_candidate
    owner = await asyncio.to_thread(_owner, phone)
    if owner is None or not message_id:
        return DispatchResult('No pude identificar la cuenta o el mensaje.', protected_presentation=True)
    try:
        async with assistant_state.claim(owner):
            state = await assistant_state.read(owner)
            context = make_context(owner, phone, message_id, '', None, state)
            if option_id.startswith('movement:change_category:'):
                from app.services.assistant_capabilities.movement_management import UpdateDraft, MovementChanges
                from app.services.assistant_runtime import context_reply
                ref = option_id.removeprefix('movement:change_category:')
                await context.operations.open(context, operation_key='update_movement', draft=UpdateDraft(
                    movement_ref=ref, changes=MovementChanges(), require_category=True))
                answer = context_reply(context, context.terminal)
            elif option_id.startswith('rec_cand:'):
                parts = option_id.split(':')
                if len(parts) != 3 or parts[1] not in {'accept', 'reject'}:
                    raise ValueError('Opción inválida')
                decision = await apply_candidate(owner, parts[2], parts[1] == 'accept')
                answer = AssistantReply(decision.presentation,
                    committed={'candidate_status': decision.status} if decision.successful else None,
                    replayed=decision.replayed)
            else:
                async def action_handler(action, variables, binding=None):
                    return await _bound_action(context, action, variables, binding)
                answer = await ConversationFlowRuntime.handle_reply(sender_phone=phone, option_id=option_id,
                    reply_type=reply_type, action_handler=action_handler)
            try:
                await assistant_state.save(owner, state)
            except Exception as exc:
                from app.services.conversation import ConversationStateUnavailable
                if not isinstance(exc, ConversationStateUnavailable) or not isinstance(answer, AssistantReply) or answer.committed is None:
                    raise
        if isinstance(answer, AssistantReply):
            return await assemble_reply(phone, answer)
        return DispatchResult(answer.reply_text, reply_message=answer.reply_message, protected_presentation=True)
    except Exception as exc:
        from app.services.conversation import ConversationStateUnavailable
        if not isinstance(exc, (ValueError, TypeError, ConversationStateUnavailable)):
            raise
        return DispatchResult(str(exc) + '. No hice cambios.', protected_presentation=True)


async def _bound_action(context, action, variables, binding):
    """Bindings are backend flow state, never editable template variables."""
    from app.services.assistant_runtime import context_reply
    from app.services.assistant_capabilities.budgets import LimitDraft
    from app.services.assistant_capabilities.movement_management import UpdateDraft, MovementChanges
    if binding is None:
        raise ValueError('Esa opción no tiene una vinculación vigente')
    if action == 'start_limit':
        await context.operations.open(context, operation_key='create_limit', draft=LimitDraft())
        context.event_key, context.event_variables = 'limit.started', {}
        return context_reply(context, context.terminal)
    if action == 'request_category_change':
        target_ref = binding.get('movement_ref')
        if target_ref is None:
            raise ValueError('La opción ya no tiene un movimiento propio vigente')
        await context.operations.open(context, operation_key='update_movement', draft=UpdateDraft(
            movement_ref=target_ref, changes=MovementChanges(), require_category=True))
        return context_reply(context, context.terminal)
    identifier, revision = binding.get('operation_id'), binding.get('revision')
    workspace = context.operations.workspace(context)
    operation = workspace.operations.get(identifier)
    if operation is None or operation.revision != revision or operation.status not in {'ready', 'collecting', 'suspended'}:
        raise ValueError('La operación de esa opción cambió o venció')
    view = await context.operations.view(context, operation)
    if action in {'reject_category', 'reject_limit', 'reject_compensation', 'cancel_pending_operation'}:
        await context.operations.cancel(context, view.ref, expected_revision=revision)
        return context_reply(context, 'Cancelé la operación vinculada. No hice cambios.' + ('\n' + context.terminal if context.terminal else ''))
    if action == 'confirm_limit_year':
        if operation.operation_key not in {'create_limit', 'change_limit'}:
            raise ValueError('La opción no corresponde a un límite')
        month = operation.draft_payload.get('month')
        year = operation.reference_day.year + (1 if month and month < operation.reference_day.month else 0)
        await context.operations.amend(context, view.ref, expected_revision=revision, changes={'year': year})
        return context_reply(context, context.terminal)
    if action in {'confirm_category', 'confirm_compensation'}:
        expected_key = 'create_category' if action == 'confirm_category' else 'propose_compensation'
        if operation.operation_key != expected_key or operation.prepared_hash != binding.get('prepared_hash'):
            raise ValueError('La opción no corresponde a esta propuesta')
        request = ConfirmationRequest(operation_id=identifier, revision=revision,
            short_token=short_token(revision, operation.prepared_hash), confirmation_message_id=context.turn.inbound_message_id)
        result = await context.operations.confirm(context, request)
        text = result.data.get('presentation', 'Operación completada.')
        if context.preview:
            text += '\n' + context.terminal
        return context_reply(context, text, committed=result.data, replayed=result.replayed,
            event_key=result.event_key, event_variables=result.event_variables)
    raise ValueError('Acción no disponible para esta operación')
