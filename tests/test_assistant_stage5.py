"""Global cut acceptance via real dispatcher, delivery and visible history (offline)."""
from datetime import date
from decimal import Decimal
import json
from uuid import uuid4

import pytest

from app.api.whatsapp import InboundInteractiveReply, WhatsAppReplyButtons, WhatsAppText
from app.models.database import AssistantWriteReceipt, Categoria, LimiteCategoria, MovimientoFinanciero, Recordatorio, Usuario
from app.services import assistant_runtime, assistant_state, dispatcher
from app.services import compensation, chart_query, conversation_flow, monthly_balance
from app.services.assistant_catalog import build_tool_registry
from app.services.conversation import ConversationHistoryService
from app.services.webhook_idempotency import process_interactive_message_once, process_text_message_once
from tests import test_assistant_pilot
from tests.test_assistant_pilot import NOW, PHONE, NativeScript, present_all, register_args
from tests.test_assistant_stage4 import initial

accounts = test_assistant_pilot.accounts


class Ingress:
    def __init__(self, redis, monkeypatch):
        self.redis, self.monkeypatch = redis, monkeypatch
        self.steps, self.sent, self.status, self.sequence = [], [], None, 0

    async def send(self, text='', *, steps=(), option_id=None, message_id=None, fail_send=False, phone=PHONE):
        self.sequence += 1
        message_id = message_id or f'stage5-{self.sequence}'
        self.steps = NativeScript(steps)
        self.monkeypatch.setattr(assistant_runtime, 'create_provider', lambda: self.steps)
        captured = None
        async def process_text(**kwargs):
            nonlocal captured
            captured = await dispatcher.process_incoming_message(**kwargs)
            return captured
        async def process_reply(**kwargs):
            nonlocal captured
            captured = await dispatcher.process_incoming_interactive_reply(**kwargs)
            return captured
        async def deliver_synthetically(phone, message):
            if fail_send:
                return False
            self.sent.append(message)
            return True
        if option_id:
            self.status = await process_interactive_message_once(redis_client=self.redis,
                interactive_reply=InboundInteractiveReply(message_id, phone, 'button_reply', option_id, text),
                process_reply=process_reply, send_message=deliver_synthetically)
        else:
            self.status = await process_text_message_once(redis_client=self.redis, sender_phone=phone,
                text_body=text, whatsapp_message_id=message_id, process_message=process_text,
                send_message=deliver_synthetically)
        return captured

    async def tool(self, name, args=None, text='pedido'):
        return await self.send(text, steps=[[(name, args or {})], present_all])

    async def confirm(self, preview, *, message_id=None):
        action = next(button.id for message in [preview.reply_message, *preview.followup_messages]
            if isinstance(message, WhatsAppReplyButtons) for button in message.buttons
            if button.id.startswith('assistant:confirm:'))
        return await self.send('Confirmar', option_id=action, message_id=message_id)


@pytest.fixture
def ingress(accounts, fake_redis, monkeypatch):
    factory, owner, *_ = accounts
    for module in (compensation, chart_query, conversation_flow, monthly_balance):
        monkeypatch.setattr(module, 'SessionLocal', factory)
    with factory() as session:
        session.get(Usuario, owner).auth_user_id = uuid4()
        session.commit()
    original = assistant_runtime.make_context
    monkeypatch.setattr(assistant_runtime, 'make_context', lambda owner, phone, message_id, text, now, state, **kw:
        original(owner, phone, message_id, text, now or NOW, state, **kw))
    for flag in ('LUKA_TOOLS_PILOT_ENABLED', 'LUKA_TOOLS_PILOT_PHONES', 'LUKA_TOOLS_PILOT_MODELS'):
        monkeypatch.delenv(flag, raising=False)
    return Ingress(fake_redis, monkeypatch)


def register_now(**patch):
    return {**register_args(), 'date_spec': {'kind': 'absent'}, **patch}


def visible_ref(kwargs, kind):
    return next(ref for ref, item in initial(kwargs)['references'].items() if item['kind'] == kind)


@pytest.mark.asyncio
@pytest.mark.parametrize('phone', [PHONE, '5491100260002'])
async def test_every_known_phone_enters_tools_without_pilot_flags(ingress, accounts, phone):
    result = await ingress.send('Hola', phone=phone, steps=[[('conversation_reply', {'kind': 'greeting'})]])
    assert result.service_invoked == 'assistant' and 'Hola' in result.reply_text
    assert result.tool_trace[0]['tool'] == 'conversation_reply'
    with accounts[0]() as session:
        assert session.query(Usuario).filter_by(whatsapp_id=phone).one().ultimo_mensaje_en is not None
    assert len(build_tool_registry().available(assistant_runtime.make_context(accounts[1], PHONE, 'catalog', '', NOW,
        assistant_state.empty(accounts[1])).turn)) == 43


