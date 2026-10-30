"""Stage 3 acceptance: shared scopes, workspace transitions and transport boundary."""
from datetime import date, timedelta
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.database import AssistantWriteReceipt, Billetera, MovimientoFinanciero, Recordatorio
from app.services import assistant_runtime, assistant_state
from app.services.assistant_capabilities.insights import compare
from app.services.assistant_capabilities.movements import find_movements
from app.services.assistant_catalog import build_tool_registry
from app.services.assistant_contracts import (
    CommitResult, ComparePeriods, ConversationWorkspace, DataResult, DatePeriod, EmptyInput,
    FindMovementsInput, FrozenModel, MonthSelector, OperationResult,
    PreparedOperation, ScopePayload, ToolData,
)
from app.services.assistant_operations import revised
from app.services.assistant_registry import OperationContract, ToolContract, ToolPolicy, ToolRegistry
from app.services.assistant_runtime import run_for_phone
from app.services.conversation import ConversationHistoryService, ConversationStateUnavailable
from app.services.webhook_idempotency import process_text_message_once, process_interactive_message_once
from tests import test_assistant_pilot
from tests.test_assistant_pilot import (
    NOW, PHONE, NativeScript, amend_active, confirm, context_for, current_operation,
    deliver, present_all, proposal, register_args, resume_last,
)

accounts = test_assistant_pilot.accounts


@pytest.mark.parametrize('month,end', [('2024-02', '2024-02-29'), ('2026-02', '2026-02-28'), ('2026-12', '2026-12-31')])
def test_month_selector_civil_inclusive_limits(month, end):
    period = MonthSelector(month=month).period()
    assert period.start.isoformat() == month + '-01' and period.end.isoformat() == end
    assert period.half_open_dates()[1] == date.fromisoformat(end) + timedelta(days=1)
    with pytest.raises(ValidationError):
        DatePeriod(start=period.end, end=period.start - timedelta(days=1))


@pytest.mark.asyncio
async def test_comparison_scopes_drive_exact_search_without_reference_override(accounts):
    factory, owner, *_ = accounts
    context = context_for(owner, now=NOW + timedelta(hours=12))
    comparison = await compare(context, ComparePeriods(currency='ARS'))
    current = comparison.data['currencies'][0]['current']
    baseline = comparison.data['currencies'][0]['baseline']
    assert current['period']['end'] == '2026-10-28' and baseline['period']['end'] == '2026-09-28'
    with factory() as db:
        row = db.query(MovimientoFinanciero).filter_by(usuario_id=owner).first()
        db.add(MovimientoFinanciero(id=uuid4(), usuario_id=owner, billetera_id=row.billetera_id, tipo='egreso',
            cantidad=9999, moneda='ARS', descripcion='fuera del corte', fecha_movimiento=date(2026, 9, 29)))
        db.commit()
    found = await find_movements(context, FindMovementsInput(selector={'kind': 'scope', 'ref': baseline['ref']}, sort_by='amount_desc', limit=1))
    assert found.data['items'][0]['description'] == 'base'
    assert found.data['total_found'] == 1
    with pytest.raises(ValueError, match='currency'):
        await find_movements(context, FindMovementsInput(selector={'kind': 'scope', 'ref': current['ref']}, currency='USD'))
    with pytest.raises(ValueError, match='movement_type'):
        await find_movements(context, FindMovementsInput(selector={'kind': 'scope', 'ref': current['ref']}, movement_type='ingreso'))
    with pytest.raises(ValidationError):
        FindMovementsInput.model_validate({'selector': {'kind': 'scope', 'ref': current['ref']}, 'period_start': '2026-01-01', 'reference': 'last_registered'})


