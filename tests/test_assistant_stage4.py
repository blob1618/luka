"""Stage 4 domain parity through the same tool runtime and delivery contract."""
from datetime import date, timedelta
from decimal import Decimal
import json
from uuid import UUID, uuid4

import pytest

from app.models.database import (
    AssistantWriteReceipt, Billetera, Categoria, CandidatoGastoRecurrente, LimiteCategoria,
    MovimientoFinanciero, Recordatorio, Usuario,
)
from app.services import assistant_state, assistant_runtime
from app.services.assistant_catalog import build_tool_registry
from app.services.assistant_runtime import run_for_phone
from app.services.conversation import ConversationHistoryService
from app.services.currency import UsdQuote
from app.services.limit import LimitService
from app.services import compensation, chart_query, monthly_balance
from app.services import conversation_flow
from tests import test_assistant_pilot
from tests.test_assistant_pilot import (
    NOW, PHONE, NativeScript, present_all, register_args, context_for, current_operation,
    deliver, confirm, proposal, amend_active, resume_last,
)

accounts = test_assistant_pilot.accounts


@pytest.fixture(autouse=True)
def stage4_sessions(isolated_db, monkeypatch):
    monkeypatch.setattr(compensation, 'SessionLocal', isolated_db)
    monkeypatch.setattr(chart_query, 'SessionLocal', isolated_db)
    monkeypatch.setattr(monthly_balance, 'SessionLocal', isolated_db)
    monkeypatch.setattr(conversation_flow, 'SessionLocal', isolated_db)


def initial(kwargs):
    return json.loads(next(message.content for message in kwargs['messages'] if message.role == 'user'))['context']


def tool_data(kwargs):
    return json.loads(next(message.content for message in kwargs['messages'] if message.role == 'tool'))['data']


async def request(name, args, message='domain', *, text='pedido', now=NOW):
    return await run_for_phone(PHONE, message, text, provider=NativeScript([[(name, args)], present_all]), now=now)


async def read(name, args=None, message='read-domain'):
    return await request(name, args or {}, message)


def category_ref(kwargs):
    return next(category['ref'] for category in initial(kwargs)['categories'] if category['name'] == 'Comida')


async def movement_reference(owner):
    context = context_for(owner)
    await context.initial_context()
    from app.services.assistant_capabilities.movements import find_movements
    from app.services.assistant_contracts import FindMovementsInput
    result = await find_movements(context, FindMovementsInput(selector={'kind': 'all'}, currency='ARS', limit=5))
    await assistant_state.save(owner, context.state)
    return [item['movement_ref'] for item in result.data['items']]


@pytest.mark.asyncio
async def test_batch_receipt_atomic_registration_and_replay(accounts):
    first = {**register_args(), 'description': 'uno'}
    second = {**register_args(), 'description': 'dos', 'movement_type': 'ingreso', 'amount': '200'}
    preview = await request('register_movement_batch', {'items': [first, second]}, text='Uno y dos el 16/09/2026')
    assert preview.confirmation_action and 'uno' in preview.text and 'dos' in preview.text
    result = await confirm(preview)
    replay = await run_for_phone(PHONE, 'batch-replay', action=preview.confirmation_action, now=NOW)
    assert replay.replayed and replay.text == result.text
    with accounts[0]() as db:
        assert db.query(AssistantWriteReceipt).one().command_kind == 'register_batch'
        rows = db.query(MovimientoFinanciero).filter(MovimientoFinanciero.whatsapp_message_id.like('domain:batch:%')).all()
        assert len(rows) == 2 and {row.descripcion for row in rows} == {'uno', 'dos'}


@pytest.mark.asyncio
async def test_batch_receipt_failure_rolls_back_every_item(accounts, monkeypatch):
    from sqlalchemy.orm import Session
    preview = await request('register_movement_batch', {'items': [register_args(), register_args()]}, text='El 16/09/2026')
    await deliver(preview)
    original = Session.flush
    def fail(self, *args, **kwargs):
        if any(isinstance(row, AssistantWriteReceipt) for row in self.new):
            raise ValueError('synthetic receipt failure')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Session, 'flush', fail)
    rejected = await run_for_phone(PHONE, 'batch-fail', action=preview.confirmation_action, now=NOW)
    assert rejected.committed is None
    with accounts[0]() as db:
        assert db.query(MovimientoFinanciero).count() == 10 and db.query(AssistantWriteReceipt).count() == 0


@pytest.mark.asyncio
async def test_batch_date_evidence_preserves_unchanged_item_across_days(accounts):
    started = NOW + timedelta(hours=2, minutes=55)
    items = [{**register_args(), 'date_spec': {'kind': 'relative_days', 'evidence_span': 'ayer', 'day_offset': -1}},
             {**register_args(), 'description': 'dos', 'date_spec': {'kind': 'relative_days', 'evidence_span': 'ayer', 'day_offset': -1}}]
    preview = await request('register_movement_batch', {'items': items}, text='Ayer: taxi y dos; ayer', now=started)
    assert preview.confirmation_action
    changed = [dict(items[0]), dict(items[1])]
    changed[1]['amount'] = '500'
    later = started + timedelta(minutes=10)
    edited = await run_for_phone(PHONE, 'batch-edit', 'El segundo importe es 500',
        provider=NativeScript([amend_active({'items': changed})]), now=later)
    pending = current_operation(await assistant_state.read(accounts[1]))
    assert edited.confirmation_action
    assert [item['date'] for item in pending['prepared_payload']['items']] == ['2026-10-26', '2026-10-26']
    assert pending['field_evidence']['items.0.date_spec']['source_message_id'] == 'domain'
    assert pending['field_evidence']['items.1.amount']['source_message_id'] == 'batch-edit'