@pytest.mark.asyncio
async def test_unknown_onboarding_precedes_tools_and_honors_cooldown(ingress):
    first = await ingress.send('Hola', phone='5491100999999')
    second = await ingress.send('Hola', phone='5491100999999')
    assert first.service_invoked == 'onboarding' and first.event_variables['registration_url']
    assert second.service_invoked == 'onboarding' and not second.reply_text
    assert ingress.steps.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected', [('help','billeteras'), ('out_of_scope','finanzas personales')])
async def test_help_and_out_of_scope_are_useful_without_classifier(ingress, kind, expected):
    result = await ingress.tool('conversation_reply', {'kind': kind})
    assert expected in result.reply_text and ingress.steps.calls == 1


@pytest.mark.asyncio
async def test_preview_delivery_confirm_replay_and_unique_visible_history(ingress, accounts):
    preview = await ingress.tool('register_movement', register_now(), 'Gasté 106 en taxi')
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count() == 0
    result = await ingress.confirm(preview, message_id='stage5-confirm')
    assert '106,00' in result.reply_text
    sent_before = len(ingress.sent)
    assert await ingress.confirm(preview, message_id='stage5-confirm') is None
    assert ingress.status == 'duplicate' and len(ingress.sent) == sent_before
    replay = await ingress.confirm(preview, message_id='stage5-new-button-event')
    assert replay.budget_threshold_alerts == [] and not replay.proposal_candidate_id
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count() == 1
    history = await ConversationHistoryService.get_recent(ingress.redis, PHONE)
    assert len(history) == 6 and 'Confirmar' in history[2].content


@pytest.mark.asyncio
async def test_failed_synthetic_delivery_cannot_authorize_mutation_or_history(ingress, accounts):
    with pytest.raises(RuntimeError):
        await ingress.send('Gasto', steps=[[('register_movement', register_now())]], fail_send=True)
    state = await assistant_state.read(accounts[1])
    operation = state['workspace']['operations'][state['workspace']['active_operation_id']]
    from app.services.assistant_operations import short_token
    old_button = f"assistant:confirm:{operation['operation_id']}:{operation['revision']}:{short_token(operation['revision'], operation['prepared_hash'])}"
    assert not await ConversationHistoryService.get_recent(ingress.redis, PHONE)
    rejected = await ingress.send('Confirmar', option_id=old_button)
    assert 'vigente' in rejected.reply_text
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count() == 0


@pytest.mark.asyncio
async def test_interruption_preserves_reminder_and_same_message_resume_can_complete(ingress, accounts):
    question = await ingress.tool('create_reminder', {'title': 'Internet', 'amount': '1500', 'currency': 'ARS'})
    assert 'día de vencimiento' in question.reply_text and 'day' not in question.reply_text
    read = await ingress.tool('list_categories')
    assert 'Comida' in read.reply_text
    blocked = await ingress.send('sí')
    assert 'vigente' in blocked.reply_text
    def resume(kwargs):
        op = initial(kwargs)['operations'][0]
        return [('operation_resume', {'ref': op['ref'], 'expected_revision': op['revision']})]
    def complete(kwargs):
        operation = json.loads(next(m.content for m in kwargs['messages'] if m.role=='tool'))['operation']
        return [('operation_amend', {'ref':operation['ref'], 'expected_revision':operation['revision'], 'changes':{'day':15}})]
    completed = await ingress.send('Retomá Internet y completalo para el día15', steps=[resume, complete])
    assert '1500' in completed.reply_text.replace('.', '').replace(',', '')
    await ingress.confirm(completed)
    paused = await ingress.send('Pausalo', steps=[lambda kw:[('change_reminder_state', {'reminder_ref': visible_ref(kw,'reminder'), 'state':'pause'})]])
    await ingress.confirm(paused)
    active = await ingress.send('Activalo', steps=[lambda kw:[('change_reminder_state', {'reminder_ref': visible_ref(kw,'reminder'), 'state':'activate'})]])
    await ingress.confirm(active)
    with accounts[0]() as session:
        row = session.query(Recordatorio).one()
        assert row.titulo == 'Internet' and row.monto == 1500 and row.dia_del_mes == 15 and row.estado == 'activo'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,description', [('ingreso','Intereses cobrados del plazo fijo'), ('egreso','Comisión cobrada por operar FCI')])