@pytest.mark.asyncio
async def test_reference_owner_conversation_kind_expiration_and_reuse(accounts):
    _, owner, other, *_ = accounts
    context = context_for(owner)
    payload = ScopePayload(period=DatePeriod(start=date(2026, 10, 1), end=date(2026, 10, 28)), currency='ARS')
    ref = await context.reference('query_scope', payload)
    assert await context.reference('query_scope', payload) == ref
    for changed in (
        context_for(other, state=context.state, phone='other'),
        context_for(owner, state=context.state, now=NOW + timedelta(minutes=31)),
    ):
        with pytest.raises(ValueError):
            await changed.references.resolve(changed.turn, ref, kind='query_scope', payload_model=ScopePayload)
    from dataclasses import replace
    context.turn = replace(context.turn, conversation_id='another')
    with pytest.raises(ValueError):
        await context.references.resolve(context.turn, ref, kind='query_scope', payload_model=ScopePayload)


@pytest.mark.asyncio
async def test_inclusive_period_and_month_search_include_last_day(accounts):
    factory, owner, _, wallet, _ = accounts
    with factory() as db:
        db.add_all([MovimientoFinanciero(id=uuid4(), usuario_id=owner, billetera_id=wallet, tipo='egreso', cantidad=1,
            moneda='ARS', descripcion=label, fecha_movimiento=day) for label, day in
            [('febrero', date(2024, 2, 29)), ('marzo', date(2024, 3, 1))]])
        db.commit()
    context = context_for(owner)
    for selector in ({'kind': 'month', 'month': '2024-02'}, {'kind': 'period', 'period': {'start': '2024-02-01', 'end': '2024-02-29'}}):
        found = await find_movements(context, FindMovementsInput(selector=selector))
        assert [item['description'] for item in found.data['items']] == ['febrero']


@pytest.mark.asyncio
async def test_read_interrupt_invalidates_yes_and_old_button_then_resume(accounts):
    first = await proposal()
    await deliver(first)
    operation = current_operation(await assistant_state.read(accounts[1]))
    answer = await run_for_phone(PHONE, 'interrupt', 'mis saldos', provider=NativeScript([[('wallet_balances', {})], present_all]), now=NOW)
    state = await assistant_state.read(accounts[1])
    pending = current_operation(state)
    assert 'Caja A' in answer.text and pending['status'] == 'suspended' and pending['resume_status'] == 'ready'
    assert pending['revision'] == operation['revision'] + 1 and pending['expires_at'] == operation['expires_at']
    assert state['workspace']['last_confirmation'] is None
    assert (await run_for_phone(PHONE, 'stale-yes', 'sí', now=NOW)).committed is None
    assert (await run_for_phone(PHONE, 'stale-button', action=first.confirmation_action, now=NOW)).committed is None
    resumed = await run_for_phone(PHONE, 'resume', 'retomá el gasto', provider=NativeScript([resume_last]), now=NOW)
    assert resumed.confirmation_action and resumed.confirmation_action != first.confirmation_action
    assert (await run_for_phone(PHONE, 'premature-yes', 'sí', now=NOW)).committed is None
    final = await confirm(resumed, 'final-confirm')
    assert final.committed


@pytest.mark.asyncio
async def test_preview_needs_real_delivery_and_rejects_self_confirmation(accounts):
    preview = await proposal()
    state = await assistant_state.read(accounts[1])
    assert state['workspace']['last_confirmation'] is None
    assert (await run_for_phone(PHONE, 'undelivered', action=preview.confirmation_action, now=NOW)).committed is None
    await deliver(preview)
    assert (await run_for_phone(PHONE, 'register-1', action=preview.confirmation_action, now=NOW)).committed is None
    assert (await run_for_phone(PHONE, 'later', 'sí', now=NOW)).committed


@pytest.mark.asyncio
async def test_stale_delivery_cannot_enable_new_revision(accounts):
    old = await proposal()
    newer = await run_for_phone(PHONE, 'amend', 'Cambiá a 107', provider=NativeScript([amend_active({'amount': '107'})]), now=NOW)
    with pytest.raises(ValueError):
        await deliver(old)
    assert (await assistant_state.read(accounts[1]))['workspace']['last_confirmation'] is None
    assert (await confirm(newer)).committed['movement']['amount'] == '107.0000000000'


