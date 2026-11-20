"""Delivered, revision-bound field questions in the existing operation workspace."""
import asyncio

from pydantic import Field, model_validator

from app.services.assistant_contracts import (
    EntityReference, FieldRequest, FrozenModel, OperationReference, OperationResult,
    PendingQuestion, QuestionDelivery, QuestionOption, ReferenceToken,
)
from app.services.assistant_registry import ToolContract, ToolPolicy


def folded(value):
    return ' '.join(value.casefold().split())


async def category_question(context, draft):
    from app.services.assistant_context import _entities
    categories, _ = await asyncio.to_thread(_entities, context.turn)
    refs = [await context.reference('category', EntityReference(id=identifier, label=name))
            for identifier, name in categories]
    return FieldRequest(field='category_ref', reason='missing', candidate_refs=tuple(refs),
        question=f'Para completar «{draft.description}», elegí una categoría o indicá el nombre de una nueva.')


def invalidate_question(context, workspace):
    raw = context.state.get('pending_question')
    if not raw:
        return
    delivery = PendingQuestion.model_validate(raw).delivery
    record = context.state['references'].get(delivery.operation_ref, {})
    identifier = record.get('payload', {}).get('operation_id')
    operation = workspace.operations.get(identifier)
    if (operation is None or workspace.active_operation_id != identifier or
            operation.status != 'collecting' or operation.revision != delivery.revision or
            operation.expires_at <= context.turn.now):
        context.state.pop('pending_question', None)
        context.question_delivery = None


async def stage_question(context, operation, view):
    field = operation.missing_fields[0]
    options = []
    if field.field == 'category_ref':
        for ref in field.candidate_refs:
            entity = await context.references.resolve(context.turn, ref, kind='category', payload_model=EntityReference)
            options.append(QuestionOption(ref=ref, label=entity.label))
        if options:
            context.terminal += '\n' + '\n'.join(f'{i}. {option.label}' for i, option in enumerate(options, 1))
    elif field.field.startswith('items.') and field.field.endswith('.product_ref'):
        from app.services.assistant_capabilities.shopping_basket import BasketProductChoice
        for ref in field.candidate_refs:
            product = await context.references.resolve(context.turn, ref, kind='basket_product',
                payload_model=BasketProductChoice)
            options.append(QuestionOption(ref=ref, label=product.visible))
        if options:
            context.terminal += '\n' + '\n'.join(f'{i}. {option.label}' for i, option in enumerate(options, 1))
    delivery = QuestionDelivery(operation_ref=view.ref, revision=operation.revision,
        field=field.field, options=tuple(options), source_message_id=context.turn.inbound_message_id,
        expires_at=operation.expires_at, text=context.terminal)
    context.state['pending_question'] = PendingQuestion(delivery=delivery).model_dump(mode='json')
    context.question_delivery = delivery


async def current_question(context):
    await context.operations.expire(context)
    raw = context.state.get('pending_question')
    if raw is None:
        return None
    pending = PendingQuestion.model_validate(raw)
    if not pending.delivered or pending.delivery.expires_at <= context.turn.now:
        return None
    delivery = pending.delivery
    operation = await context.operations.resolve(context, delivery.operation_ref, delivery.revision)
    if operation.operation_id != context.operations.workspace(context).active_operation_id:
        return None
    return delivery


async def record_question_delivered(phone, delivery, *, now=None):
    from app.services import assistant_state
    from app.services.assistant_runtime import _owner, make_context
    owner = await asyncio.to_thread(_owner, phone)
    if owner is None:
        raise ValueError('La cuenta ya no está disponible')
    async with assistant_state.claim(owner):
        state = await assistant_state.read(owner)
        context = make_context(owner, phone, delivery.source_message_id, '', now, state)
        await context.operations.expire(context)
        raw = state.get('pending_question')
        if raw is None or PendingQuestion.model_validate(raw).delivery != delivery:
            raise ValueError('La pregunta cambió o venció')
        state['pending_question'] = PendingQuestion(delivery=delivery, delivered=True).model_dump(mode='json')
        await assistant_state.save(owner, state)


class CategoryAnswer(FrozenModel):
    operation_ref: ReferenceToken
    expected_revision: int = Field(ge=1)
    category_ref: ReferenceToken | None = None
    new_name: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode='after')
    def one_choice(self):
        if (self.category_ref is None) == (self.new_name is None):
            raise ValueError('Elegí una categoría existente o un nombre nuevo')
        if self.new_name is not None and not self.new_name.strip():
            raise ValueError('Indicá un nombre de categoría')
        return self


async def answer_category(context, args):
    if not context.writable:
        raise ValueError('No hay contexto fiable para completar el borrador')
    question = await current_question(context)
    if (question is None or question.field != 'category_ref' or
            question.operation_ref != args.operation_ref or question.revision != args.expected_revision or
            question.source_message_id == context.turn.inbound_message_id):
        raise ValueError('La pregunta de categoría cambió o no fue entregada; retomá el comprobante')
    if args.category_ref:
        if args.category_ref not in {option.ref for option in question.options}:
            raise ValueError('Esa categoría no pertenece a las opciones entregadas')
        from app.services.assistant_capabilities.entities import category_reference
        await category_reference(context, args.category_ref)
        view = await context.operations.amend(context, question.operation_ref,
            expected_revision=question.revision, changes={'category_ref': args.category_ref})
    else:
        from app.services.assistant_capabilities.entities import CategoryDraft
        view = await context.operations.open(context, operation_key='create_category', draft=CategoryDraft(
            name=args.new_name.strip(), parent_ref=question.operation_ref,
            parent_revision=question.revision, parent_field=question.field))
    return OperationResult(operation=view)


