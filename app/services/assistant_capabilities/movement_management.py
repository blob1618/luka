"""Batch registration, typed edits and annulment share the durable movement receipt."""
import asyncio
from datetime import date

from pydantic import Field, field_validator

from app.services import finance
from app.services.assistant_contracts import (
    DateSpec, EntityReference, FrozenModel, MovementSnapshot, PendingCommand,
    PreparedOperation, ReferenceToken, RegisterMovement, RegisterPrepared, CommitResult, amount_text,
)
from app.services.assistant_capabilities.common import missing, operation_tool, positive, prepared
from app.services.assistant_capabilities.entities import category_reference
from app.services.assistant_capabilities.movements import MovementOperation, _validate_correction
from app.services.assistant_operations import revised
from app.services.movement_commands import commit_command, digest
from app.services.wallet import WalletService


class BatchDraft(FrozenModel):
    items: tuple[RegisterMovement, ...] = Field(default=(), max_length=5)


class BatchPrepared(FrozenModel):
    items: tuple[RegisterPrepared, ...] = Field(min_length=2, max_length=5)


class MovementChanges(FrozenModel):
    amount: str | None = Field(default=None, title='el importe')
    description: str | None = Field(title='el concepto del movimiento', default=None, min_length=1, max_length=500)
    category_ref: ReferenceToken | None = Field(default=None, title='la categoría que querés usar')
    clear_category: bool = False
    wallet_ref: ReferenceToken | None = Field(default=None, title='la billetera que querés usar')
    currency: str | None = Field(title='la moneda', default=None, pattern=r'^[A-Z]{3}$')
    movement_type: str | None = Field(title='si fue un ingreso o un gasto', default=None, pattern=r'^(ingreso|egreso)$')
    date_spec: DateSpec | None = Field(default=None, title='la fecha del movimiento')
    _positive = field_validator('amount')(positive)


class UpdateDraft(FrozenModel):
    movement_ref: ReferenceToken | None = Field(default=None, title='qué movimiento de los mostrados querés usar')
    changes: MovementChanges | None = Field(default=None, title='qué campos querés cambiar')
    require_category: bool = False


class UpdatePrepared(FrozenModel):
    movement_id: str
    expected: MovementSnapshot
    changes: dict


class AnnulDraft(FrozenModel):
    movement_refs: tuple[ReferenceToken, ...] = Field(default=(), max_length=5)


class AnnulPrepared(FrozenModel):
    selected: tuple[MovementSnapshot, ...] = Field(min_length=1, max_length=5)


async def prepare_batch(context, operation):
    draft = BatchDraft.model_validate(operation.draft_payload)
    if len(draft.items) < 2:
        from app.services.assistant_contracts import FieldRequest, IncompleteOperation
        return IncompleteOperation(missing_fields=(FieldRequest(field='items', reason='missing', question='Indicá los movimientos que querés registrar juntos.'),))
    items, previews = [], []
    handler = MovementOperation('register_movement', RegisterMovement, 'register')
    for index, item in enumerate(draft.items):
        evidence = {key: operation.field_evidence.get(f'items.{index}.{key}') or operation.field_evidence.get('items')
                    for key in item.model_fields_set}
        part = revised(operation, draft_payload=item.model_dump(mode='json'), prepared_payload=None, prepared_hash=None,
            status='collecting', resume_status=None,
            field_evidence={key: value for key, value in evidence.items() if value is not None})
        result = await handler.prepare(context, part)
        if not isinstance(result, PreparedOperation):
            from app.services.assistant_contracts import IncompleteOperation
            return IncompleteOperation(missing_fields=tuple(revised(field, field=f'items.{index}.{field.field}') for field in result.missing_fields))
        items.append(RegisterPrepared.model_validate(result.payload))
        previews.append(result.preview)
    return prepared(BatchPrepared(items=tuple(items)), '\n'.join(previews))


def _update_wallet_selection(owner, currency, wallet_id):
    with finance.SessionLocal() as session:
        return WalletService.resolve_selection(session, owner, currency=currency, wallet_id=wallet_id)