@pytest.mark.asyncio
async def test_delivery_receipt_replay_after_workspace_disappears(accounts):
    preview = await proposal()
    original = await confirm(preview)
    await (await assistant_state.client()).delete(assistant_state.key(accounts[1]))
    replay = await run_for_phone(PHONE, 'other-confirmation-id', action=preview.confirmation_action, now=NOW + timedelta(hours=2))
    assert replay.replayed and replay.text == original.text
    with accounts[0]() as db:
        assert db.query(AssistantWriteReceipt).count() == 1 and db.query(MovimientoFinanciero).count() == 11


@pytest.mark.asyncio
async def test_expiry_is_not_extended_by_reads_or_resume(accounts):
    preview = await proposal()
    await deliver(preview)
    original = current_operation(await assistant_state.read(accounts[1]))
    later = NOW + timedelta(minutes=31)
    assert (await run_for_phone(PHONE, 'expired', action=preview.confirmation_action, now=later)).committed is None
    expired = current_operation(await assistant_state.read(accounts[1]))
    assert expired['status'] == 'expired' and expired['expires_at'] == original['expires_at']
    assert expired['revision'] == original['revision'] + 1


@pytest.mark.asyncio
async def test_workspace_budget_does_not_discard_drafts(accounts):
    for number in range(3):
        assert (await proposal(f'open-{number}')).confirmation_action
    rejected = await proposal('open-too-many')
    state = await assistant_state.read(accounts[1])
    assert not rejected.confirmation_action and 'demasiadas' in rejected.text
    assert len(state['workspace']['operations']) == 3
    assert sum(item['status'] == 'suspended' for item in state['workspace']['operations'].values()) == 2


@pytest.mark.asyncio
async def test_terminal_retention_preserves_suspended_draft_and_durable_replay(accounts):
    committed_preview = await proposal('retention-committed')
    committed = await confirm(committed_preview, 'retention-confirmation')
    await proposal('retention-live-draft')
    await run_for_phone(PHONE, 'retention-interrupt', 'saldos',
        provider=NativeScript([[('wallet_balances', {})], present_all]), now=NOW)
    before = current_operation(await assistant_state.read(accounts[1]))
    for number in range(6):
        await proposal(f'retention-closed-{number}')
        cancelled = await run_for_phone(PHONE, f'retention-cancel-{number}', 'cancelar', now=NOW)
        assert 'Cancelé' in cancelled.text
    state = await assistant_state.read(accounts[1])
    operations = state['workspace']['operations']
    assert len(operations) == 4
    assert sum(op['status'] == 'cancelled' for op in operations.values()) == 3
    assert operations[before['operation_id']] == before and before['status'] == 'suspended'
    assert state['workspace']['last_confirmation'] is None
    assert set(state['operation_summaries']) == set(operations)
    assert committed.committed['operation_id'] not in operations
    replay = await run_for_phone(PHONE, 'retention-replay', action=committed_preview.confirmation_action, now=NOW)
    assert replay.replayed and replay.text == committed.text
    with accounts[0]() as db:
        assert db.query(AssistantWriteReceipt).count() == 1
        assert db.query(MovimientoFinanciero).count() == 11


@pytest.mark.asyncio
async def test_date_amend_uses_new_turn_clock_preserving_resolved_date_on_resume(accounts):
    started = NOW + timedelta(hours=2, minutes=55)
    first = await proposal('date-original', args={**register_args(), 'date_spec': {'kind': 'relative_days', 'evidence_span': 'ayer', 'day_offset': -1}}, text='Gasté 106 ayer', now=started)
    assert '26/10/2026' in first.text
    await run_for_phone(PHONE, 'suspend', 'saldos', provider=NativeScript([[('wallet_balances', {})], present_all]), now=started)
    later = started + timedelta(minutes=10)
    resumed = await run_for_phone(PHONE, 'resume', 'retomá', provider=NativeScript([resume_last]), now=later)
    assert '26/10/2026' in resumed.text
    changed = await run_for_phone(PHONE, 'date-new', 'Cambiala a ayer', provider=NativeScript([amend_active({'date_spec': {'kind': 'relative_days', 'evidence_span': 'ayer', 'day_offset': -1}})]), now=later)
    assert '27/10/2026' in changed.text
    pending = current_operation(await assistant_state.read(accounts[1]))
    assert pending['source_message_id'] == 'date-original' and pending['reference_day'] == '2026-10-27'
    evidence = pending['field_evidence']['date_spec']
    assert evidence['reference_day'] == '2026-10-28' and evidence['source_message_id'] == 'date-new' and evidence['source_text'] == 'Cambiala a ayer'
    assert pending['field_evidence']['amount']['source_message_id'] == 'date-original'


