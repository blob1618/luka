"""Explicit capability composition and policies; no domain dispatch in the loop."""
import asyncio
from dataclasses import dataclass
import inspect
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel

from app.services.assistant_contracts import (
    AuthRequiredResult, ClarificationResult, DataResult, OperationResult, ToolFailure,
    ConfirmedAction, CommitResult, IncompleteOperation, PendingOperation, PreparedOperation, ToolOutcome,
)

if TYPE_CHECKING:
    from app.services.assistant_context import ToolContext


class ToolHandler(Protocol):
    async def __call__(self, context: 'ToolContext', arguments: BaseModel) -> ToolOutcome: ...


class OperationHandler(Protocol):
    async def prepare(self, context: 'ToolContext', operation: PendingOperation) -> PreparedOperation | IncompleteOperation | AuthRequiredResult: ...
    async def commit(self, context: 'ToolContext', operation: PendingOperation, confirmation: ConfirmedAction) -> CommitResult: ...


@dataclass(frozen=True)
class ToolPolicy:
    effect: Literal['read', 'prepare', 'control']
    permissions: frozenset[str] = frozenset({'own_account'})
    timeout_seconds: float = 15
    facts: Literal['required', 'optional', 'none'] = 'required'
    interrupt_active_operation: bool = True
    ends_turn: bool = False


@dataclass(frozen=True)
class OperationContract:
    key: str
    schema_version: int
    draft_model: type[BaseModel]
    prepared_model: type[BaseModel]
    handler: OperationHandler
    requires_confirmation: bool = True


@dataclass(frozen=True)
class ToolContract:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None
    handler: ToolHandler
    policy: ToolPolicy
    operation: OperationContract | None = None


class ToolRegistry:
    def __init__(self, *, receipt_lookup=None):
        self.tools, self.operations = {}, {}
        self.receipt_lookup = receipt_lookup

    async def receipt(self, owner_id, **selector):
        if self.receipt_lookup is None:
            return None
        return await asyncio.to_thread(self.receipt_lookup, owner_id, **selector)

    def register(self, contract):
        if contract.name in self.tools or not inspect.iscoroutinefunction(contract.handler):
            raise ValueError('Duplicate capability or non-async handler')
        if contract.operation is not None:
            operation = contract.operation
            if (operation.schema_version < 1
                    or not inspect.iscoroutinefunction(operation.handler.prepare)
                    or not inspect.iscoroutinefunction(operation.handler.commit)):
                raise ValueError('Prepare requires a confirmed operation contract')
            if operation.requires_confirmation != (contract.policy.effect == 'prepare'):
                raise ValueError('Mutation operations require confirmation; read operations cannot mutate')
            if operation.key in self.operations and self.operations[operation.key] != operation:
                raise ValueError('Conflicting operation contract')
            self.operations[operation.key] = operation
        elif contract.policy.effect == 'prepare':
            raise ValueError('Prepare requires an operation contract')
        self.tools[contract.name] = contract

    def get(self, name):
        if name not in self.tools:
            raise ValueError('Herramienta no autorizada')
        return self.tools[name]

    def get_operation(self, key):
        if key not in self.operations:
            raise ValueError('Esquema de operación desconocido')
        return self.operations[key]

    def available(self, turn):
        return tuple(self.tools.values())


class ToolExecutor:
    def __init__(self, registry):
        self.registry = registry

    async def execute(self, context, *, name, arguments, timeout=None):
        contract = self.registry.get(name)
        args = contract.input_model.model_validate(arguments)
        if not contract.policy.permissions <= {'own_account'}:
            raise ValueError('Permiso no disponible')
        if contract.policy.effect == 'prepare' and not context.writable:
            raise ValueError('Sin contexto disponible no puedo preparar una escritura')
        if contract.policy.interrupt_active_operation:
            await context.operations.interrupt(context)
        outcome = await asyncio.wait_for(contract.handler(context, args),
            min(contract.policy.timeout_seconds, timeout) if timeout is not None else contract.policy.timeout_seconds)
        if not isinstance(outcome, (DataResult, OperationResult, ClarificationResult, AuthRequiredResult, ToolFailure)):
            raise ValueError('Resultado de herramienta inválido')
        # Reconstruct instead of trusting potentially unvalidated model copies.
        outcome = type(outcome).model_validate(outcome.model_dump(mode='json'))
        if isinstance(outcome, DataResult) and contract.output_model is not None:
            contract.output_model.model_validate(outcome.data)
        facts = getattr(outcome, 'facts', ())
        if contract.policy.facts == 'required' and isinstance(outcome, DataResult) and not facts:
            raise ValueError('Faltan los hechos obligatorios del resultado')
        context.presenter.include(facts)
        return outcome
