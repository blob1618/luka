"""Functional ports: explicit tools replace classifier repair and financial pending flags."""
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest

from app.api.whatsapp import WhatsAppReplyButtons, WhatsAppImage
from app.models.database import AssistantWriteReceipt, Billetera, Categoria, LimiteCategoria, MovimientoFinanciero, Recordatorio
from app.services import assistant_state
from app.services.currency import QuoteType, QuoteUnavailableError, UsdQuote
from app.services.llm import LLMService
from tests import test_assistant_stage5 as ingress_cases
from tests.test_assistant_stage5 import register_now, visible_ref
from tests.test_assistant_stage4 import initial, publish
from tests.test_assistant_pilot import NOW, amend_active

accounts = ingress_cases.accounts
ingress = ingress_cases.ingress


@pytest.mark.asyncio
async def test_category_choice_then_registration_uses_single_wallet(ingress, accounts):
    await ingress.tool('register_movement', register_now(amount=None))
    await ingress.tool('ask_clarification', {'target_field':'category','reason_code':'ambiguous_reference'})
    state = await assistant_state.read(accounts[1])
    number, choice = next((n, x) for n, x in state['selection_options'].items() if x['label']=='Comida')
    category_ref = choice['ref']
    await ingress.send(number, steps=[amend_active({'category_ref':category_ref})])
    preview = await ingress.send('El importe es 106', steps=[amend_active({'amount':'106'})])
    assert preview.assistant_preview_delivery and 'Caja A' in preview.reply_text
    await ingress.confirm(preview)
    with accounts[0]() as session:
        row = session.query(MovimientoFinanciero).filter_by(whatsapp_message_id='stage5-1').one()
        assert row.billetera_id==accounts[3] and row.categoria_id==accounts[4] and row.cantidad==106


@pytest.mark.asyncio
async def test_category_child_preserves_single_wallet(ingress, accounts):
    await ingress.tool('register_movement', register_now(requested_category='Viajes nueva'))
    def child(kwargs):
        parent=next(op for op in initial(kwargs)['operations'] if op['operation_key']=='register_movement')
        return [('create_category',{'name':'Viajes nueva','parent_ref':parent['ref'],
            'parent_revision':parent['revision'],'parent_field':'category_ref'})]
    child_preview = await ingress.send('Crear esa categoría', steps=[child])
    parent_preview = await ingress.confirm(child_preview)
    assert parent_preview.assistant_preview_delivery and 'Caja A' in parent_preview.reply_text
    state = await assistant_state.read(accounts[1])
    parent=state['workspace']['operations'][state['workspace']['active_operation_id']]
    assert parent['operation_key']=='register_movement' and parent['prepared_payload']['wallet_id']==str(accounts[3])
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0
        category=session.query(Categoria).filter_by(usuario_id=accounts[1],nombre='Viajes nueva').one()
        category_id=category.id
    await ingress.confirm(parent_preview)
    with accounts[0]() as session:
        row=session.query(MovimientoFinanciero).filter_by(whatsapp_message_id='stage5-1').one()
        assert row.billetera_id==accounts[3] and row.categoria_id==category_id and row.cantidad==106


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['stale', 'unknown'])
async def test_multiple_annulment_prevalidates_every_snapshot_without_partial_write(ingress, accounts, kind):
    await ingress.tool('find_movements', {'selector':{'kind':'all'}, 'sort_by':'date_desc', 'currency':'ARS'})
    state = await assistant_state.read(accounts[1])
    refs = state['last_shown'][:2]
    if kind=='stale':
        with accounts[0]() as session:
            session.get(MovimientoFinanciero, UUID(state['references'][refs[1]]['payload']['id'])).cantidad = 999
            session.commit()
    else:
        refs[1] = 'ref_'+'0'*32
    result = await ingress.tool('annul_movements', {'movement_refs':refs})
    assert not result.assistant_preview_delivery
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.is_not(None)).count()==0
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_batch_annulment_stale_after_preview_rolls_back_all_rows(ingress, accounts):
    await ingress.tool('find_movements', {'selector':{'kind':'all'}, 'sort_by':'date_desc', 'currency':'ARS'})
    state = await assistant_state.read(accounts[1])
    refs=state['last_shown'][:2]
    preview = await ingress.tool('annul_movements', {'movement_refs':refs})
    with accounts[0]() as session:
        session.get(MovimientoFinanciero,UUID(state['references'][refs[1]]['payload']['id'])).cantidad = 999
        session.commit()
    rejected = await ingress.confirm(preview)
    assert 'No hice cambios' in rejected.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.is_not(None)).count()==0
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_limit_multi_selection_deletes_only_selected_owned_months(ingress, accounts):
    factory,owner,other,_,category=accounts
    other_category=uuid4()
    foreign_category=uuid4()
    # Periods must stay open: the service refuses to delete a limit whose month already ended.
    first = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    second = (first + timedelta(days=32)).replace(day=1)
    with factory() as session:
        session.add_all([Categoria(id=other_category,usuario_id=owner,nombre='Transporte'),Categoria(id=foreign_category,usuario_id=other,nombre='Ajena')])
        for uid,cid,start in [(owner,category,first),(owner,category,second),(owner,other_category,first),(other,foreign_category,first)]:
            session.add(LimiteCategoria(usuario_id=uid,categoria_id=cid,cantidad_max=5000,moneda='ARS',inicio_periodo=start,fin_periodo=start.replace(day=28)))
        session.commit()
    await ingress.tool('list_limits')
    def selected(kwargs):
        refs=[ref for ref,item in initial(kwargs)['references'].items() if item['kind']=='limit' and item['details']['category']=='Comida']
        assert len(refs)==2
        return [('delete_limits',{'limit_refs':refs})]
    preview=await ingress.send('Eliminá ambos de Comida',steps=[selected])
    await ingress.confirm(preview)
    with factory() as session:
        assert session.query(LimiteCategoria).filter_by(usuario_id=owner,categoria_id=category).count()==0
        assert session.query(LimiteCategoria).count()==2