@pytest.mark.asyncio
async def test_typed_amend_and_tampered_hash_workspace_are_revalidated(accounts):
    await proposal()
    state = await assistant_state.read(accounts[1])
    context = context_for(accounts[1], state=state)
    operation = context.operations.workspace(context).operations[state['workspace']['active_operation_id']]
    view = await context.operations.view(context, operation)
    for patch in ({'amount': '-5'}, {'unexpected': True}, {'date_spec': {'kind': 'literal', 'day_offset': 1}}):
        with pytest.raises(ValueError):
            await context.operations.amend(context, view.ref, expected_revision=operation.revision, changes=patch)
    invalid = operation.model_copy(update={'prepared_hash': 'tampered'})
    with pytest.raises(ValueError):
        context.operations.update(context, context.operations.workspace(context), invalid)
    with pytest.raises(ValidationError):
        revised(operation, status='suspended', resume_status=None)
    with pytest.raises(ValidationError):
        ConversationWorkspace.model_validate({**state['workspace'], 'active_operation_id': None})


@pytest.mark.asyncio
async def test_snapshot_revalidated_when_resuming_and_committing(accounts):
    def target(kwargs):
        data = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'tool'))['data']
        return [('correct_movement_date', {'movement_ref': data['items'][0]['movement_ref'], 'date_spec': {'kind': 'absent'}})]
    preview = await run_for_phone(PHONE, 'correction', 'Cambiá la fecha', provider=NativeScript([[('find_movements', {'selector': {'kind': 'all'}, 'limit': 1})], target]), now=NOW)
    await deliver(preview)
    operation = current_operation(await assistant_state.read(accounts[1]))
    with accounts[0]() as db:
        from uuid import UUID
        row = db.get(MovimientoFinanciero, UUID(operation['prepared_payload']['movement_id']))
        row.descripcion = 'changed elsewhere'
        db.commit()
    assert (await run_for_phone(PHONE, 'stale-confirm', action=preview.confirmation_action, now=NOW)).committed is None
    await run_for_phone(PHONE, 'interrupt', 'saldos', provider=NativeScript([[('wallet_balances', {})], present_all]), now=NOW)
    resumed = await run_for_phone(PHONE, 'resume-stale', 'retomá', provider=NativeScript([resume_last]), now=NOW)
    assert not resumed.confirmation_action and current_operation(await assistant_state.read(accounts[1]))['status'] == 'collecting'


@pytest.mark.asyncio
async def test_failed_state_save_after_commit_keeps_successful_receipt(accounts, monkeypatch):
    preview = await proposal()
    await deliver(preview)
    async def fail(*args, **kwargs):
        raise ConversationStateUnavailable('synthetic save failure')
    monkeypatch.setattr(assistant_state, 'save', fail)
    result = await run_for_phone(PHONE, 'durable-confirm', action=preview.confirmation_action, now=NOW)
    assert result.committed and 'Registré' in result.text and 'No hice cambios' not in result.text
    replay = await run_for_phone(PHONE, 'durable-confirm', action=preview.confirmation_action, now=NOW)
    assert replay.replayed and replay.text == result.text