async def test_cash_flow_investment_interest_and_fees_are_admitted(ingress, accounts, kind, description):
    preview = await ingress.tool('register_movement', register_now(movement_type=kind, description=description, amount='25'))
    await ingress.confirm(preview)
    with accounts[0]() as session:
        row = session.query(MovimientoFinanciero).filter_by(descripcion=description).one()
        assert row.tipo == kind and row.cantidad == 25


@pytest.mark.asyncio
@pytest.mark.parametrize('effect', [None,'investment_principal','placement','existing_investment'])
async def test_semantic_effect_is_explicit_and_rejected_principal_never_mutates(ingress, accounts, effect):
    preview = await ingress.tool('register_movement', register_now(economic_effect=effect, description='Plazo fijo'))
    assert not isinstance(preview.reply_message, WhatsAppReplyButtons)
    assert 'economic_effect' not in preview.reply_text
    if effect is None:
        assert 'cobrado' in preview.reply_text or 'pagado' in preview.reply_text
    else:
        assert 'capital' in preview.reply_text
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count() == 0


@pytest.mark.asyncio
async def test_mixed_batch_policy_rejects_whole_batch_without_partial_write(ingress, accounts):
    result = await ingress.tool('register_movement_batch', {'items':[register_now(description='Comisión'),
        register_now(description='Capital', economic_effect='placement')]})
    assert 'capital' in result.reply_text
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count() == 0
        assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.descripcion.in_(['Comisión','Capital'])).count() == 0


@pytest.mark.asyncio
async def test_read_uses_sanitized_refs_then_update_and_annul_without_requery(ingress, accounts):
    observed = []
    def first_read(kwargs):
        observed.append(initial(kwargs))
        return [('find_movements', {'selector': {'kind':'all'}, 'currency':'ARS', 'sort_by':'date_desc'})]
    await ingress.send('Mostrá gastos', steps=[first_read,present_all])
    def update(kwargs):
        observed.append(initial(kwargs))
        return [('update_movement', {'movement_ref':visible_ref(kwargs,'movement'), 'changes':{'amount':'1200'}})]
    preview = await ingress.send('Cambiá el primero a1200', steps=[update])
    await ingress.confirm(preview)
    def annul(kwargs):
        context = initial(kwargs)
        ref = next(ref for ref,item in context['references'].items() if item['kind']=='movement' and Decimal(item['details']['amount'])==1200)
        return [('annul_movements', {'movement_refs':[ref]})]
    preview = await ingress.send('Anulá el que acabamos de modificar', steps=[annul])
    await ingress.confirm(preview)
    serialized = json.dumps(observed)
    assert str(accounts[1]) not in serialized and str(accounts[3]) not in serialized
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count() == 2
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=accounts[1], cantidad=1200).one().anulado_en is not None


@pytest.mark.asyncio
async def test_reset_clears_history_navigation_and_workspace_after_delivery_only(ingress, accounts):
    await ingress.tool('create_reminder', {'title':'Pendiente'})
    reset = await ingress.tool('reset_context')
    assert reset.clear_memory and not await ConversationHistoryService.get_recent(ingress.redis, PHONE)
    assert not (await assistant_state.read(accounts[1]))['workspace']['operations']
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=accounts[1]).count() == 9


@pytest.mark.asyncio
async def test_global_cut_ignores_old_workspace_and_rejects_old_pending_button(ingress, accounts):
    owner = accounts[1]
    await ingress.redis.set(f'assistant:v2:{owner}', json.dumps({'workspace':{'prepared_payload':{'amount':'9'}}}))
    result = await ingress.send('Confirmar', option_id='assistant:confirm:old-operation:1:0000000000000000')
    assert 'vigente' in result.reply_text
    reply = await ingress.tool('conversation_reply', {'kind':'help'})
    assert 'Podés' in reply.reply_text and not (await assistant_state.read(owner))['workspace']['operations']