@pytest.mark.asyncio
async def test_update_then_annul_exact_owned_selection_with_receipts(accounts):
    refs = await movement_reference(accounts[1])
    preview = await request('update_movement', {'movement_ref': refs[0], 'changes': {'amount': '777', 'description': 'editado'}})
    updated = await confirm(preview)
    assert updated.committed['command_kind'] == 'update' and updated.committed['movement']['description'] == 'editado'
    refs = await movement_reference(accounts[1])
    annul = await request('annul_movements', {'movement_refs': refs[:2]}, 'annul-source')
    final = await confirm(annul, 'annul-confirm')
    assert final.committed['command_kind'] == 'annul'
    with accounts[0]() as db:
        assert db.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.isnot(None)).count() == 2
        assert db.query(AssistantWriteReceipt).count() == 2
    assert (await run_for_phone(PHONE, 'annul-replay', action=annul.confirmation_action, now=NOW)).replayed


@pytest.mark.asyncio
async def test_stale_update_and_foreign_annul_cannot_write(accounts):
    refs = await movement_reference(accounts[1])
    preview = await request('update_movement', {'movement_ref': refs[0], 'changes': {'amount': '1000'}})
    await deliver(preview)
    pending = current_operation(await assistant_state.read(accounts[1]))
    with accounts[0]() as db:
        row = db.get(MovimientoFinanciero, UUID(pending['prepared_payload']['movement_id']))
        row.descripcion = 'changed elsewhere'
        db.commit()
    assert (await run_for_phone(PHONE, 'stale-update', action=preview.confirmation_action, now=NOW)).committed is None
    foreign = await run_for_phone('5491100260002', 'foreign-annul', 'anular', provider=NativeScript([
        [('annul_movements', {'movement_refs': [refs[1]]})]]), now=NOW)
    assert foreign.confirmation_action is None


@pytest.mark.asyncio
async def test_categories_create_rename_delete_and_wallet_create(accounts):
    created = await confirm(await request('create_category', {'name': 'Nueva'}))
    assert 'creada' in created.text
    def rename(kwargs):
        ref = next(item['ref'] for item in initial(kwargs)['categories'] if item['name'] == 'Nueva')
        return [('rename_category', {'category_ref': ref, 'name': 'Renombrada'})]
    preview = await run_for_phone(PHONE, 'rename', 'renombrar', provider=NativeScript([rename]), now=NOW)
    assert (await confirm(preview, 'rename-confirm')).committed
    def delete(kwargs):
        ref = next(item['ref'] for item in initial(kwargs)['categories'] if item['name'] == 'Renombrada')
        return [('delete_category', {'category_ref': ref})]
    preview = await run_for_phone(PHONE, 'delete', 'borrar', provider=NativeScript([delete]), now=NOW)
    assert (await confirm(preview, 'delete-confirm')).committed
    wallet = await confirm(await request('create_wallet', {'name': 'Banco', 'currency': 'ARS'}, 'wallet-source'), 'wallet-confirm')
    assert wallet.committed
    with accounts[0]() as db:
        assert db.query(Categoria).filter_by(usuario_id=accounts[1], nombre='Renombrada').one().esta_eliminado
        assert db.query(Billetera).filter_by(usuario_id=accounts[1], nombre='Banco').count() == 1


@pytest.mark.asyncio
async def test_category_catalog_does_not_mix_currency_amounts(accounts):
    with accounts[0]() as db:
        usd = db.query(Billetera).filter_by(usuario_id=accounts[1], moneda='USD').one()
        db.add(MovimientoFinanciero(usuario_id=accounts[1], billetera_id=usd.id, categoria_id=accounts[4],
            tipo='egreso', cantidad=9000, moneda='USD', descripcion='dólares', fecha_movimiento=date(2026, 10, 10)))
        db.commit()
    reply = await read('list_categories')
    assert 'Comida' in reply.text and '9.838' not in reply.text and '9.828' not in reply.text
    ars = await read('list_categories', {'currency': 'ARS'}, message='categories-ars')
    usd = await read('list_categories', {'currency': 'USD'}, message='categories-usd')
    assert 'ARS' in ars.text and '838,00' in ars.text and '9.000' not in ars.text
    assert 'USD' in usd.text and '9.000,00' in usd.text and '838,00' not in usd.text


@pytest.mark.asyncio
async def test_child_category_returns_parent_preview_without_autocommit_and_replays(accounts):
    # An incomplete limit is a live parent; creation supplies its missing category.
    parent = await request('create_limit', {'amount': '1000', 'month': 10, 'year': 2026, 'currency': 'ARS'}, 'parent')
    assert parent.confirmation_action is None
    def child(kwargs):
        op = initial(kwargs)['operations'][0]
        return [('create_category', {'name': 'Hija', 'parent_ref': op['ref'], 'parent_revision': op['revision'], 'parent_field': 'category_ref'})]
    preview = await run_for_phone(PHONE, 'child', 'creá Hija', provider=NativeScript([child]), now=NOW)
    await deliver(preview)
    result = await run_for_phone(PHONE, 'child-confirm', action=preview.confirmation_action, now=NOW)
    assert result.confirmation_action and 'Hija' in result.text
    with accounts[0]() as db:
        assert db.query(LimiteCategoria).count() == 0 and db.query(Categoria).filter_by(nombre='Hija').count() == 1
    replay = await run_for_phone(PHONE, 'child-replay', action=preview.confirmation_action, now=NOW)
    assert replay.replayed
    await deliver(result)
    final = await run_for_phone(PHONE, 'parent-confirm', action=result.confirmation_action, now=NOW)
    assert final.committed
    with accounts[0]() as db:
        assert db.query(LimiteCategoria).count() == 1