@pytest.mark.asyncio
async def test_selection_does_not_leak_to_another_operation(accounts):
    factory, owner, *_ = accounts
    second = uuid4()
    with factory() as db:
        db.add(Billetera(id=second, usuario_id=owner, nombre='Otra ARS', moneda='ARS'))
        db.commit()
    await proposal('first')
    state = await assistant_state.read(owner)
    number = next(n for n, item in state['selection_options'].items() if item['label'] == 'Otra ARS')
    selected_ref = state['selection_options'][number]['ref']
    first = await run_for_phone(PHONE, 'choose', number, provider=NativeScript([amend_active({'wallet_ref': selected_ref})]), now=NOW)
    assert first.confirmation_action
    await run_for_phone(PHONE, 'read', 'saldos', provider=NativeScript([[('wallet_balances', {})], present_all]), now=NOW)
    def other(kwargs):
        data = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'user'))
        ref = next(wallet['ref'] for wallet in data['context']['wallets'] if wallet['name'] == 'Caja A')
        return [('register_movement', {**register_args(), 'wallet_ref': ref})]
    second_reply = await run_for_phone(PHONE, 'second', 'Gasté 106 el 16/09/2026', provider=NativeScript([other]), now=NOW)
    assert second_reply.confirmation_action and 'Caja A' in second_reply.text


@pytest.mark.asyncio
async def test_list_reminders_added_by_composition_and_owner_only(accounts):
    factory, owner, other, *_ = accounts
    with factory() as db:
        db.add_all([Recordatorio(id=uuid4(), usuario_id=owner, titulo='Internet', dia_del_mes=15, monto=1500, moneda='ARS', estado='activo'),
            Recordatorio(id=uuid4(), usuario_id=other, titulo='Privado B', dia_del_mes=12, estado='activo')])
        db.commit()
    reply = await run_for_phone(PHONE, 'reminders', 'mis recordatorios', provider=NativeScript([[('list_reminders', {})], present_all]), now=NOW)
    assert 'Internet' in reply.text and '1.500,00' in reply.text and 'Privado B' not in reply.text


@pytest.mark.asyncio
async def test_terminal_policy_and_additional_read_require_no_loop_changes(accounts, monkeypatch):
    registry = build_tool_registry()
    async def example(context, args):
        return DataResult(data=ToolData(status='ok').model_dump(mode='json'), facts=context.presenter.add(['Resultado extensible.']))
    registry.register(ToolContract('example_read', 'Test extension', EmptyInput, ToolData, example,
        ToolPolicy('read', ends_turn=True)))
    monkeypatch.setattr(assistant_runtime, 'build_tool_registry', lambda: registry)
    script = NativeScript([[('example_read', {})]])
    result = await run_for_phone(PHONE, 'extension', 'consulta', provider=script, now=NOW)
    assert script.calls == 1 and result.text == '• Resultado extensible.'


@pytest.mark.asyncio
async def test_nonfinancial_operation_uses_domain_contract_for_commit_presentation(accounts, monkeypatch):
    class Draft(FrozenModel):
        label: str
    class Handler:
        async def prepare(self, context, operation):
            return PreparedOperation(payload=operation.draft_payload, facts=(), preview='Propuesta de prueba.')
        async def commit(self, context, operation, confirmation):
            return CommitResult(replayed=False, data={'completed': True}, facts=context.presenter.add(['Operación de prueba completada.']))
    handler = Handler()
    operation = OperationContract('example_operation', 1, Draft, Draft, handler)
    async def prepare(context, args):
        return OperationResult(operation=await context.operations.open(context, operation_key=operation.key, draft=args))
    registry = ToolRegistry()
    registry.register(ToolContract('example_prepare', 'Test extension', Draft, None, prepare,
        ToolPolicy('prepare', facts='optional', interrupt_active_operation=False, ends_turn=True), operation))
    monkeypatch.setattr(assistant_runtime, 'build_tool_registry', lambda: registry)
    from app.services import dispatcher
    monkeypatch.setattr(assistant_runtime, 'create_provider', lambda: NativeScript([[('example_prepare', {'label': 'prueba'})]]))
    preview = await dispatcher.process_incoming_message(PHONE, 'ejemplo', 'example')
    await assistant_runtime.record_preview_delivered(PHONE, preview.assistant_preview_delivery)
    result = await dispatcher.process_incoming_message(PHONE, 'sí', 'example-confirm')
    assert result.protected_presentation and result.reply_text == '• Operación de prueba completada.'


