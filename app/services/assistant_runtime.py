"""Bounded native tool loop. Capabilities and policies come only from the registry."""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import secrets
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.models.database import Usuario
from app.services import assistant_state, finance
from app.services.assistant_catalog import build_tool_registry
from app.services.assistant_contracts import PreviewDelivery, TrustedTurn
from app.services.assistant_context import ReferenceStore, ToolContext
from app.services.assistant_operations import OperationService, short_token
from app.services.assistant_registry import ToolExecutor
from app.services.conversation import ConversationHistoryService, ConversationStateUnavailable
from app.services.llm_providers import create_provider
from app.services.llm import LLMService
from app.services.telemetry import track_phase
from app.services.llm_providers.tools import GenerationPolicy, ToolDefinition, ToolMessage

TZ = ZoneInfo('America/Argentina/Buenos_Aires')
logger = logging.getLogger(__name__)
@dataclass
class AssistantReply:
    text: str
    confirmation_action: str | None = None
    replayed: bool = False
    committed: dict | None = None
    preview_delivery: PreviewDelivery | None = None
    outbound: list = field(default_factory=list)
    event_key: str | None = None
    event_variables: dict = field(default_factory=dict)
    clear_memory: bool = False
    action_binding: dict | None = None
    trace: list[dict] = field(default_factory=list)


def committed_text(result):
    """Domain handlers/receipts provide the backend presentation."""
    return result.get('presentation') or 'La operación se completó.'


def context_reply(context, text, *, committed=None, replayed=False, event_key=None, event_variables=None):
    action_id = None
    if context.preview:
        workspace = context.operations.workspace(context)
        operation = workspace.operations[workspace.active_operation_id]
        action_id = f'assistant:confirm:{operation.operation_id}:{operation.revision}:{short_token(operation.revision, operation.prepared_hash)}'
    binding = context.action_binding
    if binding is None:
        workspace = context.operations.workspace(context)
        operation = workspace.operations.get(workspace.active_operation_id)
        if operation:
            binding = {'operation_id': operation.operation_id, 'revision': operation.revision,
                       'prepared_hash': operation.prepared_hash}
        else:
            binding = {'interaction_id': secrets.token_hex(16)}
    return AssistantReply(text, confirmation_action=action_id, preview_delivery=context.preview,
        committed=committed, replayed=replayed, outbound=context.outbound,
        event_key=context.event_key or event_key, event_variables=context.event_variables or event_variables or {},
        clear_memory=context.clear_memory, action_binding=binding, trace=context.trace)


def _owner(phone):
    with finance.SessionLocal() as session:
        user = session.query(Usuario.id).filter(Usuario.whatsapp_id == phone).first()
        return user.id if user else None