async def create_limit(amount='1000', *, message='limit-source', month=10):
    def prepare(kwargs):
        return [('create_limit', {'category_ref': category_ref(kwargs), 'amount': amount, 'month': month, 'year': 2026, 'currency': 'ARS'})]
    return await run_for_phone(PHONE, message, 'límite', provider=NativeScript([prepare]), now=NOW)


@pytest.mark.asyncio
async def test_limits_list_edit_selected_and_delete_with_visible_refs(accounts):
    await confirm(await create_limit(), 'limit-confirm')
    await confirm(await create_limit('2000', message='limit-nov', month=11), 'limit-nov-confirm')
    await read('list_limits', message='limits-visible')
    def select(kwargs):
        resources = initial(kwargs)['references']
        ref = next(ref for ref, item in resources.items() if item['kind'] == 'limit' and item['details']['month'] == 11)
        assert str(accounts[1]) not in json.dumps(initial(kwargs))
        return [('change_limit', {'limit_ref': ref, 'amount': '2500'})]
    edited = await run_for_phone(PHONE, 'limit-edit', 'El segundo a 2500', provider=NativeScript([select]), now=NOW)
    assert (await confirm(edited, 'limit-edit-confirm')).committed
    def deletions(kwargs):
        return [('delete_limits', {'limit_refs': [item['limit_ref'] for item in tool_data(kwargs)['items']]})]
    preview = await run_for_phone(PHONE, 'limit-delete', 'eliminá ambos', provider=NativeScript([[('list_limits', {})], deletions]), now=NOW)
    assert (await confirm(preview, 'limit-delete-confirm')).committed
    with accounts[0]() as db:
        assert db.query(LimiteCategoria).count() == 0


@pytest.mark.asyncio
async def test_budget_and_compensation_use_backend_snapshots(accounts):
    with accounts[0]() as db:
        donor = Categoria(usuario_id=accounts[1], nombre='Transporte', es_default=False)
        db.add(donor)
        db.commit()
    LimitService.create_limit(PHONE, {'limit_category': 'Comida', 'limit_amount': '500', 'limit_month': 10, 'limit_year': 2026, 'limit_currency': 'ARS'}, today=date(2026, 10, 27))
    LimitService.create_limit(PHONE, {'limit_category': 'Transporte', 'limit_amount': '1000', 'limit_month': 10, 'limit_year': 2026, 'limit_currency': 'ARS'}, today=date(2026, 10, 27))
    query = await read('query_budget', {'currency': 'ARS'})
    assert '828,00' in query.text and '328,00' in query.text
    preview = await request('propose_compensation', {'currency': 'ARS'}, 'compensation')
    assert preview.confirmation_action and '328,00' in preview.text
    assert (await confirm(preview, 'compensation-confirm')).committed
    replay = await run_for_phone(PHONE, 'compensation-replay', action=preview.confirmation_action, now=NOW)
    assert replay.replayed


@pytest.mark.asyncio
async def test_reminder_missing_day_duplicate_edit_preserves_amount_and_manage_paused(accounts):
    partial = await request('create_reminder', {'title': 'Internet', 'amount': '1500', 'currency': 'ARS'}, 'rem-source')
    assert not partial.confirmation_action
    ready = await run_for_phone(PHONE, 'rem-day', '15', provider=NativeScript([amend_active({'day': 15})]), now=NOW)
    assert (await confirm(ready, 'rem-create-confirm')).committed
    duplicate = await request('create_reminder', {'title': 'Internet', 'day': 20}, 'rem-duplicate')
    assert not duplicate.confirmation_action
    await run_for_phone(PHONE, 'cancel-duplicate', 'cancelar', now=NOW)
    await read('list_reminders', message='rem-visible')
    def modify(kwargs):
        ref = next(ref for ref, item in initial(kwargs)['references'].items() if item['kind'] == 'reminder')
        return [('update_reminder', {'reminder_ref': ref, 'day': 16})]
    preview = await run_for_phone(PHONE, 'rem-edit', 'el día16', provider=NativeScript([modify]), now=NOW)
    assert (await confirm(preview, 'rem-edit-confirm')).committed
    with accounts[0]() as db:
        reminder = db.query(Recordatorio).filter_by(usuario_id=accounts[1]).one()
        assert reminder.titulo == 'Internet' and reminder.dia_del_mes == 16 and reminder.monto == Decimal('1500') and reminder.moneda == 'ARS'
    def pause(kwargs):
        return [('change_reminder_state', {'reminder_ref': tool_data(kwargs)['items'][0]['reminder_ref'], 'state': 'pause'})]
    preview = await run_for_phone(PHONE, 'rem-pause', 'pausá el primero', provider=NativeScript([[('list_reminders', {})], pause]), now=NOW)
    assert (await confirm(preview, 'rem-pause-confirm')).committed
    assert 'Internet' not in (await read('list_reminders')).text
    def activate(kwargs):
        return [('change_reminder_state', {'reminder_ref': tool_data(kwargs)['items'][0]['reminder_ref'], 'state': 'activate'})]
    preview = await run_for_phone(PHONE, 'rem-activate', 'activá', provider=NativeScript([[('list_reminders', {'include_paused': True})], activate]), now=NOW)
    assert (await confirm(preview, 'rem-activate-confirm')).committed
    def delete(kwargs):
        return [('change_reminder_state', {'reminder_ref': tool_data(kwargs)['items'][0]['reminder_ref'], 'state': 'delete'})]
    preview = await run_for_phone(PHONE, 'rem-delete', 'eliminá', provider=NativeScript([[('list_reminders', {})], delete]), now=NOW)
    assert (await confirm(preview, 'rem-delete-confirm')).committed


