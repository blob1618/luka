"""Category management and wallet creation using existing ownership-aware services."""
import asyncio
from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import func

from app.models.database import Billetera, Categoria, MovimientoFinanciero
from app.services import finance
from app.services.assistant_contracts import EntityReference, FrozenModel, OperationReference, ReferenceToken, amount_text
from app.services.assistant_capabilities.common import committed, data, missing, operation_tool, prepared, read_tool, require_status
from app.services.finance import FinanceService
from app.services.category_creation import CategoryCreationService
from app.services.wallet import WalletService
from app.services.assistant_operations import merge_payload


def category_patch(path, ref):
    if path == 'changes.category_ref':
        return {'changes': {'category_ref': ref}}
    if path not in {'category_ref', 'target_category_ref', 'source_category_ref'}:
        raise ValueError('Campo de categoría padre inválido')
    return {path: ref}


def validate_parent_patch(context, parent, path):
    contract = context.operations.registry.get_operation(parent.operation_key)
    contract.draft_model.model_validate(merge_payload(parent.draft_payload, category_patch(path, 'ref_' + '0' * 32)))


class CategoryDraft(FrozenModel):
    name: str | None = Field(title='el nombre', default=None, min_length=1, max_length=100)
    category_ref: ReferenceToken | None = Field(default=None, title='la categoría que querés usar')
    parent_ref: ReferenceToken | None = None
    parent_revision: int | None = Field(default=None, ge=1)
    parent_field: str | None = None


class CategoryQuery(FrozenModel):
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')


class CategoryPrepared(FrozenModel):
    name: str
    category_id: str | None = None
    original_name: str | None = None
    parent_ref: ReferenceToken | None = None
    parent_revision: int | None = None
    parent_field: str | None = None


class WalletDraft(FrozenModel):
    name: str | None = Field(title='el nombre', default=None, min_length=1, max_length=100)
    currency: str | None = Field(title='la moneda', default=None, pattern=r'^[A-Z]{3}$')

    @field_validator('currency')
    @classmethod
    def _supported_currency(cls, value):
        if value is not None and value not in {'ARS', 'USD'}:
            raise ValueError('La moneda debe ser ARS o USD')
        return value


class WalletPrepared(FrozenModel):
    name: str | None = None
    currency: str


class WalletRenameDraft(FrozenModel):
    wallet_ref: ReferenceToken = Field(title='la billetera que querés renombrar')
    name: str = Field(title='el nuevo nombre', min_length=1, max_length=100)


class WalletRenamePrepared(FrozenModel):
    wallet_id: str
    original_name: str
    name: str
    currency: str


async def category_reference(context, ref):
    reference = await context.references.resolve(context.turn, ref, kind='category', payload_model=EntityReference)
    def check():
        with finance.SessionLocal() as session:
            row = session.query(Categoria).filter(Categoria.id == UUID(reference.id),
                Categoria.usuario_id == context.turn.owner_id, Categoria.esta_eliminado.is_(False)).first()
            if row is None or row.nombre != reference.label:
                raise ValueError('stale_context: la categoría cambió')
    await asyncio.to_thread(check)
    return reference


async def list_categories(context, args):
    result = require_status(await asyncio.to_thread(FinanceService.get_categories_with_totals, context.turn.owner_id), {'ok'})
    rows, lines = [], []
    totals = {}
    if args.currency:
        def aggregate():
            with finance.SessionLocal() as session:
                return session.query(MovimientoFinanciero.categoria_id, MovimientoFinanciero.tipo,
                    func.sum(MovimientoFinanciero.cantidad)).filter(MovimientoFinanciero.usuario_id == context.turn.owner_id,
                    MovimientoFinanciero.moneda == args.currency, MovimientoFinanciero.anulado_en.is_(None)).group_by(
                        MovimientoFinanciero.categoria_id, MovimientoFinanciero.tipo).all()
        totals = {(str(identifier), kind): amount for identifier, kind, amount in await asyncio.to_thread(aggregate)}
    for category in result.categories:
        ref = await context.reference('category', EntityReference(id=category.category_id, label=category.category_name))
        rows.append({'category_ref': ref, 'name': category.category_name})
        # The legacy service's totals combine currencies. This adapter only
        # publishes its category catalog; amounts remain in currency-aware reads.
        if args.currency:
            income = totals.get((category.category_id, 'ingreso'), 0)
            expense = totals.get((category.category_id, 'egreso'), 0)
            rows[-1].update(currency=args.currency, income=str(income), expense=str(expense))
            lines.append(f'{category.category_name}: {args.currency}, ingresos {amount_text(income)}, egresos {amount_text(expense)}.')
        else:
            lines.append(category.category_name + '.')
    return data(context, rows, lines or ['No hay categorías activas.'])