@pytest.mark.asyncio
@pytest.mark.parametrize('patch,question',[({'amount':None},'importe'),({'month':None},'mes'),({'year':None},'año'),({'category_ref':None},'categoría')])
async def test_limit_collecting_retains_explicit_fields_and_completes_on_amend(ingress, accounts, patch, question):
    def start(kwargs):
        cat=next(x['ref'] for x in initial(kwargs)['categories'] if x['name']=='Comida')
        return [('create_limit',{'category_ref':cat,'amount':'5000','month':11,'year':2026,'currency':'ARS',**patch})]
    pending=await ingress.send('Crear límite',steps=[start])
    assert question in pending.reply_text
    def complete(kwargs):
        category=next(x['ref'] for x in initial(kwargs)['categories'] if x['name']=='Comida')
        op=initial(kwargs)['operations'][0]
        changes={key:{'amount':'5000','month':11,'year':2026,'category_ref':category}[key] for key in patch}
        return [('operation_amend',{'ref':op['ref'],'expected_revision':op['revision'],'changes':changes})]
    preview=await ingress.send('Completar dato',steps=[complete])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        row=session.query(LimiteCategoria).one()
        assert row.cantidad_max==5000 and row.inicio_periodo==date(2026,11,1) and row.moneda=='ARS'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('title','Internet nuevo'),('day',20),('amount','1800'),('currency','USD')])
async def test_reminder_update_changes_only_explicit_field(ingress, accounts, field, value):
    preview=await ingress.tool('create_reminder',{'title':'Internet','day':15,'amount':'1500','currency':'ARS'})
    await ingress.confirm(preview)
    preview=await ingress.send('Modificar sólo el dato indicado',steps=[lambda kw:[('update_reminder',{'reminder_ref':visible_ref(kw,'reminder'),field:value})]])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        row=session.query(Recordatorio).one()
        expected={'titulo':'Internet','dia_del_mes':15,'monto':Decimal(1500),'moneda':'ARS'}
        expected[{'title':'titulo','day':'dia_del_mes','amount':'monto','currency':'moneda'}[field]]=Decimal(value) if field=='amount' else value
        assert {key:getattr(row,key) for key in expected}==expected