@pytest.mark.asyncio
async def test_proactive_preferences_confirmed_only(accounts):
    preview = await request('set_proactive_prompts', {'enabled': False})
    assert preview.confirmation_action
    assert (await confirm(preview)).committed
    with accounts[0]() as db:
        assert not db.get(Usuario, accounts[1]).proactivo_habilitado


@pytest.mark.asyncio
@pytest.mark.parametrize('accept', [True, False])
async def test_recurring_candidate_decision_owner_expiry_and_idempotent_callback(accounts, accept):
    now = assistant_runtime.datetime.now(assistant_runtime.timezone.utc)
    with accounts[0]() as db:
        candidate = CandidatoGastoRecurrente(usuario_id=accounts[1], concepto='Servicio', descripcion_normalizada='servicio',
            patron_hash='a' * 64, categoria_id=accounts[4], dia_estimado=15, monto_estimado=1000, moneda='ARS',
            estado='pendiente', propuesta_en=now, ultima_fecha_movimiento=now.date() - timedelta(days=30),
            proxima_fecha_estimada=now.date() + timedelta(days=5), evidencia_movimiento_ids=[])
        db.add(candidate)
        db.commit()
        identifier = str(candidate.id)
    def choose(kwargs):
        return [('accept_recurring_candidate' if accept else 'reject_recurring_candidate', {'candidate_ref': tool_data(kwargs)['items'][0]['candidate_ref']})]
    preview = await run_for_phone(PHONE, 'candidate-source', 'decisión', provider=NativeScript([[('list_recurring_candidates', {})], choose]), now=NOW)
    assert preview.confirmation_action
    result = await confirm(preview)
    assert result.committed
    from app.services.assistant_capabilities.reminder_management import apply_candidate
    assert (await apply_candidate(accounts[1], identifier, accept)).status in {'already_converted', 'already_rejected'}
    assert not (await apply_candidate(accounts[2], identifier, accept)).successful


@pytest.mark.asyncio
async def test_chart_collects_period_interrupts_resumes_and_delivers_real_png_without_confirmation(accounts):
    question = await request('movement_chart', {'chart_mode': 'monthly', 'chart_currency': 'ARS'}, 'chart-source')
    assert not question.confirmation_action and 'período' in question.text
    await read('wallet_balances', message='chart-interrupt')
    await run_for_phone(PHONE, 'chart-resume', 'retomá', provider=NativeScript([resume_last]), now=NOW)
    result = await run_for_phone(PHONE, 'chart-month', 'desde septiembre', provider=NativeScript([amend_active({'chart_start_month': '2026-09'})]), now=NOW)
    assert result.confirmation_action is None and result.outbound
    assert result.outbound[0].content.startswith(b'\x89PNG') and result.event_key == 'chart.completed'
    assert current_operation(await assistant_state.read(accounts[1]))['status'] == 'committed'


@pytest.mark.asyncio
async def test_chart_followup_torta_and_distribution_choice(accounts):
    first = await request('movement_chart', {'chart_currency': 'ARS'}, 'chart-first')
    assert first.outbound and not first.confirmation_action
    def pie(kwargs):
        ref = next(ref for ref, item in initial(kwargs)['references'].items() if item['kind'] == 'chart_query')
        return [('movement_chart', {'previous_ref': ref, 'chart_type': 'pie'})]
    second = await run_for_phone(PHONE, 'chart-pie', 'ahora en torta', provider=NativeScript([pie]), now=NOW)
    assert second.outbound and second.outbound[0].content.startswith(b'\x89PNG')
    choices = await request('movement_chart', {'chart_mode': 'month_comparison', 'chart_months': ['2026-09', '2026-10'],
        'chart_type': 'pie', 'movement_type': 'both', 'chart_currency': 'ARS'}, 'chart-distribution')
    assert not choices.outbound and choices.event_key == 'chart.distribution_choice' and 'Pastel' in choices.text
    result = await run_for_phone(PHONE, 'chart-bar', 'en barras', provider=NativeScript([amend_active({'chart_type': 'bar'})]), now=NOW)
    assert result.outbound and result.confirmation_action is None


@pytest.mark.asyncio
async def test_protected_quotes_precision_and_net_worth(accounts, monkeypatch):
    from app.services.assistant_capabilities import financial_reads
    observed = []
    async def quotes(types):
        return {kind: UsdQuote(tipo=kind, compra=Decimal('1234.56789'), venta=Decimal('1235.123456'),
            fuente='fuente', as_of=NOW, fetched_at=NOW, stale=True) for kind in types}
    async def render(kind, facts, types):
        observed.append((kind, facts, types))
        return '\n'.join(item['label'] + ': ' + item['value'] for item in facts)
    monkeypatch.setattr(financial_reads, 'get_usd_quotes', quotes)
    monkeypatch.setattr(financial_reads.LLMService, 'render_financial_reply', render)
    result = await read('query_usd_quotes', {'quote_types': ['oficial']})
    assert '1.234,56789' in result.text and 'desactualizada' in result.text and 'Fuente' in result.text
    result = await read('query_net_worth', {'quote_types': ['oficial']}, message='net-worth')
    assert 'Saldos ARS' in result.text and observed[-1][0] == 'net_worth'


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args,expected', [
    ('previous_month_balance', {}, '10,00'), ('financial_education', {'term': 'inflación'}, 'inflación'),
    ('investment_guidance', {}, 'Fuentes'), ('investment_guidance', {'topic_id': 'plazo_fijo_tradicional'}, 'Plazo'),
    ('conversation_reply', {'kind': 'greeting'}, 'Hola'), ('conversation_reply', {'kind': 'help'}, 'categorías'),
    ('conversation_reply', {'kind': 'out_of_scope'}, 'finanzas'),
])
async def test_catalog_reads_and_conversation_parity(accounts, name, args, expected):
    result = await read(name, args)
    assert expected.casefold() in result.text.casefold() and not result.confirmation_action


