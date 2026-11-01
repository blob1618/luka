"""Movement reads and confirmed operations, backed by existing domain services."""
import asyncio
import logging
from uuid import UUID

from pydantic import Field

from app.models.database import MovimientoFinanciero
from app.services import finance
from app.services.assistant_contracts import (
    CommitResult, CorrectDatePrepared, CorrectMovementDate, DataResult, EntityReference, FieldRequest,
    FindMovementsInput, FrozenModel, IncompleteOperation, MovementSnapshot, OperationResult, PendingCommand,
    PreparedOperation, RegisterMovement, RegisterPrepared, ScopePayload, StaleReference, amount_text,
)
from app.services.assistant_registry import OperationContract, ToolContract, ToolPolicy
from app.services.assistant_capabilities.common import request_field
from app.services.movement_commands import commit_command, digest, snapshot, validate_dependencies
from app.services.movement_policy import registration_policy_reply
from app.services.wallet import WalletService

logger = logging.getLogger(__name__)


def visible_movement(row):
    return {key: row[key] for key in ('amount', 'currency', 'description', 'date', 'movement_type')}


class FindOutput(FrozenModel):
    status: str
    items: list[dict] = Field(default_factory=list)
    returned_count: int
    total_found: int


def _query(owner_id, args, scope, shown_ids, category_id, category_filter):
    with finance.SessionLocal() as session:
        q = session.query(MovimientoFinanciero).filter(MovimientoFinanciero.usuario_id == owner_id,
                                                      MovimientoFinanciero.anulado_en.is_(None))
        if shown_ids is not None:
            q = q.filter(MovimientoFinanciero.id.in_([UUID(identifier) for identifier in shown_ids]))
        if scope:
            start, end = scope.period.half_open_dates()
            q = q.filter(MovimientoFinanciero.fecha_movimiento >= start, MovimientoFinanciero.fecha_movimiento < end)
        descriptions = [value for value in (scope.description if scope else None, args.description) if value]
        for description in descriptions:
            escaped = description.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            q = q.filter(MovimientoFinanciero.descripcion.ilike(f'%{escaped}%', escape='\\'))
        currency = args.currency or (scope.currency if scope else None)
        movement_type = args.movement_type or (scope.movement_type if scope else None)
        if currency:
            q = q.filter(MovimientoFinanciero.moneda == currency)
        if movement_type:
            q = q.filter(MovimientoFinanciero.tipo == movement_type)
        if scope and scope.category_id:
            q = q.filter(MovimientoFinanciero.categoria_id == UUID(scope.category_id))
        if category_filter:
            q = q.filter(MovimientoFinanciero.categoria_id == UUID(category_id) if category_id else MovimientoFinanciero.categoria_id.is_(None))
        total = q.count()
        if args.sort_by == 'amount_desc' and not currency:
            raise ValueError('Para ordenar por importe necesito la moneda: ¿pesos o dólares?')
        orders = {'date_desc': (MovimientoFinanciero.fecha_movimiento.desc(), MovimientoFinanciero.creado_en.desc()),
                  'amount_desc': (MovimientoFinanciero.cantidad.desc(), MovimientoFinanciero.fecha_movimiento.desc()),
                  'registered_desc': (MovimientoFinanciero.creado_en.desc(), MovimientoFinanciero.fecha_movimiento.desc())}
        rows = [snapshot(row) for row in q.order_by(*orders[args.sort_by], MovimientoFinanciero.id.desc()).limit(args.limit).all()]
    return total, rows