@pytest.mark.asyncio
async def test_duplicate_reminder_asks_new_title_then_delete_preserves_other_user(ingress, accounts):
    preview=await ingress.tool('create_reminder',{'title':'Internet','day':15})
    await ingress.confirm(preview)
    duplicate=await ingress.tool('create_reminder',{'title':'Internet','day':20})
    assert 'nombre diferente' in duplicate.reply_text and not duplicate.assistant_preview_delivery
    renamed=await ingress.send('Llamalo Internet2',steps=[amend_active({'title':'Internet2'})])
    await ingress.confirm(renamed)
    await ingress.tool('list_reminders',{'include_paused':True})
    def remove(kwargs):
        ref=next(ref for ref,item in initial(kwargs)['references'].items() if item['kind']=='reminder' and item['details']['titulo']=='Internet2')
        return [('change_reminder_state',{'reminder_ref':ref,'state':'delete'})]
    preview=await ingress.send('Eliminá Internet2',steps=[remove])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        rows={r.titulo:r.estado for r in session.query(Recordatorio)}
        assert rows=={'Internet':'activo','Internet2':'eliminado'}


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled',[False,True])
async def test_proactive_preference_needs_its_own_confirmation(ingress, accounts, enabled):
    from app.models.database import Usuario
    with accounts[0]() as session:
        session.get(Usuario,accounts[1]).proactivo_habilitado=not enabled
        session.commit()
    preview=await ingress.tool('set_proactive_prompts',{'enabled':enabled})
    with accounts[0]() as session:
        assert session.get(Usuario,accounts[1]).proactivo_habilitado is not enabled
    await ingress.confirm(preview)
    with accounts[0]() as session:
        assert session.get(Usuario,accounts[1]).proactivo_habilitado is enabled


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['no_data','quotes_missing','quotes_stale'])
async def test_financial_presenter_keeps_quote_availability_and_accounts_separate(ingress, accounts, monkeypatch, kind):
    from app.services.assistant_capabilities import financial_reads
    observed=[]
    async def render(name,facts,requested):
        observed.append((name,facts,requested))
        return '\n'.join(f"{f['label']}: {f['value']}" for f in facts)
    monkeypatch.setattr(LLMService,'render_financial_reply',render)
    if kind=='no_data':
        with accounts[0]() as session:
            session.query(MovimientoFinanciero).filter_by(usuario_id=accounts[1]).delete()
            session.commit()
    else:
        from app.models.database import Billetera
        with accounts[0]() as session:
            usd=session.query(Billetera).filter_by(usuario_id=accounts[1],moneda='USD').one()
            session.add(MovimientoFinanciero(usuario_id=accounts[1],billetera_id=usd.id,tipo='ingreso',cantidad=10,moneda='USD',descripcion='Propio',fecha_movimiento=date(2026,10,1)))
            session.commit()
    if kind=='quotes_stale':
        quote=UsdQuote(tipo=QuoteType.BLUE,compra=Decimal('1000.12345'),venta=Decimal('1100.54321'),fuente='Fuente sintética',as_of=NOW,fetched_at=NOW,stale=True)
        monkeypatch.setattr(financial_reads,'get_usd_quotes',AsyncMock(return_value={QuoteType.BLUE:quote}))
    else:
        monkeypatch.setattr(financial_reads,'get_usd_quotes',AsyncMock(side_effect=QuoteUnavailableError('synthetic')))
    reply=await ingress.tool('query_net_worth',{'quote_types':['blue']})
    assert '987' not in reply.reply_text and '999.999' not in reply.reply_text
    if kind=='quotes_missing':
        assert 'no disponible' in reply.reply_text
    if kind=='quotes_stale':
        assert 'desactualizada' in reply.reply_text
    assert observed[0][0]=='net_worth'


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments,expected',[({'term':'interés compuesto'},'Ejemplo ilustrativo'),({'term':'interés'},'simple o interés compuesto'),({'term':'TIR'},'No tengo una definición verificada'),({'term':'inflación actual'},'No puedo confirmar')])
async def test_glossary_tool_is_read_only_and_does_not_fabricate_current_values(ingress, accounts, arguments, expected):
    reply=await ingress.tool('financial_education',arguments)
    assert expected in reply.reply_text
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
@pytest.mark.parametrize('spec,phrase,expected',[({'kind':'literal','evidence_span':'29/02/2024'},'El29/02/2024','29/02/2024'),({'kind':'relative_days','evidence_span':'ayer','day_offset':-1},'Gasté ayer','26/10/2026'),({'kind':'literal','evidence_span':'15/11/2026'},'El15/11/2026','futura')])
async def test_movement_date_evidence_reaches_backend_preview_without_clamping(ingress, spec, phrase, expected):
    preview=await ingress.tool('register_movement',register_now(date_spec=spec),phrase)
    assert preview.assistant_preview_delivery and expected in preview.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,extra',[('monthly',{'chart_start_month':'2026-01'}),('movement_comparison',{'movement_type':'both'}),('month_comparison',{'chart_months':['2026-09','2026-10']})])