@pytest.mark.asyncio
async def test_dashboard_emits_backend_url_button_without_exposing_link_to_model(accounts):
    with accounts[0]() as db:
        db.get(Usuario, accounts[1]).auth_user_id = uuid4()
        db.commit()
    result = await read('dashboard_link')
    assert result.outbound and result.event_key == 'dashboard.link.sent'
    assert result.outbound[0].url and result.outbound[0].url not in result.text


@pytest.mark.asyncio
async def test_reset_preserves_receipts_and_clears_history_only_after_delivery(accounts, fake_redis):
    await confirm(await proposal())
    await ConversationHistoryService.append_exchange(fake_redis, PHONE, 'visible', 'respuesta', 'history')
    result = await request('reset_context', {}, 'reset')
    assert result.clear_memory and not (await assistant_state.read(accounts[1]))['workspace']['operations']
    assert await ConversationHistoryService.get_recent(fake_redis, PHONE)
    with accounts[0]() as db:
        assert db.query(AssistantWriteReceipt).count() == 1


def test_catalog_covers_current_domains_and_has_no_model_commit_tool():
    registry = build_tool_registry()
    expected = {'register_movement_batch', 'update_movement', 'annul_movements', 'list_categories', 'create_category',
        'rename_category', 'delete_category', 'create_wallet', 'create_limit', 'change_limit', 'list_limits', 'delete_limits',
        'query_budget', 'propose_compensation', 'create_reminder', 'update_reminder', 'change_reminder_state',
        'set_proactive_prompts', 'accept_recurring_candidate', 'reject_recurring_candidate', 'movement_chart',
        'query_net_worth', 'query_usd_quotes', 'previous_month_balance', 'financial_education', 'investment_guidance',
        'dashboard_link', 'reset_context', 'conversation_reply'}
    assert expected <= registry.tools.keys() and not any('commit' in key or 'confirm' in key for key in registry.tools)


@pytest.fixture
def fixed_pilot_clock(monkeypatch):
    from datetime import datetime
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(assistant_runtime, 'datetime', Fixed)


def publish(accounts, event_key, definition):
    from app.services.conversation_flow import ConversationFlowService
    flow = ConversationFlowService.create(slug='stage4-' + uuid4().hex, name='Stage4',
        event_key=event_key, definition=definition, session_factory=accounts[0])
    return ConversationFlowService.publish(flow.id, session_factory=accounts[0])


@pytest.mark.asyncio
async def test_editor_same_published_node_old_button_cannot_confirm_new_operation(accounts, fixed_pilot_clock):
    from app.services.assistant_transport import assemble_reply
    from app.services.assistant_actions import process_interaction
    publish(accounts, 'category.confirmation_required', {'start_node': 'confirm', 'nodes': [
        {'id': 'confirm', 'type': 'reply_button', 'body': 'Crear {category}', 'options': [
            {'id': 'yes', 'title': 'Crear', 'action': 'confirm_category'},
            {'id': 'no', 'title': 'Cancelar', 'action': 'reject_category'}]}]})
    first = await request('create_category', {'name': 'A'}, 'editor-a')
    a = await assemble_reply(PHONE, first)
    old_button = a.followup_messages[-1].buttons[0].id
    await deliver(first)
    second = await request('create_category', {'name': 'B'}, 'editor-b')
    b = await assemble_reply(PHONE, second)
    new_button = b.followup_messages[-1].buttons[0].id
    assert old_button != new_button
    await deliver(second)
    stale = await process_interaction(PHONE, 'old-editor-button', old_button, 'button_reply')
    assert 'ya no está disponible' in stale.reply_text
    with accounts[0]() as db:
        assert db.query(Categoria).filter(Categoria.nombre.in_(['A', 'B'])).count() == 0
    good = await process_interaction(PHONE, 'new-editor-button', new_button, 'button_reply')
    assert 'creada' in good.reply_text
    with accounts[0]() as db:
        assert db.query(Categoria).filter_by(nombre='B').count() == 1
        assert db.query(Categoria).filter_by(nombre='A').count() == 0


@pytest.mark.asyncio
async def test_editor_navigation_preserves_internal_binding_and_limit_year_is_only_amend(accounts, fixed_pilot_clock):
    from app.services.assistant_transport import assemble_reply
    from app.services.assistant_actions import process_interaction
    from app.services.conversation import ConversationService
    publish(accounts, 'limit.year_confirmation', {'start_node': 'question', 'nodes': [
        {'id': 'question', 'type': 'reply_button', 'body': '¿Año {year}?', 'options': [
            {'id': 'detail', 'title': 'Ver', 'next_node': 'detail'}]},
        {'id': 'detail', 'type': 'reply_button', 'body': 'Usar {year}', 'options': [
            {'id': 'yes', 'title': 'Usar año', 'action': 'confirm_limit_year'}]}]})
    def create(kwargs):
        return [('create_limit', {'category_ref': category_ref(kwargs), 'amount': '1000', 'month': 10, 'currency': 'ARS'})]
    answer = await run_for_phone(PHONE, 'year-start', 'octubre', provider=NativeScript([create]), now=NOW)
    result = await assemble_reply(PHONE, answer)
    binding = (await ConversationService.get_pending_conversation_flow(PHONE)).backend_binding
    next_result = await process_interaction(PHONE, 'year-nav', result.followup_messages[-1].buttons[0].id, 'button_reply')
    pending = await ConversationService.get_pending_conversation_flow(PHONE)
    assert pending.backend_binding == binding and pending.node_id == 'detail'
    amended = await process_interaction(PHONE, 'year-confirm', next_result.reply_message.buttons[0].id, 'button_reply')
    assert amended.assistant_preview_delivery is not None
    with accounts[0]() as db:
        assert db.query(LimiteCategoria).count() == 0
    assert (await assistant_state.read(accounts[1]))['workspace']['last_confirmation'] is None