async def _ask_update_wallet(context, selection):
    from app.services.assistant_contracts import FieldRequest, IncompleteOperation
    candidates, options = [], {}
    for wallet in selection.options:
        ref = await context.reference('wallet', EntityReference(id=wallet.wallet_id, label=wallet.name))
        candidates.append(ref)
        options[str(len(options) + 1)] = {'ref': ref, 'label': wallet.name}
    context.state['selection_options'] = options
    return IncompleteOperation(missing_fields=(FieldRequest(field='changes.wallet_ref', reason='ambiguous',
        candidate_refs=tuple(candidates),
        question='¿A qué billetera querés mover ese movimiento? Elegí una de las opciones.'),))


async def prepare_update(context, operation):
    args = UpdateDraft.model_validate(operation.draft_payload)
    absent = missing(args, ('movement_ref', 'changes'))
    if absent:
        return absent
    target = await context.references.resolve(context.turn, args.movement_ref, kind='movement', payload_model=MovementSnapshot)
    await asyncio.to_thread(_validate_correction, context.turn.owner_id, target)
    changes = args.changes
    if args.require_category and not changes.category_ref and not changes.clear_category:
        from app.services.assistant_contracts import FieldRequest, IncompleteOperation
        return IncompleteOperation(missing_fields=(FieldRequest(field='changes.category_ref', reason='missing', question='¿Qué categoría querés asignarle a este movimiento?'),))
    patch = {name: getattr(changes, name) for name in ('amount', 'description', 'movement_type') if getattr(changes, name) is not None}
    wallet_name = wallet_id = None
    if changes.wallet_ref:
        wallet = await context.references.resolve(context.turn, changes.wallet_ref, kind='wallet', payload_model=EntityReference)
        wallet_id, wallet_name = wallet.id, wallet.label
    wanted_currency = changes.currency.upper() if changes.currency else None
    # Repeating the current currency is a no-op; only a real change needs a wallet.
    if wanted_currency is not None and wanted_currency == target.currency and not wallet_id:
        wanted_currency = None
    if wallet_id or wanted_currency:
        # Resolve the wallet while preparing so ambiguity is asked here, not after confirming.
        selection = await asyncio.to_thread(_update_wallet_selection, context.turn.owner_id,
            wanted_currency, wallet_id)
        if selection.status == 'needs_wallet_selection':
            return await _ask_update_wallet(context, selection)
        if selection.status == 'needs_wallet_creation':
            from app.services.assistant_contracts import FieldRequest, IncompleteOperation
            return IncompleteOperation(missing_fields=(FieldRequest(field='changes.wallet_ref', reason='missing',
                question=f"No tenés una billetera en {selection.currency or wanted_currency}. Creá una en esa moneda antes de cambiar el movimiento."),))
        if selection.status != 'selected':
            raise ValueError('No pude usar esa billetera; revisá la moneda y la billetera indicadas')
        patch['wallet_id'] = selection.wallet_id
        patch['currency'] = selection.currency
        wallet_name = selection.wallet_name
    if changes.category_ref:
        patch['category'] = (await category_reference(context, changes.category_ref)).label
    elif changes.clear_category:
        patch['category'] = None
    if changes.date_spec:
        evidence = operation.field_evidence.get('changes.date_spec') or operation.field_evidence.get('changes')
        try:
            patch['fecha'] = changes.date_spec.resolve(evidence.source_text, evidence.reference_day).isoformat()
        except (ValueError, AttributeError):
            from app.services.assistant_contracts import FieldRequest, IncompleteOperation
            return IncompleteOperation(missing_fields=(FieldRequest(field='changes.date_spec', reason='invalid',
                question='La fecha no es válida. Indicá un día concreto para este movimiento.'),))
    if not patch:
        raise ValueError('Indicá al menos un campo a modificar')
    parts = []
    if 'amount' in patch:
        parts.append(f"importe {patch.get('currency', target.currency)} {amount_text(patch['amount'])}")
    if 'currency' in patch:
        parts.append(f"moneda {patch['currency']}")
    if wallet_name:
        parts.append(f'billetera {wallet_name}')
    if 'description' in patch:
        parts.append(f"concepto {patch['description']}")
    if 'movement_type' in patch:
        parts.append(f"tipo {patch['movement_type']}")
    if 'category' in patch:
        parts.append('categoría ' + (patch['category'] or 'sin categoría'))
    if 'fecha' in patch:
        parts.append(f"fecha {date.fromisoformat(patch['fecha']):%d/%m/%Y}")
    return prepared(UpdatePrepared(movement_id=target.id, expected=target, changes=patch),
        f'Actualizar {target.description}. Cambios: ' + ', '.join(parts) + '.')


