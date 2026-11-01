"""Limits, budget reads and snapshot-protected compensation."""
import asyncio
import logging
from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator

from app.api.whatsapp import WhatsAppReplyButton, WhatsAppReplyButtons
from app.models.database import Billetera, Categoria, LimiteCategoria
from app.services import finance
from app.services.assistant_contracts import (
    FieldRequest, FrozenModel, IncompleteOperation, ReferenceToken,
    ResourceReference, amount_text,
)
from app.services.assistant_capabilities.common import (
    check_resource, committed, data, issue_resource, missing, operation_tool,
    positive, prepared, read_tool, refresh_resource, require_status, snapshot_row,
)
from app.services.assistant_capabilities.entities import category_reference
from app.services.budget import BudgetService
from app.services.currencies import is_supported, supported_text
from app.services.compensation import BudgetCompensationService, CompensationProposal
from app.services.limit import LimitService

LIMIT_FIELDS = ('categoria_id', 'cantidad_max', 'moneda', 'inicio_periodo', 'fin_periodo')
logger = logging.getLogger(__name__)


class BudgetQuery(FrozenModel):
    reference_day: date | None = None
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    category_ref: ReferenceToken | None = None


class LimitDraft(FrozenModel):
    category_ref: ReferenceToken | None = Field(default=None, title='la categoría que querés usar')
    limit_ref: ReferenceToken | None = Field(default=None, title='qué límite de los mostrados querés usar')
    amount: str | None = Field(default=None, title='el importe')
    month: int | None = Field(title='el mes', default=None, ge=1, le=12)
    year: int | None = Field(title='el año', default=None, ge=1, le=9999)
    currency: str | None = Field(title='la moneda', default=None, pattern=r'^[A-Z]{3}$')
    _positive = field_validator('amount')(positive)


class LimitPrepared(FrozenModel):
    category_id: str
    category: str
    amount: str
    month: int
    year: int
    currency: str
    expected: dict | None = None
    source: ResourceReference | None = None


class DeleteLimits(FrozenModel):
    limit_refs: tuple[ReferenceToken, ...] = Field(default=(), max_length=20)


class DeleteLimitsPrepared(FrozenModel):
    resources: tuple[ResourceReference, ...]


class CompensationDraft(BudgetQuery):
    target_category_ref: ReferenceToken | None = Field(default=None, title='la categoría de destino')
    source_category_ref: ReferenceToken | None = Field(default=None, title='la categoría de origen')
    amount: str | None = Field(default=None, title='el importe')
    _positive = field_validator('amount')(positive)


class CompensationPrepared(FrozenModel):
    proposal: dict


def _limit_rows(owner, *, today=None):
    with finance.SessionLocal() as session:
        query = session.query(LimiteCategoria, Categoria.nombre).join(Categoria,
            Categoria.id == LimiteCategoria.categoria_id).filter(LimiteCategoria.usuario_id == owner,
                Categoria.esta_eliminado.is_(False))
        if today:
            query = query.filter(LimiteCategoria.fin_periodo >= today)
        return [(str(row.id), name, snapshot_row(row, LIMIT_FIELDS)) for row, name in query.order_by(
            LimiteCategoria.inicio_periodo, Categoria.nombre, LimiteCategoria.id).all()]


async def refresh_limit(context, identifier, category):
    return await refresh_resource(context, 'limit', LimiteCategoria, identifier, LIMIT_FIELDS,
        lambda snap: {'category': category, 'amount': snap['cantidad_max'], 'currency': snap['moneda'],
            'month': date.fromisoformat(snap['inicio_periodo']).month, 'year': date.fromisoformat(snap['inicio_periodo']).year})


async def list_limits(context, args):
    rows, lines = [], []
    category = await category_reference(context, args.category_ref) if args.category_ref else None
    reference_day = args.reference_day or context.turn.reference_day
    for identifier, name, snap in await asyncio.to_thread(_limit_rows, context.turn.owner_id, today=reference_day):
        if category and snap['categoria_id'] != category.id:
            continue
        if args.reference_day and date.fromisoformat(snap['inicio_periodo']) > reference_day:
            continue
        visible = {'category': name, 'amount': snap['cantidad_max'], 'currency': snap['moneda'],
            'month': date.fromisoformat(snap['inicio_periodo']).month, 'year': date.fromisoformat(snap['inicio_periodo']).year}
        if args.currency and args.currency != visible['currency']:
            continue
        ref = await issue_resource(context, 'limit', identifier, snap, visible)
        rows.append({'limit_ref': ref, **visible})
        lines.append(f"{name}, {visible['month']:02}/{visible['year']}: {visible['currency']} {amount_text(visible['amount'])}.")
    summary = '\n'.join(lines or ['No hay límites vigentes.'])
    context.event_key, context.event_variables = 'limit.listed', {'summary': summary}
    return data(context, rows, lines or ['No hay límites vigentes.'])