async def select_answer(context):
    if context.turn.source_kind != 'text':
        return None
    question = await current_question(context)
    if question is None or (question.field != 'category_ref' and
            not (question.field.startswith('items.') and question.field.endswith('.product_ref'))):
        return None
    text = folded(context.turn.source_text)
    matches = [option for i, option in enumerate(question.options, 1)
               if text in {str(i), folded(option.label)}]
    if len(matches) != 1:
        return None
    try:
        if question.field.startswith('items.'):
            return await answer_basket_product(context, BasketProductAnswer(
                operation_ref=question.operation_ref, expected_revision=question.revision,
                product_ref=matches[0].ref))
        return await answer_category(context, CategoryAnswer(operation_ref=question.operation_ref,
            expected_revision=question.revision, category_ref=matches[0].ref))
    except ValueError:
        # Stale domain options must be presented again, never silently reassigned.
        context.terminal = 'Esa categoría cambió. Retomá el comprobante para revisar las opciones vigentes.'
        return False


class BasketProductAnswer(FrozenModel):
    operation_ref: ReferenceToken
    expected_revision: int = Field(ge=1)
    product_ref: ReferenceToken


async def answer_basket_product(context, args):
    if not context.writable:
        raise ValueError('No hay contexto fiable para completar el borrador')
    question = await current_question(context)
    if (question is None or not question.field.startswith('items.') or
            not question.field.endswith('.product_ref') or
            question.operation_ref != args.operation_ref or question.revision != args.expected_revision or
            question.source_message_id == context.turn.inbound_message_id or
            args.product_ref not in {option.ref for option in question.options}):
        raise ValueError('La selección de producto cambió o no fue entregada; retomá la canasta')
    operation = await context.operations.resolve(context, question.operation_ref, question.revision)
    index = int(question.field.split('.')[1])
    items = list(operation.draft_payload['items'])
    items[index] = {**items[index], 'product_ref': args.product_ref}
    view = await context.operations.amend(context, question.operation_ref,
        expected_revision=question.revision, changes={'items': items})
    return OperationResult(operation=view)


class StatusQuery(FrozenModel):
    operation_ref: ReferenceToken | None = None


async def operation_status(context, args):
    from app.services.assistant_capabilities.common import data
    await context.operations.expire(context)
    workspace = context.operations.workspace(context)
    if args.operation_ref:
        record = await context.references.resolve(context.turn, args.operation_ref,
            kind='operation', payload_model=OperationReference)
        operations = [workspace.operations.get(record.operation_id)]
        operations = [op for op in operations if op is not None]
    else:
        operations = list(workspace.operations.values())
    rows, lines = [], []
    labels = {'collecting': 'pendiente: faltan datos', 'ready': 'pendiente de confirmación',
              'suspended': 'borrador suspendido', 'awaiting_auth': 'pendiente de vinculación',
              'committed': 'operación completada', 'cancelled': 'cancelado, sin registro', 'expired': 'vencido, sin registro'}
    for op in operations:
        receipt = await context.operations.registry.receipt(context.turn.owner_id, op_id=op.operation_id)
        status = 'committed' if receipt else op.status
        label = 'movimiento registrado' if receipt else labels[status]
        text = f'{context.operations._draft_label(op)}: {label}.'
        if op.missing_fields and status in {'collecting', 'suspended'}:
            text += ' ' + ' '.join(f.question or f'Falta {f.field}.' for f in op.missing_fields)
        rows.append({'status': status, 'summary': text})
        lines.append(text)
    result = data(context, rows, lines or ['No tengo un comprobante identificado en este contexto. Consultá tus movimientos para verificar registros anteriores.'])
    context.terminal = context.presenter.render()
    return result


def question_contracts():
    return (
        ToolContract('answer_basket_product_question',
            'Elige una opción de producto por número o referencia de la pregunta entregada para agregarla a la canasta. Nunca pide EAN ni confirma el guardado.',
            BasketProductAnswer, None, answer_basket_product,
            ToolPolicy('control', facts='none', interrupt_active_operation=False, ends_turn=True)),
        ToolContract('answer_category_question',
            'Contesta la pregunta de categoría entregada (pending_question) del comprobante. '
            'Usá category_ref para una opción existente o new_name para proponer una categoría hija; '
            'el backend vincula el padre. Nunca confirma categoría ni gasto.', CategoryAnswer, None,
            answer_category, ToolPolicy('control', facts='none', interrupt_active_operation=False, ends_turn=True)),
        ToolContract('operation_status',
            'Consulta si los comprobantes/borradores fueron registrados según estado y recibos reales. '
            'No confirma ni reanuda. Sin ref muestra los estados identificados para que el usuario distinga sus comprobantes.',
            StatusQuery, None, operation_status,
            ToolPolicy('read', interrupt_active_operation=False, ends_turn=True)),
    )