@pytest.mark.asyncio
async def test_movement_category_button_opens_same_typed_workspace(accounts, fixed_pilot_clock):
    from app.services.assistant_actions import process_interaction
    registered = await confirm(await proposal())
    assert registered.outbound
    button = registered.outbound[0].buttons[0].id
    result = await process_interaction(PHONE, 'category-button', button, 'button_reply')
    pending = current_operation(await assistant_state.read(accounts[1]))
    assert 'categoría' in result.reply_text and pending['operation_key'] == 'update_movement'
    assert pending['status'] == 'collecting'


@pytest.mark.asyncio
async def test_investment_catalog_exposes_real_ids_and_unknown_topic_is_uniform(accounts):
    def topic(kwargs):
        items = tool_data(kwargs)['items']
        assert {'topic_id', 'title'} <= items[0].keys()
        return [('investment_guidance', {'topic_id': items[0]['topic_id']})]
    result = await run_for_phone(PHONE, 'investment-select', 'un tema', provider=NativeScript([
        [('investment_guidance', {})], topic, present_all]), now=NOW)
    assert 'Fuentes' in result.text
    bad = await request('investment_guidance', {'topic_id': 'inventado'}, 'investment-bad')
    assert 'desconocido' in bad.text and not bad.confirmation_action


@pytest.mark.asyncio
async def test_child_category_completes_nested_update_without_losing_other_changes(accounts):
    refs = await movement_reference(accounts[1])
    parent = await request('update_movement', {'movement_ref': refs[0], 'changes': {'amount': '777'}, 'require_category': True}, 'nested-parent')
    assert parent.confirmation_action is None
    def child(kwargs):
        op = initial(kwargs)['operations'][0]
        return [('create_category', {'name': 'Nueva edición', 'parent_ref': op['ref'], 'parent_revision': op['revision'],
            'parent_field': 'changes.category_ref'})]
    child_preview = await run_for_phone(PHONE, 'nested-child', 'crear categoría', provider=NativeScript([child]), now=NOW)
    completed = await confirm(child_preview, 'nested-child-confirm')
    assert completed.confirmation_action and '777' in completed.text
    pending = current_operation(await assistant_state.read(accounts[1]))
    assert pending['draft_payload']['changes']['amount'] == '777'
    await deliver(completed)
    result = await run_for_phone(PHONE, 'nested-parent-confirm', action=completed.confirmation_action, now=NOW)
    assert result.committed['movement']['amount'].startswith('777')
    assert result.committed['category_name'] == 'Nueva edición'


@pytest.mark.asyncio
async def test_child_with_cancelled_parent_never_creates_category(accounts):
    await request('create_limit', {'amount': '1000', 'month': 10, 'year': 2026, 'currency': 'ARS'}, 'cancelled-parent')
    def child(kwargs):
        op = initial(kwargs)['operations'][0]
        return [('create_category', {'name': 'No crear', 'parent_ref': op['ref'], 'parent_revision': op['revision'], 'parent_field': 'category_ref'})]
    preview = await run_for_phone(PHONE, 'cancelled-parent-child', 'crear', provider=NativeScript([child]), now=NOW)
    await deliver(preview)
    state = await assistant_state.read(accounts[1])
    context = context_for(accounts[1], state=state)
    parent = next(op for op in context.operations.workspace(context).operations.values() if op.source_message_id == 'cancelled-parent')
    view = await context.operations.view(context, parent)
    await context.operations.cancel(context, view.ref, expected_revision=parent.revision)
    await assistant_state.save(accounts[1], context.state)
    failed = await run_for_phone(PHONE, 'cancelled-parent-confirm', action=preview.confirmation_action, now=NOW)
    assert failed.committed is None
    with accounts[0]() as db:
        assert db.query(Categoria).filter_by(nombre='No crear').count() == 0


@pytest.mark.asyncio
async def test_limit_query_honors_category_currency_and_reference_day(accounts):
    with accounts[0]() as db:
        db.add(Categoria(usuario_id=accounts[1], nombre='Transporte', es_default=False))
        db.commit()
    for name, month, amount in [('Comida', 10, '1000'), ('Comida', 11, '2000'), ('Transporte', 10, '3000')]:
        assert LimitService.create_limit(PHONE, {'limit_category': name, 'limit_month': month, 'limit_year': 2026,
            'limit_amount': amount, 'limit_currency': 'ARS'}, today=date(2026, 10, 27)).status == 'created'
    def filtered(kwargs):
        return [('list_limits', {'category_ref': category_ref(kwargs), 'reference_day': '2026-11-15', 'currency': 'ARS'})]
    result = await run_for_phone(PHONE, 'filter-limits', 'noviembre comida', provider=NativeScript([filtered, present_all]), now=NOW)
    assert '2.000,00' in result.text and '1.000,00' not in result.text and 'Transporte' not in result.text