async def prepare_limit(context, operation):
    draft = LimitDraft.model_validate(operation.draft_payload)
    old = await check_resource(context, draft.limit_ref, 'limit', LimiteCategoria, LIMIT_FIELDS) if draft.limit_ref else None
    values = draft.model_dump()
    if old:
        visible = old.visible
        values.update({key: value for key, value in {'amount': visible['amount'], 'month': visible['month'],
            'year': visible['year'], 'currency': visible['currency']}.items() if values[key] is None})
        category_id = old.snapshot['categoria_id']
        category_name = visible['category']
    else:
        category_id = category_name = None
    if draft.category_ref:
        category = await category_reference(context, draft.category_ref)
        category_id, category_name = category.id, category.label
    updated = LimitDraft.model_validate(values)
    absent = missing(updated, ('amount', 'month', 'year', 'currency') + (() if category_id else ('category_ref',)))
    if absent:
        field = absent.missing_fields[0].field
        context.event_key = 'limit.year_confirmation' if field == 'year' else 'limit.missing_data'
        context.event_variables = {'year': str(context.turn.reference_day.year + (1 if updated.month and updated.month < context.turn.reference_day.month else 0))} if field == 'year' else {'missing_field': field}
        return absent
    if not is_supported(updated.currency):
        return IncompleteOperation(missing_fields=(FieldRequest(
            field='currency', reason='invalid',
            question=f'Moneda no soportada. Solo opero con {supported_text()}.',
        ),))

    def has_wallet():
        with finance.SessionLocal() as session:
            return session.query(Billetera.id).filter(
                Billetera.usuario_id == context.turn.owner_id,
                Billetera.moneda == updated.currency,
            ).first() is not None

    if not await asyncio.to_thread(has_wallet):
        return IncompleteOperation(missing_fields=(FieldRequest(
            field='currency', reason='missing',
            question=f'No tenés una billetera en {updated.currency}. Creá una antes de definir un límite en esa moneda.',
        ),))

    start = date(updated.year, updated.month, 1)
    existing = [dict(id=identifier, **snap) for identifier, _, snap in await asyncio.to_thread(_limit_rows, context.turn.owner_id)
        if snap['categoria_id'] == category_id and snap['inicio_periodo'] == str(start) and snap['moneda'] == updated.currency]
    expected = existing[0] if existing else None
    if operation.prepared_payload and operation.prepared_payload.get('expected') != expected:
        raise ValueError('stale_context: el límite cambió')
    return prepared(LimitPrepared(category_id=category_id, category=category_name, amount=updated.amount,
        month=updated.month, year=updated.year, currency=updated.currency, expected=expected, source=old),
        f"Límite {category_name}, {updated.month:02}/{updated.year}: {updated.currency} {amount_text(updated.amount)}.")


async def commit_limit(context, operation, confirmation):
    value = LimitPrepared.model_validate(operation.prepared_payload)
    # Re-run preparation against the sealed expected snapshot before invoking upsert.
    await prepare_limit(context, operation)
    from types import SimpleNamespace
    last_limit = SimpleNamespace(limit_id=value.source.id, category_name=value.source.visible['category'],
        amount=value.source.visible['amount'], month=value.source.visible['month'], year=value.source.visible['year'],
        currency=value.source.visible['currency']) if value.source else None
    result = require_status(await asyncio.to_thread(LimitService.create_limit, context.turn.phone,
        {'limit_category': value.category, 'limit_amount': value.amount, 'limit_month': value.month,
         'limit_year': value.year, 'limit_currency': value.currency}, today=context.turn.reference_day,
         last_limit=last_limit), {'created', 'updated'})
    variables = {'category': value.category, 'amount': amount_text(value.amount), 'currency': value.currency,
                 'period': f'{value.month:02}/{value.year}'}
    await refresh_limit(context, result.limit_id, value.category)
    label = {'created': 'creado', 'updated': 'actualizado'}.get(result.status, result.status)
    return committed(context, f"Límite {value.category}: {label}, {value.currency} {amount_text(value.amount)}.",
                     event='limit.' + result.status, variables=variables)