async def test_chart_modes_emit_real_png_and_no_financial_confirmation(ingress, mode, extra):
    result=await ingress.tool('movement_chart',{'chart_mode':mode,'chart_currency':'ARS',**extra})
    images=[m for m in result.followup_messages if isinstance(m,WhatsAppImage)]
    assert images and all(m.content.startswith(b'\x89PNG') for m in images)
    assert not result.assistant_preview_delivery


@pytest.mark.asyncio
async def test_published_confirm_button_binding_is_specific_to_proposal_at_ingress(ingress, accounts):
    publish(accounts,'category.confirmation_required',{'start_node':'same','nodes':[{'id':'same','type':'reply_button','body':'Crear {category}',
        'options':[{'id':'yes','title':'Crear','action':'confirm_category'}]}]})
    a=await ingress.tool('create_category',{'name':'CategoríaA'})
    b=await ingress.tool('create_category',{'name':'CategoríaB'})
    def editor_button(result):return next(m.buttons[0].id for m in result.followup_messages if isinstance(m,WhatsAppReplyButtons) and m.buttons[0].title=='Crear')
    stale=await ingress.send('Crear',option_id=editor_button(a))
    assert 'ya no está disponible' in stale.reply_text
    valid=await ingress.send('Crear',option_id=editor_button(b))
    assert 'creada' in valid.reply_text
    with accounts[0]() as session:
        assert session.query(Categoria).filter_by(nombre='CategoríaA').count()==0
        assert session.query(Categoria).filter_by(nombre='CategoríaB').count()==1


@pytest.mark.asyncio
async def test_new_operation_interrupts_selection_but_old_button_cannot_choose_new_task(ingress, accounts):
    preview=await ingress.tool('register_movement',register_now())
    await ingress.tool('list_limits')
    rejected=await ingress.confirm(preview)
    assert 'vigente' in rejected.reply_text
    new=await ingress.tool('create_reminder',{'title':'Nuevo','day':10})
    assert new.assistant_preview_delivery
    state=await assistant_state.read(accounts[1])
    assert any(op['operation_key']=='register_movement' and op['status']=='suspended' for op in state['workspace']['operations'].values())
    await ingress.confirm(new)
    with accounts[0]() as session:
        assert session.query(Recordatorio).count()==1 and session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_recurring_unknown_callback_verb_cannot_reject_or_accept(ingress,accounts):
    from app.models.database import CandidatoGastoRecurrente
    from datetime import timedelta
    with accounts[0]() as session:
        row=CandidatoGastoRecurrente(usuario_id=accounts[1],concepto='Internet',descripcion_normalizada='internet',
            patron_hash='c'*64,dia_estimado=15,monto_estimado=1000,moneda='ARS',estado='pendiente',
            propuesta_en=NOW,ultima_fecha_movimiento=NOW.date()-timedelta(days=30),
            proxima_fecha_estimada=NOW.date()+timedelta(days=5),evidencia_movimiento_ids=[])
        session.add(row)
        session.commit()
        identifier=row.id
    result=await ingress.send('Opción inválida',option_id='rec_cand:invented:'+str(identifier))
    assert 'inválida' in result.reply_text
    with accounts[0]() as session:
        assert session.get(CandidatoGastoRecurrente,identifier).estado=='pendiente'
        assert session.query(Recordatorio).count()==0


