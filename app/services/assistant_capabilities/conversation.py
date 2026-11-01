"""Cross-domain presentation and operation controls."""
import asyncio
from uuid import UUID

from app.models.database import Billetera
from app.services import finance
from app.services.assistant_contracts import (
    AskClarification, DataResult, EntityReference, MovementSnapshot, OperationAmend,
    OperationControl, OperationResult, PresentResult, ToolData, amount_text,
)
from app.services.assistant_registry import ToolContract, ToolPolicy
from app.services.assistant_contracts import EmptyInput, FrozenModel
from typing import Literal


def _wallet_label(owner, wallet_id: str | None, fallback: str) -> str:
    if not wallet_id:
        return fallback
    try:
        parsed = UUID(wallet_id)
    except (ValueError, TypeError):
        return fallback
    with finance.SessionLocal() as session:
        row = session.query(Billetera).filter(
            Billetera.id == parsed, Billetera.usuario_id == owner).first()
        return f'{row.nombre} ({row.moneda})' if row else fallback


class ConversationReply(FrozenModel):
    kind: Literal['greeting', 'help', 'out_of_scope'] = 'help'


async def conversation_reply(context, args):
    replies = {'greeting': 'Hola, soy Luka. Puedo ayudarte a registrar y consultar tus finanzas, presupuestos y recordatorios.',
        'help': 'Podés registrar ingresos y gastos, consultar movimientos y gráficos, administrar categorías, billeteras, límites y recordatorios, revisar presupuestos o acceder a tu dashboard. Los cambios se muestran antes de confirmarlos.',
        'out_of_scope': 'Puedo ayudarte con tus finanzas personales. Contame qué querés registrar, consultar o administrar.'}
    facts = context.presenter.add([replies[args.kind]])
    context.terminal = context.presenter.render()
    return DataResult(data=ToolData(status='ok').model_dump(mode='json'), facts=facts)


async def reset_context(context, args):
    from app.services import assistant_state
    from app.services.conversation_flow_runtime import ConversationFlowRuntime
    context.state.clear()
    context.state.update(assistant_state.empty(context.turn.owner_id, context.turn.conversation_id))
    context.preview = None
    context.clear_memory = True
    await ConversationFlowRuntime.abandon(context.turn.phone)
    context.terminal = 'Reinicié el contexto de conversación. Tus registros y recibos se conservan.'
    return DataResult(data=ToolData(status='reset').model_dump(mode='json'))


async def present(context, args):
    context.terminal = context.presenter.render(args.block_refs)
    return DataResult(data=ToolData(status='presented').model_dump(mode='json'))


async def clarify(context, args):
    labels = {'date': 'una fecha con día, mes y año', 'amount': 'el importe', 'movement': 'qué movimiento querés modificar',
              'wallet': 'qué billetera propia querés usar', 'category': 'qué categoría propia querés usar',
              'request': 'qué operación necesitás', 'operation': 'qué operación querés retomar'}
    question = ('Esa capacidad no está disponible en Luka. ' if args.reason_code == 'unsupported_capability' else '')
    question += 'Necesito que aclares ' + labels[args.target_field] + '.'
    candidates = args.candidate_refs
    if not candidates and args.target_field in {'movement', 'wallet', 'category'}:
        candidates = tuple(context.state.get('last_shown', [])) if args.target_field == 'movement' else tuple(
            ref for ref, record in context.state['references'].items() if record['kind'] == args.target_field)
    options, seen = {}, set()
    for ref in candidates:
        model = MovementSnapshot if args.target_field == 'movement' else EntityReference
        value = await context.references.resolve(context.turn, ref, kind=args.target_field, payload_model=model)
        if value.id in seen:
            continue
        seen.add(value.id)
        if isinstance(value, MovementSnapshot):
            label = f'{value.date} — {value.description}, {value.currency} {amount_text(value.amount)}'
        elif args.target_field == 'wallet':
            label = await asyncio.to_thread(_wallet_label, context.turn.owner_id, value.id, value.label)
        else:
            label = value.label
        options[str(len(options) + 1)] = {'ref': ref, 'label': label}
    if options:
        context.state['selection_options'] = options
        question += '\n' + '\n'.join(f"{number}. {item['label']}" for number, item in options.items())
    context.terminal = '\n'.join(filter(None, [context.presenter.render(), question]))
    return DataResult(data=ToolData(status='needs_clarification').model_dump(mode='json'))


async def amend(context, args):
    return OperationResult(operation=await context.operations.amend(context, args.ref,
        expected_revision=args.expected_revision, changes=args.changes))


async def resume(context, args):
    view = await context.operations.resume(context, args.ref, expected_revision=args.expected_revision)
    if view.status == 'collecting':
        context.fallback_reply, context.terminal = context.terminal, None
    return OperationResult(operation=view)


async def suspend(context, args):
    view = await context.operations.suspend(context, args.ref, expected_revision=args.expected_revision)
    context.terminal = 'Guardé la operación suspendida. Podés retomarla con su referencia.'
    return OperationResult(operation=view)


async def cancel(context, args):
    view = await context.operations.cancel(context, args.ref, expected_revision=args.expected_revision)
    context.terminal = 'Cancelé la propuesta. No hice cambios.' + ('\n' + context.terminal if context.terminal else '')
    return OperationResult(operation=view)


def conversation_contracts():
    terminal = ToolPolicy('control', facts='none', interrupt_active_operation=False, ends_turn=True)
    return (
        ToolContract('conversation_reply', 'Responde saludo, ayuda o pedido fuera de alcance con información verificada de Luka.', ConversationReply, ToolData, conversation_reply, terminal),
        ToolContract('reset_context', 'Reinicia borradores y contexto efímero propios. El historial visible se limpia después de enviar la respuesta; no borra registros ni recibos.', EmptyInput, ToolData, reset_context, terminal),
        ToolContract('present_result', 'Finaliza con hechos de backend. Los obligatorios se incluyen automáticamente; '
                     'block_refs opcional sólo selecciona u ordena hechos conocidos. Nunca redactes valores libres.', PresentResult, ToolData, present, terminal),
        ToolContract('ask_clarification', 'Pide aclarar un campo o seleccionar una referencia propia. Para recopilar '
                     'campos de una operación, abrí primero el borrador incompleto con su capacidad.', AskClarification, ToolData, clarify, terminal),
        ToolContract('operation_amend', 'Modifica campos de un borrador usando su ref y revisión actuales. '
                     'También retoma y completa un borrador suspendido en un paso: si el pedido agrega datos, pasarlos en changes. '
                     'changes se valida con el schema de la operación; fecha nueva usa DateSpec del turno actual.', OperationAmend, None, amend, terminal),
        ToolContract('operation_resume', 'Retoma una operación suspendida por ref/revisión y muestra una nueva propuesta. '
                     'Si queda collecting y el mismo mensaje aporta datos, continuá con operation_amend usando la revisión devuelta antes de terminar. '
                     'Si hay varias y el pedido no distingue cuál, pedí aclaración.', OperationControl, None, resume,
                     ToolPolicy('control', facts='none', interrupt_active_operation=False, ends_turn=False)),
        ToolContract('operation_suspend', 'Suspende la operación activa conservando borrador y vencimiento.', OperationControl, None, suspend, terminal),
        ToolContract('operation_cancel', 'Cancela una operación por ref/revisión sin aplicar cambios.', OperationControl, None, cancel, terminal),
    )