async def prepare_delete(context, operation):
    draft = DeleteLimits.model_validate(operation.draft_payload)
    if not draft.limit_refs:
        from app.services.assistant_contracts import FieldRequest, IncompleteOperation
        return IncompleteOperation(missing_fields=(FieldRequest(field='limit_refs', reason='missing', question='Elegí de los límites mostrados cuáles querés eliminar.'),))
    resources = tuple([await check_resource(context, ref, 'limit', LimiteCategoria, LIMIT_FIELDS) for ref in draft.limit_refs])
    if len({resource.id for resource in resources}) != len(resources):
        raise ValueError('Límites repetidos')
    return prepared(DeleteLimitsPrepared(resources=resources), 'Eliminar estos límites: ' + '; '.join(
        f"{r.visible['category']} {r.visible['month']}/{r.visible['year']} {r.visible['currency']} {amount_text(r.visible['amount'])}" for r in resources))


async def commit_delete(context, operation, confirmation):
    value = DeleteLimitsPrepared.model_validate(operation.prepared_payload)
    candidates = [{'limit_id': r.id, **r.visible} for r in value.resources]
    result = require_status(await asyncio.to_thread(LimitService.delete_limits_by_ids, context.turn.phone, candidates), {'deleted'})
    for resource in value.resources:
        context.references.retire('limit', resource.id)
    return committed(context, f'Eliminé {len(result.deleted)} límites.', event='limit.bulk_deleted',
        variables={'category': ', '.join(r.visible['category'] for r in value.resources),
                   'periods': ', '.join(f"{r.visible['month']}/{r.visible['year']}" for r in value.resources), 'count': str(len(result.deleted))})


async def query_budget(context, args):
    category = await category_reference(context, args.category_ref) if args.category_ref else None
    def query():
        with finance.SessionLocal() as session:
            return BudgetService.query_statuses(session, context.turn.owner_id, args.reference_day or context.turn.reference_day,
                currency=args.currency, category_id=UUID(category.id) if category else None)
    budgets = await asyncio.to_thread(query)
    lines = [f'{b.category_name}: límite {b.currency} {amount_text(b.limit_amount)}, registrado {amount_text(b.spent_amount)}, '
        f'disponible {amount_text(b.remaining_amount)}, excedido {amount_text(b.exceeded_amount)}, {amount_text(b.percentage)} %, estado {b.state}.' for b in budgets]
    context.event_key, context.event_variables = 'budget.result', {'summary': '\n'.join(lines or ['No hay presupuestos para ese período.'])}
    return data(context, [], lines or ['No hay presupuestos para ese período.'])


def compensation_text(proposal: CompensationProposal, *, confirmation: bool = False) -> str:
    target = proposal.target
    prefix = ('Ajustar presupuesto' if confirmation else
              f'{target.category_name} superó su límite en {proposal.currency} {amount_text(proposal.amount)}. Podés ajustar el presupuesto así')
    lines = [prefix + ':', f'{target.category_name}: límite {proposal.currency} {amount_text(target.before_limit)} → '
             f'{amount_text(target.after_limit)}; gastado {amount_text(target.spent_amount)}.']
    for donor in proposal.donors:
        contributed = donor.before_limit - donor.after_limit
        lines.append(f'{donor.category_name}: límite {amount_text(donor.before_limit)} → '
                     f'{amount_text(donor.after_limit)} (aporta {proposal.currency} {amount_text(contributed)}).')
    if not confirmation:
        lines.append('Si querés, tocá «Ajustar presupuesto» o decime «ajustá el presupuesto».')
    return '\n'.join(lines)