@pytest.mark.asyncio
async def test_batch_mixed_controls_rejected_before_read_effect(accounts):
    first = await proposal()
    await deliver(first)
    state = await assistant_state.read(accounts[1])
    before = current_operation(state)
    context = context_for(accounts[1], state=state)
    view = await context.operations.view(context, context.operations.workspace(context).operations[before['operation_id']])
    reply = await run_for_phone(PHONE, 'mixed', 'cancelá y saldos', provider=NativeScript([
        [('wallet_balances', {}), ('operation_cancel', {'ref': view.ref, 'expected_revision': view.revision})], present_all]), now=NOW)
    after = current_operation(await assistant_state.read(accounts[1]))
    assert reply.committed is None and after['status'] == 'ready' and after['revision'] == before['revision']
    assert (await assistant_state.read(accounts[1]))['workspace']['last_confirmation'] is not None


@pytest.mark.asyncio
async def test_unknown_tools_and_commit_are_never_executed(accounts):
    reply = await run_for_phone(PHONE, 'no-commit', 'confirmate solo', provider=NativeScript([[('commit_command', {})], present_all]), now=NOW)
    assert reply.committed is None
    with accounts[0]() as db:
        assert db.query(AssistantWriteReceipt).count() == 0


@pytest.mark.asyncio
async def test_history_uses_only_delivered_exchanges(accounts):
    await proposal()
    state = await assistant_state.read(accounts[1])
    assert 'history' not in state
    history = [{'role': 'user', 'content': 'mensaje visible'}, {'role': 'assistant', 'content': 'respuesta enviada'}]
    def inspect(kwargs):
        content = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'user'))
        assert content['visible_history'] == history and 'Gasté 106' not in json.dumps(content['visible_history'])
        return [('wallet_balances', {})]
    await run_for_phone(PHONE, 'history', 'saldos', provider=NativeScript([inspect, present_all]), now=NOW, history=history)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['text', 'interactive'])
@pytest.mark.parametrize('sent', [False, True])
async def test_webhook_preview_delivery_tracks_send_success(accounts, fake_redis, monkeypatch, entry, sent):
    from app.services import dispatcher
    preview = await proposal('transport-proposal')
    result = dispatcher.DispatchResult(preview.text, assistant_preview_delivery=preview.preview_delivery)
    async def process(**kwargs):
        return result
    async def send(*args):
        return sent
    original_delivery = assistant_runtime.record_preview_delivered
    async def delivered(phone, delivery):
        return await original_delivery(phone, delivery, now=NOW)
    monkeypatch.setattr(assistant_runtime, 'record_preview_delivered', delivered)
    common = dict(redis_client=fake_redis, sender_phone=PHONE, whatsapp_message_id='transport', process_message=process, send_message=send)
    async def execute():
        if entry == 'text':
            return await process_text_message_once(**common, text_body='pedido')
        from types import SimpleNamespace
        return await process_interactive_message_once(redis_client=fake_redis,
            interactive_reply=SimpleNamespace(sender_phone=PHONE, message_id='transport',
                option_id='option', reply_type='button', title='pedido'),
            process_reply=process, send_message=send)
    if sent:
        assert await execute() == 'completed'
        assert (await assistant_state.read(accounts[1]))['workspace']['last_confirmation'] is not None
    else:
        with pytest.raises(RuntimeError):
            await execute()
        assert (await assistant_state.read(accounts[1]))['workspace']['last_confirmation'] is None
    history = await ConversationHistoryService.get_recent(fake_redis, PHONE)
    if entry == 'text':
        assert bool(history) == sent