async def find_movements(context, args):
    selector, scope, shown_ids, category_id = args.selector, None, None, None
    if selector.kind == 'scope':
        scope = await context.references.resolve(context.turn, selector.ref, kind='query_scope', payload_model=ScopePayload)
        if args.currency and scope.currency and args.currency != scope.currency:
            raise ValueError('currency es incompatible con el scope')
        if args.movement_type and scope.movement_type and args.movement_type != scope.movement_type:
            raise ValueError('movement_type es incompatible con el scope')
    elif selector.kind in {'month', 'period'}:
        period = selector.period() if selector.kind == 'month' else selector.period
        scope = ScopePayload(period=period)
    elif selector.kind == 'shown':
        shown_ids = [(await context.references.resolve(context.turn, ref, kind='movement', payload_model=MovementSnapshot)).id
                     for ref in selector.refs]
    if args.category_ref:
        category_id = (await context.references.resolve(context.turn, args.category_ref, kind='category', payload_model=EntityReference)).id
    total, rows = await asyncio.to_thread(_query, context.turn.owner_id, args, scope, shown_ids, category_id, bool(args.category_ref))
    items, refs, lines = [], [], []
    for row in rows:
        ref = await context.reference('movement', MovementSnapshot.model_validate(row))
        refs.append(ref)
        items.append({**visible_movement(row), 'movement_ref': ref})
        lines.append(f"{row['date']} — {row['description']}: {row['currency']} {amount_text(row['amount'])} ({row['movement_type']}).")
    context.state['last_shown'] = refs
    no_data_fact = None
    if rows and total > len(rows):
        lines.append(f'Mostré {len(rows)} de {total} movimientos.')
    elif not rows:
        message = 'No encontré movimientos con esos filtros.'
        no_data_fact = context.presenter.find(message)
        if no_data_fact is None:
            no_data_fact = context.presenter.add([message])[0]
    if total > len(rows) and scope is not None:
        from app.services.dashboard_link import DashboardLinkDecision, DashboardLinkService, dashboard_link_message
        link = await asyncio.to_thread(DashboardLinkService.generate_or_reuse, context.turn.phone,
            session_factory=finance.SessionLocal, now=context.turn.now,
            date_from=scope.period.start, date_to=scope.period.end)
        if link.decision == DashboardLinkDecision.SEND_LINK:
            context.outbound.append(dashboard_link_message(link.login_url, link.link_ttl_minutes,
                body='Podés ver los demás movimientos del período en tu dashboard.'))
            context.event_key = 'movements.query_result'
            context.event_variables = {'summary': '\n'.join(lines), 'dashboard_url': link.login_url,
                                       'ttl_minutes': str(link.link_ttl_minutes)}
    facts = context.presenter.add(lines)
    if not facts and no_data_fact is not None:
        facts = (no_data_fact,)
    return DataResult(data=FindOutput(status='ok' if rows else 'no_data', items=items,
        returned_count=len(rows), total_found=total).model_dump(mode='json'), facts=facts)


def _register_dependencies(owner, args, wallet_id, category_id):
    with finance.SessionLocal() as session:
        selection = WalletService.resolve_selection(session, owner, currency=args.currency, wallet_id=wallet_id)
        if selection.status != 'selected':
            return selection, None
        category = validate_dependencies(session, owner, selection.wallet_id, selection.currency, category_id)
        return selection, category.nombre if category else 'Sin categoría'


def _validate_correction(owner, expected):
    with finance.SessionLocal() as session:
        row = session.query(MovimientoFinanciero).filter(MovimientoFinanciero.id == UUID(expected.id),
            MovimientoFinanciero.usuario_id == owner, MovimientoFinanciero.anulado_en.is_(None)).first()
        if row is None:
            raise StaleReference('El movimiento ya no está disponible')
        if snapshot(row) != expected.model_dump(mode='json'):
            raise StaleReference('stale_context: el movimiento cambió')
        validate_dependencies(session, owner, str(row.billetera_id), row.moneda, str(row.categoria_id) if row.categoria_id else None)