async def prepare_compensation(context, operation):
    args = CompensationDraft.model_validate(operation.draft_payload)
    target = await category_reference(context, args.target_category_ref) if args.target_category_ref else None
    source = await category_reference(context, args.source_category_ref) if args.source_category_ref else None
    if operation.prepared_payload:
        proposal = CompensationProposal.from_dict(operation.prepared_payload['proposal'])
        # Existing apply validates row amounts/period snapshots under its transaction.
        if proposal.user_id != str(context.turn.owner_id):
            raise ValueError('La propuesta pertenece a otra cuenta')
    else:
        result = await asyncio.to_thread(BudgetCompensationService.build_proposal,
            context.turn.owner_id, target_category=target.label if target else None,
            source_category=source.label if source else None, requested_amount=Decimal(args.amount) if args.amount else None,
            reference_date=args.reference_day or context.turn.reference_day, currency=args.currency)
        if result.status != 'ok':
            from app.services.assistant_contracts import FieldRequest, IncompleteOperation
            questions = {'no_funds': 'No hay categorías con presupuesto disponible para compensar. Podés revisar tus límites antes de volver a intentarlo.',
                'no_excess': 'No hay un presupuesto excedido en esa categoría, moneda y período. Revisá la selección si querías consultar otro.',
                'error': 'No pude calcular la propuesta de compensación. Volvé a intentarlo.'}
            return IncompleteOperation(missing_fields=(FieldRequest(field='dependencies', reason='invalid',
                question=questions.get(result.status, 'Revisá el importe, la moneda y las categorías para calcular la compensación.')),))
        proposal = result.proposal
    summary = compensation_text(proposal, confirmation=True)
    context.event_key, context.event_variables = 'budget.compensation_proposed', {'summary': summary}
    return prepared(CompensationPrepared(proposal=proposal.to_dict()), summary)


async def suggest_compensation(context, source_operation, result):
    """Preserve the post-expense offer as a new, separately confirmed proposal."""
    if result.get('replayed') or result.get('command_kind') not in {'register', 'register_batch', 'update'}:
        return
    workspace = context.operations.workspace(context)
    if any(op.operation_id != source_operation.operation_id and op.status not in {'committed', 'cancelled', 'expired'}
           for op in workspace.operations.values()):
        return
    rows = result.get('movements') or [result['movement']]
    try:
        evaluations = await asyncio.to_thread(BudgetService.evaluate_movements, [row['id'] for row in rows])
        budget = next((item.budget for item in evaluations if item.should_alert and item.budget), None)
        if budget is None:
            return
        available = await asyncio.to_thread(BudgetCompensationService.build_proposal, context.turn.owner_id,
            target_category=budget.category_name, currency=budget.currency, reference_date=context.turn.reference_day)
        if available.status != 'ok':
            return
        context.outbound.append(WhatsAppReplyButtons(
            body=compensation_text(available.proposal),
            buttons=(WhatsAppReplyButton(
                id=f'budget:compensate:{budget.currency}',
                title='Ajustar presupuesto'),)))
        context.action_binding = None
        context.trace.append({'source': 'backend', 'tool': 'propose_compensation', 'outcome': 'suggested'})
    except ValueError:
        logger.warning('compensation_suggestion_unavailable')


async def commit_compensation(context, operation, confirmation):
    value = CompensationPrepared.model_validate(operation.prepared_payload)
    if value.proposal['user_id'] != str(context.turn.owner_id):
        raise ValueError('La compensación pertenece a otra cuenta')
    require_status(await asyncio.to_thread(BudgetCompensationService.apply, value.proposal, now=context.turn.now), {'applied'})
    proposal = CompensationProposal.from_dict(value.proposal)
    for allocation in [proposal.target, *proposal.donors]:
        await refresh_limit(context, allocation.limit_id, allocation.category_name)
    return committed(context, 'Apliqué la compensación confirmada.')


def budget_contracts():
    return (read_tool('list_limits', 'Lista límites propios vigentes y futuros con refs y snapshots. reference_day filtra los que cubren ese día; category_ref y currency restringen categoría/moneda.', BudgetQuery, list_limits),
        read_tool('query_budget', 'Consulta presupuesto: consumido, disponible, porcentaje y excedente calculados por backend.', BudgetQuery, query_budget),
        *(operation_tool(key, 'Prepara crear o cambiar un límite mensual. Requiere categoría propia, importe, moneda, mes y año explícitos; limit_ref conserva la selección.',
            LimitDraft, LimitPrepared, prepare_limit, commit_limit) for key in ('create_limit', 'change_limit')),
        operation_tool('delete_limits', 'Prepara eliminar exactamente los límites propios seleccionados por refs.', DeleteLimits, DeleteLimitsPrepared, prepare_delete, commit_delete),
        operation_tool('propose_compensation', 'Propone compensación de presupuesto calculada por backend y exige confirmación del snapshot.',
            CompensationDraft, CompensationPrepared, prepare_compensation, commit_compensation))