@pytest.mark.asyncio
async def test_excess_suggests_new_compensation_without_applying_it(ingress, accounts):
    factory, owner, _, _, category = accounts
    donor = uuid4()
    with factory() as session:
        session.add(Categoria(id=donor, usuario_id=owner, nombre='Donante'))
        session.add_all([LimiteCategoria(usuario_id=owner,categoria_id=category,cantidad_max=1000,moneda='ARS',inicio_periodo=date(2026,10,1),fin_periodo=date(2026,10,31)),
            LimiteCategoria(usuario_id=owner,categoria_id=donor,cantidad_max=2000,moneda='ARS',inicio_periodo=date(2026,10,1),fin_periodo=date(2026,10,31))])
        session.commit()
    def registration(kwargs):
        category = next(c['ref'] for c in initial(kwargs)['categories'] if c['name']=='Comida')
        return [('register_movement', register_now(amount='300',category_ref=category))]
    preview = await ingress.send('Gasté300encomida', steps=[registration])
    result = await ingress.confirm(preview)
    assert not result.assistant_preview_delivery
    messages = [message.body for message in result.followup_messages
                if isinstance(message, WhatsAppText)]
    assert any('superó su límite' in body and 'Donante' in body
               and 'ajustá el presupuesto' in body for body in messages)
    state = await assistant_state.read(owner)
    assert state['workspace']['active_operation_id'] is None
    with factory() as session:
        assert {Decimal(row.cantidad_max) for row in session.query(LimiteCategoria)} == {Decimal(1000), Decimal(2000)}

    proposal = await ingress.tool('propose_compensation', {'currency': 'ARS'})
    assert proposal.assistant_preview_delivery
    assert 'Ajustar presupuesto' in proposal.reply_text and 'Donante' in proposal.reply_text
    await ingress.confirm(proposal)
    with factory() as session:
        rows = list(session.query(LimiteCategoria))
        assert sum(row.cantidad_max for row in rows) == 3000
        assert next(row.cantidad_max for row in rows if row.categoria_id==category) > 1000


@pytest.mark.asyncio
async def test_period_list_dashboard_link_only_when_more_rows_and_backend_period(ingress):
    scoped = await ingress.tool('find_movements', {'selector':{'kind':'period','period':{'start':'2026-10-01','end':'2026-10-31'}}, 'sort_by':'date_desc'}, 'Gastos deoctubre')
    from app.api.whatsapp import WhatsAppCTAURL
    links = [m for m in scoped.followup_messages if isinstance(m,WhatsAppCTAURL)]
    assert len(links)==1 and '2026-10-01' in links[0].url and '2026-10-31' in links[0].url
    unscoped = await ingress.tool('find_movements', {'selector':{'kind':'all'}, 'sort_by':'date_desc'})
    assert not any(isinstance(m,WhatsAppCTAURL) for m in unscoped.followup_messages)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['text', 'interactive'])
@pytest.mark.parametrize('proposal_succeeds', [False, True])
async def test_recurring_delivery_marks_only_exact_bound_followup_once(fake_redis, monkeypatch, entry, proposal_succeeds):
    from unittest.mock import AsyncMock, Mock
    from app.api.whatsapp import WhatsAppText, WhatsAppReplyButton
    from app.services.dispatch_contract import DispatchResult
    from app.services import recurring_expense
    candidate_id = str(uuid4())
    other = WhatsAppText('Otro seguimiento entregado')
    proposal = WhatsAppReplyButtons('PROPUESTA RECURRENTE', (WhatsAppReplyButton('opaque', 'Elegir'),))
    result = DispatchResult('Respuesta principal', followup_messages=[other, proposal, WhatsAppText('Otro final')],
        proposal_candidate_id=candidate_id, proposal_delivery_mode='followup', proposal_followup_message=proposal)
    record = Mock()
    monkeypatch.setattr(recurring_expense, 'record_proposal_sent', record)
    async def send(phone, message):
        return proposal_succeeds if message is proposal else True
    process = AsyncMock(return_value=result)
    async def execute():
        if entry == 'text':
            return await process_text_message_once(redis_client=fake_redis, sender_phone=PHONE, text_body='Pedido',
                whatsapp_message_id='exact-followup-text', process_message=process, send_message=send)
        return await process_interactive_message_once(redis_client=fake_redis,
            interactive_reply=InboundInteractiveReply('exact-followup-button', PHONE, 'button_reply', 'opaque', 'Pedido'),
            process_reply=process, send_message=send)
    assert await execute() == 'completed'
    assert record.call_count == int(proposal_succeeds)
    if proposal_succeeds:
        record.assert_called_once_with(candidate_id)
    history = await ConversationHistoryService.get_recent(fake_redis, PHONE)
    assert 'Otro seguimiento' in history[-1].content
    assert ('PROPUESTA RECURRENTE' in history[-1].content) == proposal_succeeds
    assert await execute() == 'duplicate'
    assert record.call_count == int(proposal_succeeds)
    assert len(await ConversationHistoryService.get_recent(fake_redis, PHONE)) == 2
