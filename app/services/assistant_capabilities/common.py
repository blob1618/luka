"""Small composition helpers for typed local domain adapters."""
import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import UUID

from pydantic import BaseModel, Field

from app.services import finance
from app.services.assistant_contracts import (
    CommitResult, DataResult, FieldRequest, FrozenModel, IncompleteOperation, OperationResult,
    PreparedOperation, ResourceReference, ToolData,
)
from app.services.assistant_registry import OperationContract, ToolContract, ToolPolicy

Money = str


class ResourceInput(FrozenModel):
    ref: str | None = None


class NamedInput(FrozenModel):
    name: str | None = Field(title='el nombre', default=None, min_length=1, max_length=100)


def missing(draft, names):
    fields = tuple(request_field(draft, name) for name in names if getattr(draft, name) is None)
    return IncompleteOperation(missing_fields=fields) if fields else None


def request_field(draft, name, *, reason='missing', candidate_refs=()):
    label = type(draft).model_fields[name].title
    return FieldRequest(field=name, reason=reason, candidate_refs=candidate_refs,
        question=f'Necesito que me indiques {label}.' if label else 'Me falta un dato para completar la propuesta. Indicá cómo querés completarla.')


def require_status(result, allowed):
    if result.status not in allowed:
        raise ValueError(result.message if hasattr(result, 'message') else result.status)
    return result


def positive(value):
    if value is not None:
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError('El importe debe ser un número decimal positivo') from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError('El importe debe ser positivo')
    return value


async def entity(context, ref, kind):
    return await context.references.resolve(context.turn, ref, kind=kind, payload_model=ResourceReference)


def snapshot_row(row, fields):
    values = {}
    for name in fields:
        value = getattr(row, name)
        values[name] = value if isinstance(value, (bool, int)) or value is None else str(value)
    return values


def owned_snapshot(owner, model, identifier, fields, *, expected=None):
    with finance.SessionLocal() as session:
        row = session.query(model).filter(model.id == UUID(identifier), model.usuario_id == owner).first()
        if row is None:
            raise ValueError('La referencia no pertenece a esta cuenta')
        snapshot = snapshot_row(row, fields)
        if expected is not None and snapshot != expected:
            raise ValueError('stale_context: el recurso cambió')
        return snapshot


async def check_resource(context, ref, kind, model, fields):
    resource = await entity(context, ref, kind)
    await asyncio.to_thread(owned_snapshot, context.turn.owner_id, model, resource.id, fields, expected=resource.snapshot)
    return resource


async def issue_resource(context, kind, identifier, snapshot, visible):
    return await context.reference(kind, ResourceReference(id=str(identifier), snapshot=snapshot, visible=visible))


async def refresh_resource(context, kind, model, identifier, fields, visible):
    snapshot = await asyncio.to_thread(owned_snapshot, context.turn.owner_id, model, str(identifier), fields)
    return await issue_resource(context, kind, identifier, snapshot, visible(snapshot))


def data(context, rows, lines, *, status='ok'):
    return DataResult(data=ToolData(status=status, items=rows).model_dump(mode='json'),
                      facts=context.presenter.add(lines))


def committed(context, line, *, event=None, variables=None, **result):
    return CommitResult(replayed=False, data={'presentation': line, **result}, facts=(),
                        event_key=event, event_variables=variables or {})


@dataclass
class DomainHandler:
    prepare: object
    commit: object


def operation_tool(name, description, draft_model, prepared_model, prepare, commit, *, read=False):
    contract = OperationContract(name, 1, draft_model, prepared_model, DomainHandler(prepare, commit), not read)
    async def open_operation(context, args):
        return OperationResult(operation=await context.operations.open(context, operation_key=name, draft=args))
    return ToolContract(name, description, draft_model, None, open_operation,
        ToolPolicy('read' if read else 'prepare', facts='optional', interrupt_active_operation=False, ends_turn=True), contract)


def read_tool(name, description, model, handler, *, terminal=False):
    return ToolContract(name, description, model, ToolData, handler,
        ToolPolicy('read', ends_turn=terminal))


def prepared(payload: BaseModel, preview, *, parent_operation_id=None):
    return PreparedOperation(payload=payload.model_dump(mode='json'), facts=(), preview=preview,
                             parent_operation_id=parent_operation_id)
