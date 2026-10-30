"""Validated workspace transitions. Entry points hold the owner's Redis claim."""
from datetime import timedelta
import hashlib
import hmac

from app.services.assistant_contracts import (
    AuthRequiredResult, CommitResult, ConfirmedAction, ConfirmationPointer, ConversationWorkspace,
    FieldEvidence, FieldRequest, IncompleteOperation, OperationReference, PendingOperation, PendingOperationView,
    PreviewDelivery, PreparedOperation, StaleReference,
)
from app.services.movement_commands import digest, operation_id

ACTIVE = {'collecting', 'ready', 'awaiting_auth'}
TERMINAL = {'committed', 'cancelled', 'expired'}


def revised(model, **changes):
    return type(model).model_validate({**model.model_dump(mode='json'), **changes})


def short_token(revision, prepared_hash):
    return hashlib.sha256(f'{revision}:{prepared_hash}'.encode()).hexdigest()[:16]


def merge_payload(base, patch):
    result = dict(base)
    for key, value in patch.items():
        result[key] = merge_payload(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


class OperationService:
    def __init__(self, registry, *, max_open_operations=3, operation_ttl_seconds=1800,
                 max_terminal_operations=3):
        self.registry = registry
        self.max_open_operations = max_open_operations
        self.operation_ttl_seconds = operation_ttl_seconds
        self.max_terminal_operations = max_terminal_operations

    def workspace(self, context):
        workspace = ConversationWorkspace.model_validate(context.state['workspace'])
        if workspace.owner_id != context.turn.owner_id or workspace.conversation_id != context.turn.conversation_id:
            raise ValueError('El workspace pertenece a otra conversación')
        for operation in workspace.operations.values():
            contract = self.registry.get_operation(operation.operation_key)
            if operation.schema_version != contract.schema_version:
                raise ValueError('Versión de operación desconocida')
            contract.draft_model.model_validate(operation.draft_payload)
            if operation.prepared_payload is not None:
                prepared = contract.prepared_model.model_validate(operation.prepared_payload).model_dump(mode='json')
                if prepared != operation.prepared_payload or digest(operation.operation_key, prepared) != operation.prepared_hash:
                    raise ValueError('La propuesta preparada cambió')
        return workspace

    def save(self, context, workspace):
        # Validate full workspace and domain payloads before exposing it to persistence.
        previous = context.state['workspace']
        workspace = ConversationWorkspace.model_validate(workspace)
        terminal = sorted((op for op in workspace.operations.values() if op.status in TERMINAL),
                          key=lambda op: (op.updated_at, op.created_at))
        removed = {op.operation_id for op in terminal[:max(0, len(terminal) - self.max_terminal_operations)]}
        if removed:
            workspace = revised(workspace, operations={key: op for key, op in workspace.operations.items() if key not in removed})
        context.state['workspace'] = workspace.model_dump(mode='json')
        try:
            self.workspace(context)
        except Exception:
            context.state['workspace'] = previous
            raise
        context.state['operation_summaries'] = {key: summary for key, summary in
            context.state.get('operation_summaries', {}).items() if key in workspace.operations}
        for ref, record in list(context.state['references'].items()):
            if record['kind'] == 'operation' and record['payload'].get('operation_id') in removed:
                del context.state['references'][ref]

    def update(self, context, workspace, operation, *, pointer=None):
        operations = dict(workspace.operations)
        operations[operation.operation_id] = operation
        active_id = operation.operation_id if operation.status in ACTIVE else workspace.active_operation_id
        if active_id == operation.operation_id and operation.status not in ACTIVE:
            active_id = None
        data = workspace.model_dump(mode='json')
        data.update(operations={key: value.model_dump(mode='json') for key, value in operations.items()},
                    active_operation_id=active_id,
                    last_confirmation=pointer.model_dump(mode='json') if pointer else None)
        self.save(context, data)
        if operation.status in TERMINAL and active_id is None:
            context.state.pop('selection_options', None)
            context.state.pop('selected_ref', None)

    async def expire(self, context):
        workspace = self.workspace(context)
        for operation in list(workspace.operations.values()):
            if operation.status not in TERMINAL and operation.expires_at <= context.turn.now:
                operation = revised(operation, status='expired', resume_status=None,
                    revision=operation.revision + 1, updated_at=context.turn.now)
                self.update(context, self.workspace(context), operation)
        # Compact an already populated workspace on ordinary reads as well. Durable
        # receipts, rather than these terminal task records, provide mutation replay.
        self.save(context, self.workspace(context))

    async def view(self, context, operation):
        ref = None
        for token, record in context.state['references'].items():
            if (record['kind'] == 'operation' and record['payload'].get('operation_id') == operation.operation_id
                    and record['expires_at'] == operation.expires_at.isoformat()):
                ref = token
                break
        if ref is None:
            ref = await context.reference('operation', OperationReference(operation_id=operation.operation_id),
                                          expires_at=operation.expires_at)
        return PendingOperationView(ref=ref, operation_key=operation.operation_key, status=operation.status,
            revision=operation.revision, summary=context.state.get('operation_summaries', {}).get(operation.operation_id, operation.operation_key),
            known_fields=operation.draft_payload, missing_fields=operation.missing_fields, expires_at=operation.expires_at)

    async def views(self, context):
        await self.expire(context)
        return tuple([await self.view(context, operation) for operation in self.workspace(context).operations.values()
                      if operation.status not in TERMINAL])

    async def resolve(self, context, ref, expected_revision):
        await self.expire(context)
        record = await context.references.resolve(context.turn, ref, kind='operation', payload_model=OperationReference)
        operation = self.workspace(context).operations.get(record.operation_id)
        if operation is None or operation.status in TERMINAL or operation.revision != expected_revision:
            raise StaleReference('La operación cambió o ya no está vigente')
        return operation

    def evidence(self, context, values, previous=None, explicit=()):
        evidence = {}
        def visit(name, value, old):
            if old == value and not any(path == name or path.startswith(name + '.') for path in explicit):
                return
            span = value.get('evidence_span') if isinstance(value, dict) else None
            evidence[name] = FieldEvidence(source_message_id=context.turn.inbound_message_id,
                reference_day=context.turn.reference_day, source_text=context.turn.source_text, literal_span=span)
            if isinstance(value, dict):
                for key, item in value.items():
                    visit(name + '.' + key, item, old.get(key) if isinstance(old, dict) else None)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(name + '.' + str(index), item, old[index] if isinstance(old, list) and index < len(old) else None)
        for name, value in values.items():
            visit(name, value, (previous or {}).get(name))
        return evidence

    async def prepare(self, context, operation):
        contract = self.registry.get_operation(operation.operation_key)
        try:
            result = await contract.handler.prepare(context, operation)
        except StaleReference:
            # Only handles that no longer match durable state are reported as stale.
            result = IncompleteOperation(missing_fields=(FieldRequest(field='dependencies', reason='stale',
                question='Los datos de la propuesta necesitan una nueva revisión. Volvé a consultarlos y elegir el registro vigente.'),))
        except ValueError as exc:
            # Domain validation errors keep their own message instead of blaming stale data.
            result = IncompleteOperation(missing_fields=(FieldRequest(field='dependencies', reason='invalid',
                question=str(exc) or 'No pude preparar la propuesta con esos datos; revisá el pedido y volvé a intentarlo.'),))
        if isinstance(result, IncompleteOperation):
            return revised(operation, status='collecting', resume_status=None, missing_fields=result.missing_fields,
                           prepared_payload=None, prepared_hash=None)
        if isinstance(result, AuthRequiredResult):
            return revised(operation, status='awaiting_auth', resume_status=None,
                           prepared_payload=None, prepared_hash=None)
        payload = contract.prepared_model.model_validate(result.payload).model_dump(mode='json')
        if result.parent_operation_id and result.parent_operation_id not in self.workspace(context).operations:
            raise ValueError('La operación padre no pertenece a este workspace')
        context.presenter.include(result.facts)
        context.state.setdefault('operation_summaries', {})[operation.operation_id] = result.preview
        return revised(operation, status='ready', resume_status=None, missing_fields=(),
            prepared_payload=payload, prepared_hash=digest(operation.operation_key, payload),
            parent_operation_id=result.parent_operation_id)

    def live_operations(self, workspace):
        return [op for op in workspace.operations.values() if op.status not in TERMINAL]

    @staticmethod
    def _draft_label(operation):
        text = ' '.join((operation.original_text or '').split())
        if not text:
            return operation.operation_key.replace('_', ' ')
        return text if len(text) <= 60 else text[:57].rstrip() + '…'

    def _capacity_message(self, workspace):
        live = sorted(self.live_operations(workspace), key=lambda op: (op.updated_at, op.created_at))
        labels = ', '.join(f'«{self._draft_label(op)}»' for op in live)
        return (f'Tenés demasiadas propuestas abiertas: {labels}. '
                'Confirmá o cancelá una antes de empezar otra; si querés cancelar, nombrá el borrador.')

    def open_draft_labels(self, workspace):
        return [self._draft_label(op) for op in
                sorted(self.live_operations(workspace), key=lambda op: (op.updated_at, op.created_at))]

    async def open(self, context, *, operation_key, draft):
        await self.expire(context)
        workspace = self.workspace(context)
        if len(self.live_operations(workspace)) >= self.max_open_operations:
            raise ValueError(self._capacity_message(workspace))
        contract = self.registry.get_operation(operation_key)
        draft = contract.draft_model.model_validate(draft.model_dump(mode='json', exclude_unset=True))
        op_id = operation_id(context.turn.owner_id, context.turn.inbound_message_id, operation_key)
        if op_id in workspace.operations:
            raise ValueError('Este mensaje ya abrió una operación')
        await self.interrupt(context)
        # Options belong to the current preparation only; the pending selection is
        # consumed by prepare() so the handler can prove the draft uses that choice.
        context.state.pop('selection_options', None)
        operation = PendingOperation(operation_id=op_id, owner_id=context.turn.owner_id,
            conversation_id=context.turn.conversation_id, operation_key=operation_key, schema_version=contract.schema_version,
            source_message_id=context.turn.inbound_message_id, original_text=context.turn.source_text,
            reference_day=context.turn.reference_day, status='collecting', draft_payload=draft.model_dump(mode='json'),
            field_evidence=self.evidence(context, draft.model_dump(mode='json', exclude_unset=True)),
            created_at=context.turn.now, updated_at=context.turn.now,
            expires_at=context.turn.now + timedelta(seconds=self.operation_ttl_seconds))
        operation = await self.prepare(context, operation)
        context.state.pop('selected_ref', None)
        self.update(context, self.workspace(context), operation)
        return await self.present(context, operation)

    async def amend(self, context, ref, *, expected_revision, changes):
        operation = await self.resolve(context, ref, expected_revision)
        contract = self.registry.get_operation(operation.operation_key)
        if set(changes) - set(contract.draft_model.model_fields):
            raise ValueError('El cambio contiene campos desconocidos')
        draft = contract.draft_model.model_validate(merge_payload(operation.draft_payload, changes))
        canonical_changes = {key: draft.model_dump(mode='json')[key] for key in changes}
        def explicit_fields(patch, prefix=''):
            paths = []
            for key, value in patch.items():
                path = prefix + key
                if isinstance(value, dict):
                    paths.extend(explicit_fields(value, path + '.'))
                elif not isinstance(value, list):
                    paths.append(path)
            return paths
        # Validate all changes before invalidating any live proposal.
        await self.interrupt(context, except_id=operation.operation_id)
        context.state.pop('selection_options', None)
        operation = revised(operation, revision=operation.revision + 1, status='collecting', resume_status=None,
            draft_payload=draft.model_dump(mode='json'), prepared_payload=None, prepared_hash=None,
            field_evidence={**operation.field_evidence, **self.evidence(context, canonical_changes, operation.draft_payload,
                explicit=explicit_fields(changes))}, updated_at=context.turn.now)
        operation = await self.prepare(context, operation)
        context.state.pop('selected_ref', None)
        self.update(context, self.workspace(context), operation)
        return await self.present(context, operation)

    async def interrupt(self, context, except_id=None):
        await self.expire(context)
        workspace = self.workspace(context)
        op_id = workspace.active_operation_id
        if op_id and op_id != except_id:
            operation = workspace.operations[op_id]
            operation = revised(operation, status='suspended', resume_status=operation.status,
                revision=operation.revision + 1, updated_at=context.turn.now)
            self.update(context, workspace, operation)
            context.state.pop('selection_options', None)
            context.state.pop('selected_ref', None)

    async def suspend(self, context, ref, *, expected_revision):
        operation = await self.resolve(context, ref, expected_revision)
        if operation.status == 'suspended':
            raise ValueError('La operación ya está suspendida')
        await self.interrupt(context)
        return await self.view(context, self.workspace(context).operations[operation.operation_id])

    async def resume(self, context, ref, *, expected_revision):
        operation = await self.resolve(context, ref, expected_revision)
        if operation.status != 'suspended':
            raise ValueError('La operación ya está activa')
        await self.interrupt(context)
        context.state.pop('selection_options', None)
        context.state.pop('selected_ref', None)
        operation = revised(operation, status='collecting', resume_status=None, missing_fields=(),
            revision=operation.revision + 1, updated_at=context.turn.now)
        operation = await self.prepare(context, operation)
        self.update(context, self.workspace(context), operation)
        return await self.present(context, operation)

    async def cancel(self, context, ref, *, expected_revision):
        operation = await self.resolve(context, ref, expected_revision)
        operation = revised(operation, status='cancelled', resume_status=None,
            revision=operation.revision + 1, updated_at=context.turn.now)
        self.update(context, self.workspace(context), operation)
        context.state.pop('selection_options', None)
        context.state.pop('selected_ref', None)
        if operation.parent_operation_id:
            parent = self.workspace(context).operations.get(operation.parent_operation_id)
            if parent and parent.status == 'suspended':
                parent_view = await self.view(context, parent)
                await self.resume(context, parent_view.ref, expected_revision=parent.revision)
        return await self.view(context, operation)

    async def present(self, context, operation):
        view = await self.view(context, operation)
        if operation.status == 'ready':
            contract = self.registry.get_operation(operation.operation_key)
            if not contract.requires_confirmation:
                result = await contract.handler.commit(context, operation, None)
                context.presenter.include(result.facts)
                context.event_key, context.event_variables = result.event_key, result.event_variables
                context.terminal = context.presenter.render() or result.data.get('presentation', '')
                operation = revised(operation, status='committed', revision=operation.revision + 1,
                    commit_result=result.model_dump(mode='json'), updated_at=context.turn.now)
                self.update(context, self.workspace(context), operation)
                return await self.view(context, operation)
            summary = context.state['operation_summaries'][operation.operation_id]
            context.preview_text = summary + ' ¿Confirmás esta propuesta? Respondé sí o cancelar. Todavía no hice cambios.'
            context.preview = PreviewDelivery(operation_ref=view.ref, revision=operation.revision,
                prepared_hash=operation.prepared_hash, source_message_id=context.turn.inbound_message_id)
            context.terminal = '\n'.join(filter(None, [context.presenter.render(), context.preview_text]))
        else:
            options = context.state.get('selection_options', {})
            questions = list(dict.fromkeys(field.question or 'Me falta un dato para completar la propuesta. Indicá cómo querés completarla.'
                                         for field in operation.missing_fields))
            context.terminal = '\n'.join(filter(None, [context.presenter.render(), *questions]))
            if options:
                context.terminal += '\n' + '\n'.join(f"{n}. {item['label']}" for n, item in options.items())
        return view

    async def mark_preview_delivered(self, context, ref, *, expected_revision, preview_source_message_id, prepared_hash):
        operation = await self.resolve(context, ref, expected_revision)
        workspace = self.workspace(context)
        if (workspace.active_operation_id != operation.operation_id or operation.status != 'ready'
                or not hmac.compare_digest(operation.prepared_hash, prepared_hash)):
            raise ValueError('La entrega corresponde a una propuesta anterior')
        pointer = ConfirmationPointer(operation_id=operation.operation_id, revision=operation.revision,
            prepared_hash=operation.prepared_hash, preview_source_message_id=preview_source_message_id,
            expires_at=operation.expires_at)
        self.update(context, workspace, operation, pointer=pointer)

    async def confirm(self, context, request):
        # Lookup and identity validation happen while the caller holds the same owner claim.
        if request.confirmation_message_id != context.turn.inbound_message_id:
            raise ValueError('La confirmación no corresponde a este mensaje')
        prior = await self.registry.receipt(context.turn.owner_id, op_id=request.operation_id)
        if prior:
            revision = prior.get('revision', 1)
            if revision != request.revision or not hmac.compare_digest(prior['version_token'], request.short_token):
                raise ValueError('La confirmación no corresponde al recibo')
            return CommitResult(replayed=True, data=prior, facts=())
        await self.expire(context)
        workspace = self.workspace(context)
        pointer = workspace.last_confirmation
        operation = workspace.operations.get(request.operation_id)
        if (operation and operation.status == 'committed' and operation.commit_result
                and (operation.committed_revision or operation.revision - 1) == request.revision and operation.prepared_hash
                and hmac.compare_digest(short_token(request.revision, operation.prepared_hash), request.short_token)):
            return CommitResult.model_validate({**operation.commit_result, 'replayed': True})
        if (not pointer or not operation or operation.status != 'ready'
                or pointer.operation_id != request.operation_id or pointer.revision != request.revision
                or request.confirmation_message_id in {operation.source_message_id, pointer.preview_source_message_id}
                or not hmac.compare_digest(short_token(operation.revision, operation.prepared_hash), request.short_token)):
            raise ValueError('No hay una propuesta entregada y vigente para confirmar; retomá y revisá la operación')
        confirmation = ConfirmedAction(owner_id=context.turn.owner_id, operation_id=operation.operation_id,
            revision=operation.revision, prepared_hash=operation.prepared_hash,
            confirmation_message_id=request.confirmation_message_id)
        contract = self.registry.get_operation(operation.operation_key)
        checked = await contract.handler.prepare(context, operation)
        if not isinstance(checked, PreparedOperation):
            raise ValueError('La propuesta necesita una nueva aclaración antes de confirmar')
        payload = contract.prepared_model.model_validate(checked.payload).model_dump(mode='json')
        if digest(operation.operation_key, payload) != operation.prepared_hash:
            raise ValueError('stale_context: cambió la propuesta; revisá una nueva preview')
        result = await contract.handler.commit(context, operation, confirmation)
        workspace = self.workspace(context)
        operation = workspace.operations[operation.operation_id]
        operation = revised(operation, status='committed', resume_status=None, revision=operation.revision + 1,
                            committed_revision=request.revision,
                            commit_result=result.model_dump(mode='json'), updated_at=context.turn.now)
        self.update(context, workspace, operation)
        return result