@pytest.mark.asyncio
async def test_budget_feedback_and_recurring_offer_keep_amounts_and_both_actions(ingress, accounts):
    from datetime import timedelta
    from app.models.database import CandidatoGastoRecurrente
    from app.services.recurring_expense import calculate_pattern_hash, normalize_description
    from app.services.webhook_idempotency import _visible_reply_text
    factory, owner, _, _, category = accounts
    with factory() as session:
        session.add(LimiteCategoria(usuario_id=owner, categoria_id=category, cantidad_max=1200, moneda='ARS',
            inicio_periodo=date(2026, 10, 1), fin_periodo=date(2026, 10, 31)))
        session.add(CandidatoGastoRecurrente(usuario_id=owner, categoria_id=category, concepto='Internet',
            descripcion_normalizada=normalize_description('Internet'),
            patron_hash=calculate_pattern_hash(normalize_description('Internet'), category, 'ARS'),
            dia_estimado=15, monto_estimado=106, moneda='ARS', estado='pendiente',
            ultima_fecha_movimiento=NOW.date()-timedelta(days=30),
            proxima_fecha_estimada=NOW.date()+timedelta(days=15), evidencia_movimiento_ids=[]))
        session.commit()
    def registration(kwargs):
        ref = next(c['ref'] for c in initial(kwargs)['categories'] if c['name'] == 'Comida')
        return [('register_movement', register_now(description='Internet', category_ref=ref))]
    preview = await ingress.send('Pagué106enInternet', steps=[registration])
    result = await ingress.confirm(preview)
    visible = _visible_reply_text(result)
    assert all(amount in visible for amount in ['106', '934', '266', '1.200'])
    assert 'Noté que solés pagar' in visible
    actions = [b.id for m in [result.reply_message, *result.followup_messages]
               if isinstance(m, WhatsAppReplyButtons) for b in m.buttons]
    assert any(a.startswith('movement:change_category:') for a in actions)
    assert any(a.startswith('rec_cand:accept:') for a in actions)
    with factory() as session:
        assert session.query(AssistantWriteReceipt).count() == 1


@pytest.mark.asyncio
async def test_explicit_wallet_ref_uses_single_wallet(ingress, accounts):
    def plan(kwargs):
        wallet = next(w['ref'] for w in initial(kwargs)['wallets'] if w['name'] == 'Caja A')
        return [('register_movement', {**register_now(), 'wallet_ref': wallet})]
    preview = await ingress.send('Gasté 106 en taxi', steps=[plan])
    assert preview.assistant_preview_delivery and 'Caja A' in preview.reply_text
    await ingress.confirm(preview)
    with accounts[0]() as session:
        row = session.query(MovimientoFinanciero).filter_by(whatsapp_message_id='stage5-1').one()
        assert row.billetera_id == accounts[3]