@pytest.mark.asyncio
async def test_editor_commit_save_failure_reports_committed_success(accounts, fixed_pilot_clock, monkeypatch):
    from app.services.assistant_actions import process_interaction
    from app.services.assistant_transport import assemble_reply
    from app.services.conversation import ConversationStateUnavailable
    publish(accounts, 'category.confirmation_required', {'start_node': 'confirm', 'nodes': [
        {'id': 'confirm', 'type': 'reply_button', 'body': 'Crear {category}', 'options': [
            {'id': 'yes', 'title': 'Crear', 'action': 'confirm_category'}]}]})
    preview = await request('create_category', {'name': 'Guardada'}, 'save-fail-category')
    dispatched = await assemble_reply(PHONE, preview)
    await deliver(preview)
    async def fail(*args):
        raise ConversationStateUnavailable('synthetic failure')
    monkeypatch.setattr(assistant_state, 'save', fail)
    result = await process_interaction(PHONE, 'save-fail-confirm', dispatched.followup_messages[-1].buttons[0].id, 'button_reply')
    assert 'creada' in result.reply_text and 'No hice cambios' not in result.reply_text
    with accounts[0]() as db:
        assert db.query(Categoria).filter_by(nombre='Guardada').count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args', [('create_limit', {'amount': 'cinco mil'}),
    ('create_reminder', {'amount': '5,000'}), ('update_movement', {'changes': {'amount': 'cinco mil'}})])
async def test_invalid_money_arguments_are_schema_feedback_without_mutation(accounts, name, args):
    result = await request(name, args, 'invalid-money')
    assert result.committed is None and result.confirmation_action is None
    state = await assistant_state.read(accounts[1])
    assert not state['workspace']['operations']
    with accounts[0]() as db:
        assert db.query(AssistantWriteReceipt).count() == 0 and db.query(MovimientoFinanciero).count() == 10


@pytest.mark.asyncio
async def test_registration_explicit_new_category_remains_collecting_for_child(accounts):
    preview = await request('register_movement', {**register_args(), 'requested_category': 'Nueva'},
        'register-needs-category', text='Gasté106 el16/09/2026 en categoría Nueva')
    assert not preview.confirmation_action and 'categoría' in preview.text


@pytest.mark.asyncio
async def test_recurring_callback_save_failure_keeps_domain_success(accounts, fixed_pilot_clock, monkeypatch):
    from app.services.assistant_actions import process_interaction
    from app.services.conversation import ConversationStateUnavailable
    with accounts[0]() as db:
        candidate = CandidatoGastoRecurrente(usuario_id=accounts[1], concepto='Callback', descripcion_normalizada='callback',
            patron_hash='b' * 64, dia_estimado=15, monto_estimado=1000, moneda='ARS', estado='pendiente', propuesta_en=NOW,
            ultima_fecha_movimiento=NOW.date() - timedelta(days=30), proxima_fecha_estimada=NOW.date() + timedelta(days=5),
            evidencia_movimiento_ids=[])
        db.add(candidate)
        db.commit()
        identifier = str(candidate.id)
    async def fail(*args):
        raise ConversationStateUnavailable('synthetic failure')
    monkeypatch.setattr(assistant_state, 'save', fail)
    result = await process_interaction(PHONE, 'candidate-callback-save-fail', 'rec_cand:accept:' + identifier, 'button_reply')
    assert 'Agendé el recordatorio' in result.reply_text and 'No hice cambios' not in result.reply_text
    with accounts[0]() as db:
        assert db.query(Recordatorio).filter_by(candidato_id=UUID(identifier)).count() == 1


@pytest.mark.asyncio
async def test_update_publishes_current_snapshot_for_direct_annul_and_keeps_stale_rejection(accounts):
    refs = await movement_reference(accounts[1])
    old_ref = refs[0]
    preview = await request('update_movement', {'movement_ref': old_ref, 'changes': {'amount': '1200'}}, 'refresh-update')
    updated = await confirm(preview, 'refresh-update-confirm')
    identifier = updated.committed['movement']['id']
    def annul_current(kwargs):
        view = initial(kwargs)
        assert old_ref not in view['references'] and old_ref not in view['last_shown']
        candidates = [(ref, value) for ref, value in view['references'].items() if value['kind'] == 'movement'
            and value['details']['amount'].startswith('1200')]
        assert len(candidates) == 1
        return [('annul_movements', {'movement_refs': [candidates[0][0]]})]
    preview = await run_for_phone(PHONE, 'refresh-annul', 'anulá el modificado', provider=NativeScript([annul_current]), now=NOW)
    assert preview.confirmation_action
    result = await confirm(preview, 'refresh-annul-confirm')
    assert result.committed['movement']['id'] == identifier
    stale = await request('update_movement', {'movement_ref': old_ref, 'changes': {'amount': '1'}}, 'refresh-stale')
    assert stale.confirmation_action is None


@pytest.mark.asyncio
async def test_reminder_pause_activate_without_requery_uses_new_resource_snapshot(accounts):
    preview = await request('create_reminder', {'title': 'Internet', 'day': 15, 'amount': '1500'}, 'refresh-reminder')
    await confirm(preview, 'refresh-reminder-confirm')
    def pause(kwargs):
        ref = next(ref for ref, value in initial(kwargs)['references'].items() if value['kind'] == 'reminder' and value['details']['estado'] == 'activo')
        return [('change_reminder_state', {'reminder_ref': ref, 'state': 'pause'})]
    preview = await run_for_phone(PHONE, 'refresh-pause', 'pausalo', provider=NativeScript([pause]), now=NOW)
    await confirm(preview, 'refresh-pause-confirm')
    def activate(kwargs):
        resources = [value for value in initial(kwargs)['references'].values() if value['kind'] == 'reminder']
        assert len(resources) == 1 and resources[0]['details']['estado'] == 'pausado'
        ref = next(ref for ref, value in initial(kwargs)['references'].items() if value['kind'] == 'reminder')
        return [('change_reminder_state', {'reminder_ref': ref, 'state': 'activate'})]
    preview = await run_for_phone(PHONE, 'refresh-activate', 'activalo', provider=NativeScript([activate]), now=NOW)
    assert preview.confirmation_action
    await confirm(preview, 'refresh-activate-confirm')
    with accounts[0]() as db:
        assert db.query(Recordatorio).filter_by(titulo='Internet').one().estado == 'activo'


