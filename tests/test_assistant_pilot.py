"""Offline pilot acceptance: real domain services, synthetic provider and delivery."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.models.database import AssistantWriteReceipt, Billetera, Categoria, MovimientoFinanciero, Usuario
from app.services import assistant_state
from app.services.assistant_contracts import (
    ComparePeriods, CorrectMovementDate, DateSpec, EntityReference, FindMovementsInput,
    MovementSnapshot, PendingCommand, RegisterMovement,
)
from app.services.assistant_capabilities import movement_effects
from app.services.assistant_capabilities.movements import find_movements
from app.services.assistant_runtime import make_context, record_preview_delivered, run_for_phone
from app.services.llm_providers.tools import ProviderContinuation, ToolCall, ToolTurn
from app.services.movement_commands import commit_command, digest, operation_id, snapshot
from app.services.period_insights import compare_periods

PHONE = '5491100260001'
NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


@pytest.fixture
def accounts(isolated_db):
    owner, other = uuid4(), uuid4()
    wallet, other_wallet, usd, category = uuid4(), uuid4(), uuid4(), uuid4()
    with isolated_db() as db:
        db.add_all([Usuario(id=owner, nombre='A', email='a@example.invalid', whatsapp_id=PHONE),
            Usuario(id=other, nombre='B', email='b@example.invalid', whatsapp_id='5491100260002'),
            Billetera(id=wallet, usuario_id=owner, nombre='Caja A', moneda='ARS'),
            Billetera(id=usd, usuario_id=owner, nombre='USD A', moneda='USD'),
            Billetera(id=other_wallet, usuario_id=other, nombre='Caja privada B', moneda='ARS'),
            Categoria(id=category, usuario_id=owner, nombre='Comida', es_default=False)])
        for i in range(8):
            db.add(MovimientoFinanciero(id=uuid4(), usuario_id=owner, billetera_id=wallet,
                categoria_id=category, tipo='egreso', cantidad=Decimal(100 + i), moneda='ARS',
                descripcion=f'gasto {i}', fecha_movimiento=date(2026, 10, 10 + i)))
        db.add(MovimientoFinanciero(id=uuid4(), usuario_id=owner, billetera_id=wallet,
            categoria_id=category, tipo='egreso', cantidad=Decimal('10'), moneda='ARS',
            descripcion='base', fecha_movimiento=date(2026, 9, 10)))
        db.add(MovimientoFinanciero(id=uuid4(), usuario_id=other, billetera_id=other_wallet,
            tipo='egreso', cantidad=Decimal('999999'), moneda='ARS', descripcion='privado', fecha_movimiento=date(2026, 10, 15)))
        db.commit()
    return isolated_db, owner, other, wallet, category


class NativeScript:
    def __init__(self, steps):
        self.steps, self.calls = iter(steps), 0

    async def generate_with_tools(self, **kwargs):
        self.calls += 1
        step = next(self.steps)
        if callable(step):
            step = step(kwargs)
        return ToolTurn([ToolCall(f'call-{self.calls}-{i}', name, args) for i, (name, args) in enumerate(step)],
            ProviderContinuation('fake', 'test', kwargs['policy'].scope, []))


def present_all(kwargs):
    # Required facts need no model-side ID copying.
    return [('present_result', {})]


def register_args():
    return {'economic_effect': 'cash_flow', 'movement_type': 'egreso', 'amount': '106', 'currency': 'ARS', 'description': 'taxi',
            'date_spec': {'kind': 'literal', 'evidence_span': '16/09/2026'}}


def context_for(owner, *, state=None, text='detalle', message_id='context', now=NOW, phone=PHONE):
    return make_context(owner, phone, message_id, text, now, state or assistant_state.empty(owner))


def current_operation(state):
    workspace = state['workspace']
    active = workspace['active_operation_id']
    return workspace['operations'][active] if active else next(reversed(workspace['operations'].values()))


def amend_active(changes):
    def step(kwargs):
        content = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'user'))
        operations = content['context']['operations']
        operation = next((item for item in operations if item['status'] != 'suspended'), operations[-1])
        return [('operation_amend', {'ref': operation['ref'], 'expected_revision': operation['revision'], 'changes': changes})]
    return step


def resume_last(kwargs):
    content = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'user'))
    operation = content['context']['operations'][-1]
    return [('operation_resume', {'ref': operation['ref'], 'expected_revision': operation['revision']})]


async def proposal(message_id='register-1', *, args=None, text='Gasté 106 en taxi el 16/09/2026', now=NOW):
    return await run_for_phone(PHONE, message_id, text,
        provider=NativeScript([[('register_movement', args or register_args())]]), now=now)


async def deliver(reply, *, now=NOW):
    assert reply.preview_delivery is not None
    await record_preview_delivered(PHONE, reply.preview_delivery, now=now)


async def confirm(reply, message_id='confirmation-1', *, now=NOW):
    await deliver(reply, now=now)
    return await run_for_phone(PHONE, message_id, action=reply.confirmation_action, now=now)


@pytest.mark.parametrize('token', ['16/09/08', '09/08'])
def test_date_literal_never_matches_partial_year(token):
    with pytest.raises(ValueError):
        DateSpec(kind='literal', evidence_span=token).resolve('Gasté 1 el 16/09/08', NOW.date())


def test_date_contract_and_money_are_strict():
    with pytest.raises(ValidationError):
        DateSpec(kind='literal', evidence_span='16/09', day_offset=1)
    with pytest.raises(ValidationError):
        RegisterMovement.model_validate({**register_args(), 'user_id': str(uuid4())})
    with pytest.raises(ValidationError):
        RegisterMovement.model_validate({**register_args(), 'amount': '0'})
    assert DateSpec(kind='literal', evidence_span='16/09').resolve('el 16/09', NOW.date()) == date(2026, 9, 16)


def test_complete_aggregates_use_more_than_five_and_isolate(accounts):
    factory, owner, *_ = accounts
    with factory() as db:
        result, blocks = compare_periods(db, owner, ComparePeriods(), date(2026, 10, 28))
    ars = result['currencies'][0]
    assert Decimal(ars['totals']['current']['egreso']) == 828
    assert Decimal(ars['expense_delta']) == sum(Decimal(x['delta']) for x in ars['contributions']) == 818
    assert '999999' not in str(result) and any('parcial' in x for x in blocks)


@pytest.mark.asyncio
async def test_native_read_loop_and_protected_values(accounts):
    provider = NativeScript([[('wallet_balances', {})], present_all])
    result = await run_for_phone(PHONE, 'read-1', 'mis saldos', provider=provider, now=NOW)
    assert 'ARS -838,00' in result.text and 'Caja privada B' not in result.text and provider.calls == 2


@pytest.mark.asyncio
async def test_invalid_month_batch_returns_feedback_without_partial_write(accounts):
    def corrected(kwargs):
        feedback = [json.loads(m.content) for m in kwargs['messages'] if m.role == 'tool']
        assert [item['status'] for item in feedback] == ['invalid_arguments', 'batch_not_executed']
        assert feedback[0]['errors'][0]['field'] == 'current_month'
        with accounts[0]() as db:
            assert db.query(AssistantWriteReceipt).count() == 0 and db.query(MovimientoFinanciero).count() == 10
        return [('compare_periods', {'current_month': '2026-10'})]
    provider = NativeScript([[('compare_periods', {'current_month': '2026-10-28'}), ('register_movement', register_args())], corrected, present_all])
    reply = await run_for_phone(PHONE, 'invalid-month', 'Compará octubre con septiembre', provider=provider, now=NOW)
    assert provider.calls == 3 and '828,00' in reply.text and reply.confirmation_action is None
    assert not (await assistant_state.read(accounts[1]))['workspace']['operations']


@pytest.mark.asyncio
async def test_required_facts_are_rendered_without_copying_refs(accounts):
    provider = NativeScript([[('wallet_balances', {})], [('present_result', {'block_refs': []})]])
    reply = await run_for_phone(PHONE, 'presentation', 'mis saldos', provider=provider, now=NOW)
    assert provider.calls == 2 and 'Caja A' in reply.text and 'USD A' in reply.text and 'No pude' not in reply.text


@pytest.mark.asyncio
async def test_model_context_exposes_handles_without_canonical_ids(accounts):
    _, owner, _, wallet_id, category_id = accounts
    context = context_for(owner, text='fecha 16/09/2026')
    initial = await context.initial_context()
    found = await find_movements(context, FindMovementsInput(selector={'kind': 'all'}, currency='ARS', limit=1))
    ref = found.data['items'][0]['movement_ref']
    internal = context.state['references'][ref]['payload']
    await context.operations.open(context, operation_key='correct_movement_date', draft=CorrectMovementDate(
        movement_ref=ref, date_spec=DateSpec(kind='literal', evidence_span='16/09/2026')))
    visible = json.dumps([initial, found.model_dump(mode='json'), await context.initial_context()])
    for canonical in (str(owner), str(wallet_id), str(category_id), internal['id']):
        assert canonical not in visible
    assert ref in visible and current_operation(context.state)['prepared_payload']['expected']['id'] == internal['id']


@pytest.mark.asyncio
async def test_registration_only_after_confirmation_and_replay(accounts):
    factory, *_ = accounts
    preview = await proposal()
    assert preview.confirmation_action and '106,00' in preview.text and '16/09/2026' in preview.text
    with factory() as db:
        assert db.query(AssistantWriteReceipt).count() == 0 and db.query(MovimientoFinanciero).count() == 10
    result = await confirm(preview)
    replay = await run_for_phone(PHONE, 'confirmation-1', action=preview.confirmation_action, now=NOW)
    assert result.committed and replay.replayed and replay.text == result.text
    with factory() as db:
        assert db.query(AssistantWriteReceipt).count() == 1 and db.query(MovimientoFinanciero).count() == 11
        assert db.query(MovimientoFinanciero).filter_by(whatsapp_message_id='register-1').one().fecha_movimiento == date(2026, 9, 16)


@pytest.mark.asyncio
async def test_cancel_and_repeated_yes_do_not_write(accounts):
    await proposal('reg-cancel')
    answer = await run_for_phone(PHONE, 'cancel', 'cancelar', now=NOW)
    assert 'Cancelé' in answer.text
    assert 'No hay' in (await run_for_phone(PHONE, 'yes-after-cancel', 'sí', now=NOW)).text
    with accounts[0]() as db:
        assert db.query(AssistantWriteReceipt).count() == 0


@pytest.mark.asyncio
async def test_two_writes_are_rejected_before_proposal(accounts):
    result = await run_for_phone(PHONE, 'multi', 'dos gastos el 16/09/2026',
        provider=NativeScript([[('register_movement', register_args()), ('register_movement', register_args())]]), now=NOW)
    assert 'separado' in result.text and not (await assistant_state.read(accounts[1]))['workspace']['operations']


@pytest.mark.asyncio
async def test_new_request_suspends_without_replacing_pending_operation(accounts):
    _, owner, *_ = accounts
    first = await proposal('original-pending')
    before = current_operation(await assistant_state.read(owner))
    second = await proposal('new-request')
    state = await assistant_state.read(owner)
    old = state['workspace']['operations'][before['operation_id']]
    assert old['status'] == 'suspended' and old['source_message_id'] == 'original-pending'
    assert old['prepared_payload'] == before['prepared_payload'] and len(state['workspace']['operations']) == 2
    assert first.confirmation_action != second.confirmation_action


@pytest.mark.asyncio
async def test_missing_amount_clarification_keeps_original_clock_and_message(accounts):
    partial = register_args()
    partial.pop('amount')
    partial['date_spec'] = {'kind': 'absent'}
    started = NOW + timedelta(hours=2, minutes=55)
    question = await proposal('amount-request', args=partial, text='Gasté en taxi', now=started)
    assert 'importe' in question.text and question.confirmation_action is None
    later = started + timedelta(minutes=10)
    revised_reply = await run_for_phone(PHONE, 'amount-reply', '106', provider=NativeScript([amend_active({'amount': '106'})]), now=later)
    pending = current_operation(await assistant_state.read(accounts[1]))
    assert revised_reply.confirmation_action and pending['source_message_id'] == 'amount-request'
    assert pending['prepared_payload']['date'] == '2026-10-27'
    assert pending['field_evidence']['amount']['source_message_id'] == 'amount-reply'


def make_pending(owner, wallet, *, target=None):
    payload = {'economic_effect': 'cash_flow', 'movement_type': 'egreso', 'amount': '5', 'currency': 'ARS', 'description': 'nuevo',
               'wallet_id': str(wallet), 'category_id': None, 'date': '2026-09-16'}
    kind = 'register'
    if target:
        kind, payload = 'correct_date', {'movement_id': target['id'], 'expected': target, 'date': '2026-09-16'}
    return PendingCommand(owner_id=str(owner), operation_id=operation_id(owner, 'source-test'),
        source_message_id='source-test', original_text='Gasté 5', reference_date='2026-10-28', command_kind=kind,
        payload=payload, command_hash=digest(kind, payload), expires_at='2099-01-01T00:00:00+00:00')


def test_correct_date_preserves_every_other_field(accounts):
    factory, owner, _, wallet, _ = accounts
    with factory() as db:
        before = snapshot(db.query(MovimientoFinanciero).filter_by(usuario_id=owner).first())
    after = commit_command(factory, owner, PHONE, make_pending(owner, wallet, target=before), 'confirm-date')['movement']
    assert after['date'] == '2026-09-16'
    assert {k: v for k, v in before.items() if k != 'date'} == {k: v for k, v in after.items() if k != 'date'}


def test_receipt_failure_rolls_back_financial_mutation(accounts, monkeypatch):
    from sqlalchemy.orm import Session
    factory, owner, _, wallet, _ = accounts
    original = Session.flush
    def fail_receipt(self, *args, **kwargs):
        if any(isinstance(item, AssistantWriteReceipt) for item in self.new):
            raise RuntimeError('synthetic receipt failure')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Session, 'flush', fail_receipt)
    with pytest.raises(RuntimeError):
        commit_command(factory, owner, PHONE, make_pending(owner, wallet), 'confirm-fail')
    with factory() as db:
        assert db.query(MovimientoFinanciero).filter_by(whatsapp_message_id='source-test').count() == 0


@pytest.mark.asyncio
async def test_largest_expense_is_queried_by_type_currency_and_uncategorized(accounts):
    factory, owner, _, wallet, _ = accounts
    with factory() as db:
        usd = db.query(Billetera).filter_by(usuario_id=owner, moneda='USD').one()
        db.add_all([MovimientoFinanciero(id=uuid4(), usuario_id=owner, billetera_id=wallet, tipo='ingreso', cantidad=Decimal('999999'), moneda='ARS', descripcion='income', fecha_movimiento=date(2026, 10, 20)),
            MovimientoFinanciero(id=uuid4(), usuario_id=owner, billetera_id=wallet, tipo='egreso', cantidad=Decimal('1000'), moneda='ARS', descripcion='large older expense', fecha_movimiento=date(2026, 10, 1)),
            MovimientoFinanciero(id=uuid4(), usuario_id=owner, billetera_id=usd.id, tipo='egreso', cantidad=Decimal('10000'), moneda='USD', descripcion='USD expense', fecha_movimiento=date(2026, 10, 2))])
        db.commit()
    context = context_for(owner)
    uncategorized = await context.reference('category', EntityReference(id=None, label='Sin categoría'))
    with pytest.raises(ValueError, match='moneda'):
        await find_movements(context, FindMovementsInput(selector={'kind': 'all'}, sort_by='amount_desc', movement_type='egreso'))
    result = await find_movements(context, FindMovementsInput(selector={'kind': 'all'}, sort_by='amount_desc', movement_type='egreso', currency='ARS', category_ref=uncategorized, limit=1))
    assert result.data['items'][0]['description'] == 'large older expense' and result.data['total_found'] == 1


@pytest.mark.asyncio
async def test_stale_snapshot_and_foreign_reference_do_not_mutate(accounts):
    factory, owner, other, wallet, _ = accounts
    context = context_for(owner)
    with factory() as db:
        row = db.query(MovimientoFinanciero).filter_by(usuario_id=owner).first()
        before = snapshot(row)
        row.descripcion = 'changed elsewhere'
        db.commit()
    with pytest.raises(ValueError, match='stale_context'):
        commit_command(factory, owner, PHONE, make_pending(owner, wallet, target=before), 'stale-confirm')
    ref = await context.reference('movement', MovementSnapshot.model_validate(before))
    foreign = context_for(other, state=context.state, phone='5491100260002')
    with pytest.raises(ValueError, match='referencia'):
        await foreign.references.resolve(foreign.turn, ref, kind='movement', payload_model=MovementSnapshot)


@pytest.mark.asyncio
async def test_redis_failure_allows_read_but_never_a_proposal(accounts, monkeypatch):
    async def unavailable():
        raise RuntimeError('synthetic Redis outage')
    monkeypatch.setattr(assistant_state, 'client', unavailable)
    read = await run_for_phone(PHONE, 'outage-read', 'saldos', provider=NativeScript([[('wallet_balances', {})], present_all]), now=NOW)
    assert 'Caja A' in read.text
    proposed = await run_for_phone(PHONE, 'outage-write', 'Gasté 106 el 16/09/2026', provider=NativeScript([[('register_movement', register_args())], present_all]), now=NOW)
    assert proposed.confirmation_action is None and 'Sin contexto' in proposed.text


@pytest.mark.asyncio
@pytest.mark.parametrize('date_spec,text,expected', [
    ({'kind': 'literal', 'evidence_span': '16/09'}, 'Gasté 106 el 16/09', '16/09/2026'),
    ({'kind': 'literal', 'evidence_span': '15/11/2026'}, 'Gasté 106 el 15/11/2026', 'futura'),
])
async def test_year_and_future_always_preview_before_mutation(accounts, date_spec, text, expected):
    reply = await proposal('date-preview', args={**register_args(), 'date_spec': date_spec}, text=text)
    assert expected in reply.text and reply.confirmation_action
    with accounts[0]() as db:
        assert db.query(AssistantWriteReceipt).count() == 0


@pytest.mark.asyncio
async def test_dispatch_text_and_interactive_use_protected_executor(accounts, monkeypatch):
    from app.services import dispatcher
    from app.services.assistant_runtime import AssistantReply
    calls = []
    async def fake_run(phone, message_id, text='', **kwargs):
        calls.append((phone, message_id, text, kwargs.get('action'), kwargs.get('history')))
        return AssistantReply('ARS 106,00 — fecha 16/09/2026')
    async def no_legacy(*args, **kwargs):
        pytest.fail('Pilot should not use legacy model or configured presentation')
    monkeypatch.setattr(dispatcher, 'run_for_phone', fake_run)
    text = await dispatcher.process_incoming_message(PHONE, 'pedido', 'text-id', conversation_history=[{'role': 'user', 'content': 'visible'}])
    assert text.protected_presentation and calls[-1][-1] == [{'role': 'user', 'content': 'visible'}]
    button = await dispatcher.process_incoming_interactive_reply(sender_phone=PHONE, option_id='assistant:confirm:operation:1:0123456789abcdef', reply_type='button', whatsapp_message_id='button-id')
    assert button.protected_presentation and calls[-1][1] == 'button-id'



@pytest.mark.asyncio
async def test_ambiguous_movement_visible_choice_to_date_confirmation(accounts):
    factory, owner, *_ = accounts
    def ask_candidates(kwargs):
        data = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'tool'))['data']
        return [('ask_clarification', {'reason_code': 'ambiguous_reference', 'target_field': 'movement', 'candidate_refs': [x['movement_ref'] for x in data['items'][:2]]})]
    question = await run_for_phone(PHONE, 'ambiguous-source', 'Mostrá gastos', provider=NativeScript([[('find_movements', {'selector': {'kind': 'all'}, 'description': 'gasto', 'limit': 2})], ask_candidates]), now=NOW)
    assert '1. ' in question.text and '2. ' in question.text and 'gasto' in question.text
    state = await assistant_state.read(owner)
    chosen_ref = state['selection_options']['2']['ref']
    chosen_id = state['references'][chosen_ref]['payload']['id']
    def correct_selected(kwargs):
        content = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'user'))
        return [('correct_movement_date', {'movement_ref': content['context']['selected_ref'], 'date_spec': {'kind': 'absent'}})]
    preview = await run_for_phone(PHONE, 'ambiguous-choice', '2', provider=NativeScript([correct_selected]), now=NOW)
    result = await confirm(preview)
    assert result.committed['movement']['id'] == chosen_id
    with factory() as db:
        assert db.query(AssistantWriteReceipt).one().source_message_id == 'ambiguous-choice'


@pytest.mark.asyncio
async def test_wallet_visible_choice_preserves_date_and_source(accounts):
    factory, owner, *_ = accounts
    second = uuid4()
    with factory() as db:
        db.add(Billetera(id=second, usuario_id=owner, nombre='Otra ARS', moneda='ARS'))
        db.commit()
    question = await proposal('wallet-source')
    assert 'Caja A' in question.text and 'Otra ARS' in question.text
    state = await assistant_state.read(owner)
    number = next(n for n, item in state['selection_options'].items() if item['label'] == 'Otra ARS')
    ref = state['selection_options'][number]['ref']
    preview = await run_for_phone(PHONE, 'wallet-choice', number, provider=NativeScript([amend_active({'wallet_ref': ref})]), now=NOW)
    result = await confirm(preview)
    assert result.committed['movement']['wallet_id'] == str(second) and result.committed['movement']['date'] == '2026-09-16'
    assert current_operation(await assistant_state.read(owner))['source_message_id'] == 'wallet-source'


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['text', 'interactive'])
async def test_budget_and_recurring_feedback_once_for_confirmation(accounts, monkeypatch, entry):
    from app.services import dispatcher
    from app.services.assistant_runtime import AssistantReply
    from app.services.budget import BudgetEvaluation
    factory, owner, *_ = accounts
    with factory() as db:
        row = snapshot(db.query(MovimientoFinanciero).filter_by(usuario_id=owner).first())
    reply = AssistantReply('Registré ARS 106,00', committed={'movement': row, 'command_kind': 'register', 'owner_id': str(owner), 'patron_hash': 'synthetic'})
    async def fake_run(*args, **kwargs):
        return reply
    counts = {'alerts': 0, 'recurring': 0}
    async def alerts(*args):
        counts['alerts'] += 1
    def recurring(*args):
        counts['recurring'] += 1
    monkeypatch.setattr(dispatcher, 'run_for_phone', fake_run)
    monkeypatch.setattr(movement_effects.BudgetService, 'evaluate_movement', lambda _: BudgetEvaluation('not_applicable', ''))
    monkeypatch.setattr(movement_effects, 'append_budget_threshold_alerts', alerts)
    monkeypatch.setattr(movement_effects, 'check_candidate_proposal', recurring)
    async def process(message_id):
        if entry == 'text':
            return await dispatcher.process_incoming_message(PHONE, 'sí', message_id)
        return await dispatcher.process_incoming_interactive_reply(sender_phone=PHONE, option_id='assistant:confirm:operation:1:0123456789abcdef', reply_type='button', whatsapp_message_id=message_id)
    await process('feedback-original')
    assert counts == {'alerts': 1, 'recurring': 1}
    reply.replayed = True
    await process('feedback-retry')
    assert counts == {'alerts': 1, 'recurring': 1}


@pytest.mark.asyncio
async def test_edit_requires_new_preview_and_invalidates_old_confirmation(accounts):
    first = await proposal('edit-original')
    await deliver(first)
    await run_for_phone(PHONE, 'edit-control', 'modificar', now=NOW)
    blocked = await run_for_phone(PHONE, 'edit-old-confirm', action=first.confirmation_action, now=NOW)
    assert blocked.committed is None
    newer = await run_for_phone(PHONE, 'edit-new-date', 'Cambiá la fecha al 17/09/2026', provider=NativeScript([amend_active({'date_spec': {'kind': 'literal', 'evidence_span': '17/09/2026'}})]), now=NOW)
    assert newer.confirmation_action != first.confirmation_action
    assert (await run_for_phone(PHONE, 'edit-stale', action=first.confirmation_action, now=NOW)).committed is None
    pending = current_operation(await assistant_state.read(accounts[1]))
    assert pending['source_message_id'] == 'edit-original' and pending['prepared_payload']['date'] == '2026-09-17'


@pytest.mark.asyncio
async def test_invalid_date_clarification_preserves_original_request(accounts):
    question = await proposal('invalid-original', args={**register_args(), 'date_spec': {'kind': 'literal', 'evidence_span': '31/02/2026'}}, text='Gasté 106 el 31/02/2026')
    assert question.confirmation_action is None and 'fecha' in question.text
    newer = await run_for_phone(PHONE, 'valid-date-answer', '16/09/2026', provider=NativeScript([amend_active({'date_spec': {'kind': 'literal', 'evidence_span': '16/09/2026'}})]), now=NOW)
    assert newer.confirmation_action and current_operation(await assistant_state.read(accounts[1]))['source_message_id'] == 'invalid-original'


@pytest.mark.asyncio
async def test_category_number_selection_uses_own_existing_category(accounts):
    factory, owner, _, _, category = accounts
    question = await run_for_phone(PHONE, 'category-original', 'Qué categorías tengo', provider=NativeScript([[('ask_clarification', {'reason_code': 'missing_field', 'target_field': 'category'})]]), now=NOW)
    assert 'Comida' in question.text
    state = await assistant_state.read(owner)
    number = next(n for n, item in state['selection_options'].items() if item['label'] == 'Comida')
    def selected_category(kwargs):
        content = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'user'))
        return [('register_movement', {**register_args(), 'category_ref': content['context']['selected_ref'], 'date_spec': {'kind': 'absent'}})]
    preview = await run_for_phone(PHONE, 'category-answer', number, provider=NativeScript([selected_category]), now=NOW)
    result = await confirm(preview)
    with factory() as db:
        assert db.get(MovimientoFinanciero, UUID(result.committed['movement']['id'])).categoria_id == category


@pytest.mark.asyncio
async def test_long_preview_keeps_full_text_and_short_confirmation_buttons(accounts, monkeypatch):
    from app.api.whatsapp import WhatsAppReplyButtons, WhatsAppText
    from app.services import dispatcher
    from app.services.assistant_runtime import AssistantReply
    text = 'Propuesta ' + 'x' * 1300
    async def fake_run(*args, **kwargs):
        return AssistantReply(text, confirmation_action='assistant:confirm:operation:1:0123456789abcdef')
    monkeypatch.setattr(dispatcher, 'run_for_phone', fake_run)
    result = await dispatcher.process_incoming_message(PHONE, 'long-preview', 'pedido')
    assert isinstance(result.reply_message, WhatsAppText) and result.reply_message.body == text
    assert isinstance(result.followup_messages[0], WhatsAppReplyButtons) and len(result.followup_messages[0].body) <= 1024


@pytest.mark.asyncio
async def test_cancel_after_failed_selection_does_not_bind_next_draft(accounts):
    factory, owner, *_ = accounts
    with factory() as db:
        db.add(Billetera(id=uuid4(), usuario_id=owner, nombre='Otra ARS', moneda='ARS'))
        db.commit()
    await proposal('selection-source')
    state = await assistant_state.read(owner)
    number = next(n for n, item in state['selection_options'].items() if item['label'] == 'Otra ARS')
    ref = state['selection_options'][number]['ref']
    await run_for_phone(PHONE, 'selection-answer', number, provider=NativeScript([amend_active({'wallet_ref': ref, 'date_spec': {'kind': 'literal', 'evidence_span': '31/02/2026'}})]), now=NOW)
    state = await assistant_state.read(owner)
    # A failed preparation consumes its selection: nothing binds the next draft.
    assert not state.get('selected_ref') and not state.get('selection_options')
    await run_for_phone(PHONE, 'selection-cancel', 'cancelar', now=NOW)
    state = await assistant_state.read(owner)
    assert not state.get('selected_ref') and not state.get('selection_options')
    def other_wallet(kwargs):
        data = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'user'))
        ref = next(wallet['ref'] for wallet in data['context']['wallets'] if wallet['name'] == 'Caja A')
        return [('register_movement', {**register_args(), 'wallet_ref': ref})]
    preview = await run_for_phone(PHONE, 'new-draft', 'Gasté 106 el 16/09/2026', provider=NativeScript([other_wallet]), now=NOW)
    assert preview.confirmation_action and 'Caja A' in preview.text