@pytest.mark.asyncio
async def test_update_repeating_current_currency_with_single_wallet(ingress, accounts):
    """Re-sending the current currency must not turn into a wallet failure at confirmation."""
    def registration(kwargs):
        wallet = next(w for w in initial(kwargs)['wallets'] if w['name'] == 'Caja A')
        return [('register_movement', {**register_now(), 'wallet_ref': wallet['ref']})]
    await ingress.confirm(await ingress.send('Gasté 106 en taxi', steps=[registration]))
    await ingress.send('Mostrá mis gastos', steps=[[('find_movements', {'selector': {'kind': 'all'}, 'sort_by': 'date_desc', 'currency': 'ARS'})]])

    def update(kwargs):
        return [('update_movement', {'movement_ref': visible_ref(kwargs, 'movement'), 'changes': {'amount': '1200', 'currency': 'ARS'}})]
    preview = await ingress.send('Cambiá el importe a 1200 ARS', steps=[update])
    assert preview.assistant_preview_delivery and 'importe' in preview.reply_text
    assert '1200.0000000000' not in preview.reply_text and 'amount:' not in preview.reply_text
    result = await ingress.confirm(preview)
    assert 'No se actualizó' not in result.reply_text
    with accounts[0]() as session:
        taxi = session.query(MovimientoFinanciero).filter_by(descripcion='taxi').one()
        assert Decimal(taxi.cantidad) == Decimal('1200')


@pytest.mark.asyncio
async def test_update_currency_change_uses_existing_usd_wallet(ingress, accounts):
    await ingress.confirm(await ingress.tool('register_movement', register_now()))
    await ingress.send('Mostrá mis gastos', steps=[[('find_movements', {'selector': {'kind': 'all'}, 'sort_by': 'date_desc', 'currency': 'ARS'})]])

    def update(kwargs):
        return [('update_movement', {'movement_ref': visible_ref(kwargs, 'movement'), 'changes': {'currency': 'USD'}})]
    preview = await ingress.send('Pasalo a dólares', steps=[update])
    assert preview.assistant_preview_delivery and 'USD A' in preview.reply_text
    result = await ingress.confirm(preview)
    assert 'No se actualizó' not in result.reply_text
    with accounts[0]() as session:
        usd = session.query(Billetera).filter_by(usuario_id=accounts[1], nombre='USD A').one()
        row = session.query(MovimientoFinanciero).filter_by(whatsapp_message_id='stage5-1').one()
        assert row.moneda == 'USD' and row.billetera_id == usd.id


@pytest.mark.asyncio
async def test_update_invalid_date_is_reported_as_invalid_not_stale(ingress, accounts):
    await ingress.confirm(await ingress.tool('register_movement', register_now()))
    await ingress.send('Mostrá mis gastos', steps=[[('find_movements', {'selector': {'kind': 'all'}, 'sort_by': 'date_desc', 'currency': 'ARS'})]])

    def update(kwargs):
        return [('update_movement', {'movement_ref': visible_ref(kwargs, 'movement'),
                                     'changes': {'date_spec': {'kind': 'literal', 'evidence_span': '31/02/2027'}}})]
    response = await ingress.send('Cambiá la fecha al 31/02/2027', steps=[update])
    assert not response.assistant_preview_delivery
    assert 'fecha' in response.reply_text and 'revisión' not in response.reply_text


@pytest.mark.asyncio
async def test_annul_preview_formats_amount_and_date(ingress, accounts):
    await ingress.confirm(await ingress.tool('register_movement', register_now()))
    await ingress.send('Mostrá mis gastos', steps=[[('find_movements', {'selector': {'kind': 'all'}, 'sort_by': 'date_desc', 'currency': 'ARS'})]])

    def annul(kwargs):
        return [('annul_movements', {'movement_refs': [visible_ref(kwargs, 'movement')]})]
    preview = await ingress.send('Anulá ese gasto', steps=[annul])
    assert preview.assistant_preview_delivery
    assert '106,00' in preview.reply_text and '106.0000000000' not in preview.reply_text
    with accounts[0]() as session:
        expected = session.query(MovimientoFinanciero).filter_by(descripcion='taxi').one().fecha_movimiento.strftime('%d/%m/%Y')
    assert expected in preview.reply_text


@pytest.mark.asyncio
async def test_reminder_preview_and_confirmation_use_spanish_labels(ingress, accounts):
    preview = await ingress.tool('create_reminder', {'title': 'Pagar Internet', 'day': 15,
                                                     'amount': '1500', 'currency': 'ARS'})
    assert preview.assistant_preview_delivery
    assert preview.reply_text.startswith('Crear recordatorio') and 'create:' not in preview.reply_text
    result = await ingress.confirm(preview)
    assert 'creado' in result.reply_text and 'created' not in result.reply_text