async def prepare_annul(context, operation):
    args = AnnulDraft.model_validate(operation.draft_payload)
    if not args.movement_refs:
        from app.services.assistant_contracts import FieldRequest, IncompleteOperation
        return IncompleteOperation(missing_fields=(FieldRequest(field='movement_refs', reason='missing', question='Elegí de los movimientos mostrados cuáles querés anular.'),))
    rows = tuple([await context.references.resolve(context.turn, ref, kind='movement', payload_model=MovementSnapshot) for ref in args.movement_refs])
    if len({row.id for row in rows}) != len(rows):
        raise ValueError('La selección contiene movimientos repetidos')
    for row in rows:
        await asyncio.to_thread(_validate_correction, context.turn.owner_id, row)
    return prepared(AnnulPrepared(selected=rows), 'Anular: ' + '; '.join(
        f'{row.description}, {row.currency} {amount_text(row.amount)}, {row.date:%d/%m/%Y}' for row in rows) + '.')


def commit_mutation(kind):
    async def commit(context, operation, confirmation):
        pending = PendingCommand(owner_id=str(operation.owner_id), operation_id=operation.operation_id,
            source_message_id=operation.source_message_id, original_text=operation.original_text,
            reference_date=operation.reference_day.isoformat(), command_kind=kind, payload=operation.prepared_payload,
            command_hash=digest(kind, operation.prepared_payload), authorization_hash=confirmation.prepared_hash,
            expires_at=operation.expires_at.isoformat(), version=operation.revision)
        result = await asyncio.to_thread(commit_command, finance.SessionLocal, context.turn.owner_id, context.turn.phone,
            pending, confirmation.confirmation_message_id, now=context.turn.now)
        row = result['movement']
        if kind == 'annul':
            for item in result['movements']:
                context.references.retire('movement', item['id'])
        else:
            for item in result.get('movements', [row]):
                await context.reference('movement', MovementSnapshot.model_validate(item))
            from app.services.assistant_capabilities.budgets import suggest_compensation
            await suggest_compensation(context, operation, result)
        return CommitResult(replayed=result.get('replayed', False), data=result, facts=(),
            event_key='movement.annulled' if kind == 'annul' else 'movement.updated',
            event_variables={'description': row['description'], 'amount': amount_text(row['amount']), 'currency': row['currency'],
                             'category': result['category_name']})
    return commit


def movement_management_contracts():
    return (
        operation_tool('register_movement_batch', 'Prepara entre dos y cinco movimientos de un pedido como lote atómico. Cada item conserva importe, moneda, billetera, categoría y evidencia de fecha. Una aprobación del lote completo.',
            BatchDraft, BatchPrepared, prepare_batch, commit_mutation('register_batch')),
        operation_tool('update_movement', 'Prepara editar exclusivamente los campos indicados de un movimiento propio mostrado. Usa movement_ref y cambios tipados; conserva snapshot y exige aprobación.',
            UpdateDraft, UpdatePrepared, prepare_update, commit_mutation('update')),
        operation_tool('annul_movements', 'Prepara anular uno o hasta cinco movimientos propios seleccionados por refs. Conjunto y snapshots exactos, sin borrar recibos.',
            AnnulDraft, AnnulPrepared, prepare_annul, commit_mutation('annul')),
    )