class MovementOperation:
    def __init__(self, key, draft_model, command_kind):
        self.key, self.draft_model, self.command_kind = key, draft_model, command_kind

    async def prepare(self, context, operation):
        args = self.draft_model.model_validate(operation.draft_payload)
        required = ('movement_type', 'economic_effect', 'amount', 'description', 'date_spec') if self.command_kind == 'register' else ('movement_ref', 'date_spec')
        missing = [request_field(args, name) for name in required if getattr(args, name) is None]
        if self.command_kind == 'register':
            policy = registration_policy_reply(args.economic_effect)
            if policy:
                missing = [field for field in missing if field.field != 'economic_effect']
                missing.append(FieldRequest(field='economic_effect', reason='missing' if args.economic_effect is None else 'invalid', question=policy))
        if missing:
            return IncompleteOperation(missing_fields=tuple(missing))
        if self.command_kind == 'register' and args.requested_category and not args.category_ref:
            return IncompleteOperation(missing_fields=(request_field(args, 'category_ref'),))
        evidence = operation.field_evidence.get('date_spec')
        try:
            target = args.date_spec.resolve(evidence.source_text if evidence else operation.original_text,
                evidence.reference_day if evidence else operation.reference_day)
        except ValueError as exc:
            logger.warning('date_spec_invalid kind=%s has_span=%s detail=%s',
                           args.date_spec.kind, bool(args.date_spec.evidence_span), str(exc)[:120])
            return IncompleteOperation(missing_fields=(FieldRequest(field='date_spec', reason='invalid',
                question='La fecha no es válida. Decime el día: «hoy», «ayer» o «30/10».'),))
        if self.command_kind == 'register':
            wallet_id = (await context.references.resolve(context.turn, args.wallet_ref, kind='wallet', payload_model=EntityReference)).id if args.wallet_ref else None
            category_id = (await context.references.resolve(context.turn, args.category_ref, kind='category', payload_model=EntityReference)).id if args.category_ref else None
            selected_ref = context.state.get('selected_ref')
            if selected_ref:
                record = context.state['references'].get(selected_ref)
                if record and record['kind'] in {'wallet', 'category'} and getattr(args, record['kind'] + '_ref') != selected_ref:
                    raise ValueError('La propuesta no usa la opción que seleccionaste')
            selection, category_name = await asyncio.to_thread(_register_dependencies, context.turn.owner_id, args, wallet_id, category_id)
            if selection.status == 'needs_wallet_creation':
                return IncompleteOperation(missing_fields=(FieldRequest(
                    field='wallet_ref', reason='missing',
                    question=f"No tenés una billetera en {selection.currency}. ¿La creo?"),))
            if selection.status != 'selected':
                raise ValueError('No pude usar esa billetera; revisá la moneda y la billetera indicadas')
            payload = RegisterPrepared(economic_effect=args.economic_effect, movement_type=args.movement_type, amount=args.amount, currency=selection.currency,
                description=args.description, wallet_id=selection.wallet_id, category_id=category_id, date=target)
            preview = f'Registrar {args.movement_type}: {args.description}, {selection.currency} {amount_text(args.amount)}, fecha {target:%d/%m/%Y}, billetera {selection.wallet_name}. Categoría: {category_name}.'
        else:
            expected = await context.references.resolve(context.turn, args.movement_ref, kind='movement', payload_model=MovementSnapshot)
            selected = context.state.get('selected_ref')
            if selected and context.state['references'].get(selected, {}).get('kind') == 'movement' and selected != args.movement_ref:
                raise ValueError('La propuesta no usa el movimiento seleccionado')
            await asyncio.to_thread(_validate_correction, context.turn.owner_id, expected)
            payload = CorrectDatePrepared(movement_id=expected.id, expected=expected, date=target)
            preview = f'Corregir solo la fecha de {expected.description}: {expected.date} → {target:%d/%m/%Y}. Importe {expected.currency} {amount_text(expected.amount)}; demás datos sin cambios.'
        if target > context.turn.reference_day:
            preview += ' La fecha es futura.'
        return PreparedOperation(payload=payload.model_dump(mode='json'), facts=(), preview=preview)

    async def commit(self, context, operation, confirmation):
        if confirmation.prepared_hash != operation.prepared_hash or confirmation.owner_id != operation.owner_id:
            raise ValueError('La autorización cambió')
        payload = operation.prepared_payload
        pending = PendingCommand(owner_id=str(operation.owner_id), operation_id=operation.operation_id,
            source_message_id=operation.source_message_id, original_text=operation.original_text,
            reference_date=operation.reference_day.isoformat(), command_kind=self.command_kind,
            payload=payload, command_hash=digest(self.command_kind, payload), expires_at=operation.expires_at.isoformat(),
            version=operation.revision, authorization_hash=confirmation.prepared_hash)
        result = await asyncio.to_thread(commit_command, finance.SessionLocal, context.turn.owner_id,
            context.turn.phone, pending, confirmation.confirmation_message_id, now=context.turn.now)
        row = result['movement']
        ref = await context.reference('movement', MovementSnapshot.model_validate(row))
        context.action_binding = {'movement_ref': ref, 'operation_id': operation.operation_id,
            'revision': operation.revision + 1, 'prepared_hash': operation.prepared_hash}
        if not result.get('replayed'):
            from app.services.assistant_capabilities.budgets import suggest_compensation
            await suggest_compensation(context, operation, result)
        event = 'movement.registered' if self.command_kind == 'register' else 'movement.updated'
        return CommitResult(replayed=result.get('replayed', False), data=result, facts=(), event_key=event,
            event_variables={'movement_id': row['id'], 'movement_type': row['movement_type'],
                'description': row['description'], 'amount': amount_text(row['amount']), 'currency': row['currency'], 'category': result['category_name']})


def movement_contracts():
    contracts = [ToolContract('find_movements', 'Busca movimientos propios con selector scope, period, month, shown o all. '
        'sort_by siempre explícito: date_desc, registered_desc o amount_desc (requiere moneda). Scope conserva sus límites. Máximo cinco, informa total_found.',
        FindMovementsInput, FindOutput, find_movements, ToolPolicy('read'))]
    for key, draft, prepared, kind, description in (
        ('register_movement', RegisterMovement, RegisterPrepared, 'register', 'Prepara un registro individual, nunca confirma. Puede abrir borrador con campos faltantes. Fecha usa DateSpec con cita literal del turno que la aporta. Usa refs de categoría/billetera existentes.'),
        ('correct_movement_date', CorrectMovementDate, CorrectDatePrepared, 'correct_date', 'Prepara cambio solo de fecha de un movement_ref propio mostrado por find_movements. Usa DateSpec y nunca un UUID.')):
        handler = MovementOperation(key, draft, kind)
        operation = OperationContract(key, 1, draft, prepared, handler)
        async def open_operation(context, args, operation_key=key):
            return OperationResult(operation=await context.operations.open(context, operation_key=operation_key, draft=args))
        contracts.append(ToolContract(key, description, draft, None, open_operation,
            ToolPolicy('prepare', facts='optional', interrupt_active_operation=False, ends_turn=True), operation))
    return tuple(contracts)