@pytest.mark.asyncio
async def test_modificar_without_active_operation_asks_for_a_proposal(ingress, accounts):
    response = await ingress.send('modificar', steps=[])
    assert 'No hay una propuesta activa' in response.reply_text
    assert 'Guardé el borrador' not in response.reply_text


@pytest.mark.asyncio
async def test_typed_confirmar_confirms_the_delivered_preview(ingress, accounts):
    preview = await ingress.tool('register_movement', register_now())
    assert preview.assistant_preview_delivery
    await ingress.send('Confirmar', steps=[])
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).filter_by(descripcion='taxi').count() == 1


@pytest.mark.asyncio
async def test_typed_cancela_cancels_the_active_draft(ingress, accounts):
    preview = await ingress.tool('register_movement', register_now())
    assert preview.assistant_preview_delivery
    response = await ingress.send('Cancelá', steps=[])
    assert 'Cancelé la propuesta' in response.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).filter_by(descripcion='taxi').count() == 0


@pytest.mark.asyncio
async def test_amount_sort_without_currency_asks_in_plain_words(ingress, accounts):
    response = await ingress.send('¿Cuál fue mi gasto más grande?',
        steps=[[('find_movements', {'selector': {'kind': 'all'}, 'sort_by': 'amount_desc'})]])
    assert 'pesos o dólares' in response.reply_text
    assert 'Elegí una moneda' not in response.reply_text


@pytest.mark.asyncio
async def test_fourth_draft_is_rejected_with_an_actionable_list(ingress, accounts):
    await ingress.tool('register_movement', register_now(description='luz'), text='Gasté 2500 de luz')
    await ingress.tool('register_movement', register_now(description='agua'), text='Gasté 800 de agua')
    await ingress.tool('register_movement', register_now(description='gas'), text='Gasté 600 de gas')
    fourth = await ingress.tool('register_movement', register_now(description='internet'),
                                text='Gasté 900 de internet')
    assert not fourth.assistant_preview_delivery
    assert 'demasiadas' in fourth.reply_text
    assert 'luz' in fourth.reply_text and 'cancelá' in fourth.reply_text.casefold()
    state = await assistant_state.read(accounts[1])
    operations = list(state['workspace']['operations'].values())
    live = [op for op in operations if op['status'] in {'collecting', 'ready', 'suspended'}]
    assert len(live) == 3 and not any(op['status'] == 'expired' for op in operations)


@pytest.mark.asyncio
async def test_cancel_without_active_operation_lists_open_drafts(ingress, accounts):
    await ingress.tool('register_movement', register_now(description='luz'), text='Gasté 2500 de luz')
    await ingress.send('saldos', steps=[[('wallet_balances', {})]])
    response = await ingress.send('cancelá', steps=[])
    assert 'Borradores abiertos' in response.reply_text and 'luz' in response.reply_text


@pytest.mark.asyncio
async def test_relative_day_labeled_as_literal_still_prepares(ingress, accounts):
    """Producción: el modelo manda «ayer» como literal y el registro no puede bloquearse."""
    preview = await ingress.tool('register_movement',
        register_now(date_spec={'kind': 'literal', 'evidence_span': 'ayer'}), 'Gasté 106 ayer')
    assert preview.assistant_preview_delivery
    state = await assistant_state.read(accounts[1])
    reference = date.fromisoformat(next(iter(state['workspace']['operations'].values()))['reference_day'])
    assert (reference - timedelta(days=1)).strftime('%d/%m/%Y') in preview.reply_text


@pytest.mark.asyncio
async def test_non_date_literal_still_asks_with_a_usable_question(ingress, accounts):
    response = await ingress.tool('register_movement',
        register_now(date_spec={'kind': 'literal', 'evidence_span': '2000'}), 'Gasté 2000 en uber')
    assert not response.assistant_preview_delivery
    assert 'hoy' in response.reply_text and 'ayer' in response.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).filter_by(descripcion='taxi').count() == 0