@pytest.mark.asyncio
async def test_limit_changed_reference_can_be_deleted_without_requery(accounts):
    await confirm(await create_limit(), 'refresh-limit-confirm')
    def change(kwargs):
        ref = next(ref for ref, value in initial(kwargs)['references'].items() if value['kind'] == 'limit')
        return [('change_limit', {'limit_ref': ref, 'amount': '2500'})]
    preview = await run_for_phone(PHONE, 'refresh-limit-change', 'cambialo', provider=NativeScript([change]), now=NOW)
    await confirm(preview, 'refresh-limit-change-confirm')
    def delete(kwargs):
        resources = [(ref, value) for ref, value in initial(kwargs)['references'].items() if value['kind'] == 'limit']
        assert len(resources) == 1 and resources[0][1]['details']['amount'].startswith('2500')
        return [('delete_limits', {'limit_refs': [resources[0][0]]})]
    preview = await run_for_phone(PHONE, 'refresh-limit-delete', 'eliminalo', provider=NativeScript([delete]), now=NOW)
    assert preview.confirmation_action
    await confirm(preview, 'refresh-limit-delete-confirm')
    with accounts[0]() as db:
        assert db.query(LimiteCategoria).count() == 0


@pytest.mark.asyncio
async def test_resume_collecting_continues_to_amend_from_same_user_message(accounts):
    await request('create_reminder', {'title': 'Internet', 'amount': '1500', 'currency': 'ARS'}, 'resume-one-source')
    await read('wallet_balances', message='resume-one-interrupt')
    def complete(kwargs):
        operation = json.loads(next(message.content for message in kwargs['messages'] if message.role == 'tool'))['operation']
        assert operation['status'] == 'collecting'
        return [('operation_amend', {'ref': operation['ref'], 'expected_revision': operation['revision'], 'changes': {'day': 15}})]
    provider = NativeScript([resume_last, complete])
    result = await run_for_phone(PHONE, 'resume-and-complete', 'Retomá Internet y completalo: vence el día15', provider=provider, now=NOW)
    assert provider.calls == 2 and result.confirmation_action and '1500' in result.text.replace('.', '').replace(',', '')
    assert 'Necesito que aclares' not in result.text
    assert (await confirm(result, 'resume-completed-confirm')).committed
    with accounts[0]() as db:
        reminder = db.query(Recordatorio).filter_by(titulo='Internet').one()
        assert reminder.dia_del_mes == 15 and reminder.monto == Decimal('1500')


@pytest.mark.asyncio
async def test_resume_collecting_keeps_backend_question_when_no_new_data(accounts):
    await request('create_reminder', {'title': 'Internet'}, 'resume-fallback-source')
    await read('wallet_balances', message='resume-fallback-interrupt')
    result = await run_for_phone(PHONE, 'resume-no-data', 'Retomalo', provider=NativeScript([resume_last, []]), now=NOW)
    assert not result.confirmation_action and 'día de vencimiento' in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize('ready_parent', [False, True])
async def test_editor_reject_category_resumes_parent_without_autocommit(accounts, fixed_pilot_clock, ready_parent):
    from app.services.assistant_actions import process_interaction
    from app.services.assistant_transport import assemble_reply
    publish(accounts, 'category.confirmation_required', {'start_node': 'reject', 'nodes': [
        {'id': 'reject', 'type': 'reply_button', 'body': 'Crear {category}', 'options': [
            {'id': 'no', 'title': 'No crear', 'action': 'reject_category'}]}]})
    if ready_parent:
        await create_limit(message='reject-parent')
    else:
        await request('create_limit', {'amount': '1000', 'month': 10, 'year': 2026, 'currency': 'ARS'}, 'reject-parent')
    def child(kwargs):
        parent = initial(kwargs)['operations'][0]
        return [('create_category', {'name': 'No crear', 'parent_ref': parent['ref'], 'parent_revision': parent['revision'], 'parent_field': 'category_ref'})]
    preview = await run_for_phone(PHONE, 'reject-child', 'Crear categoría', provider=NativeScript([child]), now=NOW)
    rendered = await assemble_reply(PHONE, preview)
    await deliver(preview)
    result = await process_interaction(PHONE, 'reject-child-button', rendered.followup_messages[-1].buttons[0].id, 'button_reply')
    state = await assistant_state.read(accounts[1])
    parent = current_operation(state)
    assert parent['source_message_id'] == 'reject-parent' and parent['status'] == ('ready' if ready_parent else 'collecting')
    assert state['workspace']['last_confirmation'] is None
    assert bool(result.assistant_preview_delivery) == ready_parent
    with accounts[0]() as db:
        assert db.query(Categoria).filter_by(nombre='No crear').count() == 0
        assert db.query(LimiteCategoria).count() == 0


@pytest.mark.asyncio
async def test_editor_start_limit_opens_same_collecting_workspace(accounts, fixed_pilot_clock):
    from app.services.assistant_actions import process_interaction
    from app.services.assistant_transport import assemble_reply
    publish(accounts, 'limit.listed', {'start_node': 'start', 'nodes': [
        {'id': 'start', 'type': 'reply_button', 'body': '{summary}', 'options': [
            {'id': 'create', 'title': 'Crear límite', 'action': 'start_limit'}]}]})
    answer = await read('list_limits', message='start-limit-list')
    rendered = await assemble_reply(PHONE, answer)
    result = await process_interaction(PHONE, 'start-limit-button', rendered.followup_messages[-1].buttons[0].id, 'button_reply')
    operation = current_operation(await assistant_state.read(accounts[1]))
    assert operation['operation_key'] == 'create_limit' and operation['status'] == 'collecting'
    assert result.assistant_preview_delivery is None and result.event_key == 'limit.started'