def make_context(owner_id, phone, message_id, text, now, state, *, writable=True, registry=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError('El reloj de entrada necesita zona horaria')
    turn = TrustedTurn(owner_id=owner_id, phone=phone, conversation_id='whatsapp',
        inbound_message_id=message_id, source_text=text, now=current,
        reference_day=current.astimezone(TZ).date(), locale='es-AR')
    registry = registry or build_tool_registry()
    return ToolContext(turn, state, ReferenceStore(state), OperationService(registry), writable=writable)


async def run_for_phone(phone, message_id, text='', *, action=None, provider=None, now=None, history=None):
    owner_id = await asyncio.to_thread(_owner, phone)
    if owner_id is None:
        return AssistantReply('No encuentro una cuenta propia vinculada.')
    if not message_id:
        return AssistantReply('No pude identificar el mensaje; no hice cambios.')
    registry = build_tool_registry()
    try:
        async with assistant_state.claim(owner_id):
            prior = await registry.receipt(owner_id, confirmation_message_id=message_id)
            if prior:
                return AssistantReply(committed_text(prior), replayed=True, committed=prior)
            state = await assistant_state.read(owner_id)
            context = make_context(owner_id, phone, message_id, text, now, state, registry=registry)
            answer = await _run(context, registry, action, provider, history)
            try:
                await assistant_state.save(owner_id, state)
            except ConversationStateUnavailable:
                if answer.committed:
                    return answer  # The durable receipt and movement already committed.
                return AssistantReply('No pude guardar el contexto de conversación. No hice cambios; volvé a enviar el pedido.')
            return answer
    except ConversationStateUnavailable:
        if action or text.strip().casefold() in {'sí', 'si', 'confirmo', 'cancelar', 'modificar'}:
            return AssistantReply('No pude recuperar una propuesta vigente; no hice cambios. Volvé a enviar el pedido.')
        state = assistant_state.empty(owner_id)
        context = make_context(owner_id, phone, message_id, text, now, state, writable=False, registry=registry)
        return await _run(context, registry, None, provider, history)


async def _run(context, registry, action, provider, history):
    from app.services.assistant_actions import handle_user_action
    direct = await handle_user_action(context, action)
    if direct is not None:
        return direct
    options = context.state.get('selection_options', {})
    selected = options.get(context.turn.source_text.strip())
    if selected is None:
        matches = [item for item in options.values() if item['label'] == context.turn.source_text.strip()]
        selected = matches[0] if len(matches) == 1 else None
    if selected:
        context.state['selected_ref'] = selected['ref']
    try:
        initial = await context.initial_context()
    except ValueError as exc:
        return AssistantReply(str(exc))
    if history is None:
        history = [message.to_dict() for message in await ConversationHistoryService.get_recent(
            await assistant_state.client(), context.turn.phone)] if context.writable else []
    system = LLMService._load_system_prompt()
    messages = [ToolMessage('system', system), ToolMessage('user', json.dumps({
        'request': context.turn.source_text, 'date': context.turn.reference_day.isoformat(),
        'context': initial, 'visible_history': history}, ensure_ascii=False))]
    definitions = [ToolDefinition(contract.name, contract.description, contract.input_model.model_json_schema())
                   for contract in registry.available(context.turn)]
    executor, continuation = ToolExecutor(registry), None
    deadline = asyncio.get_running_loop().time() + 60
    seen, reads, proposals = set(), 0, 0
    provider = provider or create_provider()
    for _ in range(4):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            with track_phase('llm'):
                turn = await asyncio.wait_for(provider.generate_with_tools(messages=messages, tools=definitions,
                    continuation=continuation, policy=GenerationPolicy(
                        f'{context.turn.owner_id}:{context.turn.inbound_message_id}', min(remaining, 30),
                        tuple(x.strip() for x in os.getenv('LUKA_TOOLS_MODELS', '').split(',') if x.strip()) or
                        ((provider._get_model(),) if hasattr(provider, '_get_model') else ()))), remaining)
        except Exception as exc:
            logger.warning('assistant_provider_failed error=%s detail=%s',
                           type(exc).__name__, str(exc)[:200])
            context.presenter.add(['No pude completar la selección de herramientas; no confirmé ninguna escritura.'])
            break
        if not turn.calls:
            break
        typed, invalid = [], {}
        if len({call.id for call in turn.calls}) != len(turn.calls) or any(call.id in seen for call in turn.calls):
            context.presenter.add(['El proveedor repitió identificadores de herramientas; no confirmé ninguna escritura.'])
            break
        for call in turn.calls:
            try:
                contract = registry.get(call.name)
                typed.append((call, contract, contract.input_model.model_validate(call.arguments)))
            except ValidationError as exc:
                invalid[call.id] = [{'field': '.'.join(map(str, item['loc'])), 'message': item['msg']} for item in exc.errors()]
            except ValueError as exc:
                invalid[call.id] = [{'field': 'name', 'message': str(exc)}]
        effects = [contract.policy.effect for _, contract, _ in typed]
        new_proposals = effects.count('prepare')
        batch_error = None
        if new_proposals + proposals > 1:
            context.terminal = 'Enviá cada escritura por separado. No hice cambios.'
            break
        if len(typed) > 1 and any(effect != 'read' for effect in effects):
            batch_error = 'prepare/control se ejecutan solos; separá las llamadas de este batch'
        if reads + effects.count('read') > 6:
            batch_error = 'Se agotó el presupuesto de lecturas'
        continuation = turn.continuation
        messages = [ToolMessage('system', system)]
        # All inputs and the whole batch policy are checked before any effects.
        if invalid or batch_error:
            for call in turn.calls:
                seen.add(call.id)
                result = {'kind': 'error', 'status': 'invalid_arguments' if call.id in invalid else 'batch_not_executed',
                    'errors': invalid.get(call.id, []), 'reason': batch_error,
                    'instruction': 'Correct the arguments and retry; no tools in this batch were executed.'}
                context.trace.append({'tool': call.name, 'outcome': result})
                messages.append(ToolMessage('tool', json.dumps(result), call.id, call.name))
            continue
        reads += effects.count('read')
        proposals += new_proposals
        for call, contract, args in typed:
            seen.add(call.id)
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError('Se agotó el tiempo del pedido')
                outcome = await executor.execute(context, name=call.name,
                    arguments=args.model_dump(mode='json'), timeout=remaining)
                result = outcome.model_dump(mode='json')
                context.trace.append({'tool': call.name, 'effect': contract.policy.effect, 'outcome': result})
                if contract.policy.ends_turn and context.terminal is None:
                    context.terminal = context.presenter.render()
            except (ValueError, TypeError, TimeoutError) as exc:
                result = {'kind': 'error', 'code': 'invalid_request', 'retryable': False, 'reason': str(exc)}
                context.trace.append({'tool': call.name, 'effect': contract.policy.effect, 'outcome': result})
                context.presenter.add(['No pude completar esa parte del pedido: ' + str(exc)])
            messages.append(ToolMessage('tool', json.dumps(result, ensure_ascii=False), call.id, call.name))
            if context.terminal is not None:
                break
        if context.terminal is not None:
            break
    reply = context.terminal or '\n'.join(filter(None, [context.presenter.render(), context.fallback_reply])) or 'Esa capacidad no está disponible o necesito que aclares el pedido. No hice cambios.'
    if len(reply) > 4000:
        context.preview = None
        reply = 'El resultado es demasiado extenso para un mensaje. Acotá la consulta o enviá la escritura por separado. No hice cambios.'
    return context_reply(context, reply)


async def record_preview_delivered(phone, delivery, *, now=None):
    """Transport callback after the complete preview was actually sent successfully."""
    owner_id = await asyncio.to_thread(_owner, phone)
    if owner_id is None:
        raise ValueError('La cuenta ya no está disponible')
    async with assistant_state.claim(owner_id):
        state = await assistant_state.read(owner_id)
        context = make_context(owner_id, phone, delivery.source_message_id, '', now, state)
        await context.operations.mark_preview_delivered(context, delivery.operation_ref,
            expected_revision=delivery.revision, prepared_hash=delivery.prepared_hash,
            preview_source_message_id=delivery.source_message_id)
        await assistant_state.save(owner_id, state)