def category_operation(action):
    async def prepare_category(context, operation):
        draft = CategoryDraft.model_validate(operation.draft_payload)
        needed = ('name',) if action == 'create' else (('category_ref', 'name') if action == 'rename' else ('category_ref',))
        absent = missing(draft, needed)
        if absent:
            return absent
        previous = await category_reference(context, draft.category_ref) if draft.category_ref else None
        parent_revision, parent = None, None
        if draft.parent_ref:
            parent_ref = await context.references.resolve(context.turn, draft.parent_ref, kind='operation', payload_model=OperationReference)
            parent = context.operations.workspace(context).operations.get(parent_ref.operation_id)
            if (not parent or draft.parent_revision is None or parent.status != 'suspended'
                    or parent.revision not in {draft.parent_revision, draft.parent_revision + 1}):
                raise ValueError('El borrador padre cambió')
            parent_revision = parent.revision
            validate_parent_patch(context, parent, draft.parent_field)
        payload = CategoryPrepared(name=draft.name or previous.label,
            category_id=previous.id if previous else None, original_name=previous.label if previous else None,
            parent_ref=draft.parent_ref, parent_revision=parent_revision, parent_field=draft.parent_field)
        if action == 'create':
            context.event_key = 'category.confirmation_required'
            context.event_variables = {'category': payload.name}
        verb = {'create': 'Crear', 'rename': 'Renombrar', 'delete': 'Eliminar'}[action]
        return prepared(payload, f'{verb} categoría: {payload.name}.',
                        parent_operation_id=parent.operation_id if parent else None)
    async def commit_category(context, operation, confirmation):
        payload = CategoryPrepared.model_validate(operation.prepared_payload)
        parent = None
        if payload.parent_ref:
            parent = await context.operations.resolve(context, payload.parent_ref, payload.parent_revision)
            if parent.status != 'suspended':
                raise ValueError('El borrador padre ya no está suspendido')
            validate_parent_patch(context, parent, payload.parent_field)
        if action != 'create':
            await category_reference(context, CategoryDraft.model_validate(operation.draft_payload).category_ref)
        if action == 'create':
            result = await asyncio.to_thread(CategoryCreationService.confirm, context.turn.phone, payload.name)
            require_status(result, {'created', 'already_exists'})
        elif action == 'rename':
            result = await asyncio.to_thread(FinanceService.rename_category, context.turn.owner_id,
                payload.original_name, payload.name, expected_category_id=payload.category_id)
            require_status(result, {'renamed'})
        else:
            result = await asyncio.to_thread(FinanceService.delete_category, context.turn.owner_id, payload.original_name)
            require_status(result, {'deleted'})
        if action == 'delete':
            context.references.retire('category', payload.category_id)
        else:
            await context.reference('category', EntityReference(id=result.category_id, label=result.category_name))
        # The child can complete its parent's typed category field. It never commits
        # the parent: amend produces a new preview requiring its own delivered approval.
        if payload.parent_ref and action == 'create':
            ref = await context.reference('category', EntityReference(id=result.category_id, label=result.category_name))
            await context.operations.amend(context, payload.parent_ref, expected_revision=parent.revision,
                changes=category_patch(payload.parent_field, ref))
        return committed(context, result.message, event='category.deleted' if action == 'delete' else None,
            variables={'category': payload.name}, parent_updated=bool(parent))
    return operation_tool(action + '_category', 'Prepara '+action+' de categoría propia; creación/reactivación exige aprobación. '
        'Para completar un borrador padre, copiar parent_ref, parent_revision y parent_field; no confirma el padre.',
        CategoryDraft, CategoryPrepared, prepare_category, commit_category)


async def prepare_wallet(context, operation):
    draft = WalletDraft.model_validate(operation.draft_payload)
    absent = missing(draft, ('currency',))
    if absent:
        return absent
    label = f'«{draft.name}»' if draft.name else f'en {draft.currency}'
    return prepared(WalletPrepared(**draft.model_dump()), f'Crear billetera {label}.')


async def commit_wallet(context, operation, confirmation):
    payload = WalletPrepared.model_validate(operation.prepared_payload)
    result = require_status(await asyncio.to_thread(WalletService.create_wallet, context.turn.owner_id,
        payload.name, payload.currency), {'created', 'wallet_already_exists'})
    if result.status == 'wallet_already_exists':
        return committed(context, f'Ya tenés una billetera en {payload.currency}: «{result.name}». Usala o renombrala.')
    return committed(context, f'Billetera «{result.name}» creada.')


async def prepare_wallet_rename(context, operation):
    draft = WalletRenameDraft.model_validate(operation.draft_payload)
    absent = missing(draft, ('wallet_ref', 'name'))
    if absent:
        return absent
    wallet = await context.references.resolve(context.turn, draft.wallet_ref, kind='wallet',
                                               payload_model=EntityReference)

    def check():
        with finance.SessionLocal() as session:
            row = session.query(Billetera).filter(
                Billetera.id == UUID(wallet.id),
                Billetera.usuario_id == context.turn.owner_id,
            ).first()
            if row is None or row.nombre != wallet.label:
                raise ValueError('stale_context: la billetera cambió')
            return row

    row = await asyncio.to_thread(check)
    payload = WalletRenamePrepared(wallet_id=str(row.id), original_name=row.nombre,
                                   name=draft.name, currency=row.moneda)
    return prepared(payload, f'Renombrar billetera «{row.nombre}» a «{draft.name}».')


async def commit_wallet_rename(context, operation, confirmation):
    payload = WalletRenamePrepared.model_validate(operation.prepared_payload)
    result = require_status(await asyncio.to_thread(WalletService.rename_wallet,
        context.turn.owner_id, payload.wallet_id, payload.name), {'renamed'})
    await context.reference('wallet', EntityReference(id=result.wallet_id, label=result.name))
    return committed(context, f'Billetera renombrada a «{result.name}».')


def entity_contracts():
    return (read_tool('list_categories', 'Lista categorías propias activas con referencias seleccionables. Si se piden totales, especificar currency: jamás sumar monedas diferentes.', CategoryQuery, list_categories),
        *(category_operation(action) for action in ('create', 'rename', 'delete')),
        operation_tool('create_wallet', 'Prepara crear una billetera propia. Si no se indica nombre, usa «Pesos» para ARS y «Dólares» para USD.',
            WalletDraft, WalletPrepared, prepare_wallet, commit_wallet),
        operation_tool('rename_wallet', 'Prepara renombrar la billetera propia indicada.',
            WalletRenameDraft, WalletRenamePrepared, prepare_wallet_rename, commit_wallet_rename))
