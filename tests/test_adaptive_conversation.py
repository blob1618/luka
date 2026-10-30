"""Explicit tool scenarios preserve financial effects after the global cut."""
from datetime import date
import json
import pytest
from app.models.database import AssistantWriteReceipt, LimiteCategoria, MovimientoFinanciero, Usuario
from app.services import assistant_state
from app.services.finance import FinanceService
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import register_now,visible_ref
from tests.test_assistant_stage4 import initial
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
async def test_amount_correction_recomputes_budget_without_duplicate_expense(ingress,accounts):
    with accounts[0]() as session:
        session.add(LimiteCategoria(usuario_id=accounts[1],categoria_id=accounts[4],cantidad_max=1100,moneda='ARS',inicio_periodo=date(2026,10,1),fin_periodo=date(2026,10,31)))
        session.commit()
        before=session.query(MovimientoFinanciero).count()
    await ingress.tool('find_movements',{'selector':{'kind':'all'},'description':'gasto 7','sort_by':'date_desc'})
    preview=await ingress.send('Corregir importe',steps=[lambda kw:[('update_movement',{'movement_ref':visible_ref(kw,'movement'),'changes':{'amount':'500'}})]])
    await ingress.confirm(preview)
    status=await ingress.tool('query_budget',{'currency':'ARS'})
    assert '1.221' in status.reply_text and '121' in status.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count()==before
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=accounts[1],descripcion='gasto 7').one().cantidad==500


@pytest.mark.asyncio
async def test_multi_movement_message_retains_distinct_ids_for_ordinal_correction(ingress,accounts):
    preview=await ingress.send('Dos registros',message_id='adaptive-batch',steps=[[('register_movement_batch',{'items':[register_now(description='primero',amount='100'),register_now(description='segundo',amount='200')]})]])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        rows=session.query(MovimientoFinanciero).filter(MovimientoFinanciero.whatsapp_message_id.like('adaptive-batch:batch:%')).all()
        ids={r.descripcion:r.id for r in rows}
        assert len(set(ids.values()))==2
    def edit(kwargs):
        ref=next(ref for ref,item in initial(kwargs)['references'].items() if item['kind']=='movement' and item['details']['description']=='primero')
        return [('update_movement',{'movement_ref':ref,'changes':{'amount':'125'}})]
    preview=await ingress.send('Cambiá sólo el primero a125',steps=[edit])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        assert session.get(MovimientoFinanciero,ids['primero']).cantidad==125
        assert session.get(MovimientoFinanciero,ids['segundo']).cantidad==200
        assert session.query(AssistantWriteReceipt).count()==2


@pytest.mark.asyncio
async def test_numeric_movement_choice_applies_only_to_shown_item(ingress,accounts):
    def options(kwargs):
        data=json.loads(next(m.content for m in kwargs['messages'] if m.role=='tool'))['data']
        return [('ask_clarification',{'reason_code':'ambiguous_reference','target_field':'movement','candidate_refs':[x['movement_ref'] for x in data['items']]})]
    await ingress.send('Elegí un gasto',steps=[[('find_movements',{'selector':{'kind':'all'},'sort_by':'date_desc','limit':2})],options])
    state=await assistant_state.read(accounts[1])
    selected=state['selection_options']['2']['ref']
    identifier=state['references'][selected]['payload']['id']
    preview=await ingress.send('2',steps=[lambda kw:[('annul_movements',{'movement_refs':[initial(kw)['selected_ref']]})]])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        removed=session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.is_not(None)).all()
        assert len(removed)==1 and str(removed[0].id)==identifier
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=accounts[2]).one().anulado_en is None


@pytest.fixture
def mutation_db(isolated_db):
    with isolated_db() as session:
        user=Usuario(nombre='Batch owner',email='batch-owner@example.invalid',whatsapp_id='batch-owner')
        session.add(user)
        session.commit()
        yield session,user


def shown(item):
    return {'id':str(item.id),'amount':str(item.cantidad),'currency':item.moneda,'description':item.descripcion,'date':str(item.fecha_movimiento)}


def test_batch_delete_rejects_stale_snapshot_without_partial_change(mutation_db):
    session,user=mutation_db
    for description in ('tv','ventilador'):
        session.add(MovimientoFinanciero(usuario_id=user.id,tipo='egreso',cantidad=1000,moneda='ARS',descripcion=description,fecha_movimiento=date(2026,9,17)))
    session.commit()
    selected=[shown(item) for item in FinanceService.find_movement_candidates(user.whatsapp_id)]
    session.query(MovimientoFinanciero).filter_by(descripcion='tv').one().cantidad=2000
    session.commit()
    assert FinanceService.annul_movements(user.whatsapp_id,selected).status=='stale_context'
    session.expire_all()
    assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.is_not(None)).count()==0


def test_batch_delete_rejects_other_users_movement_atomically(mutation_db):
    session,user=mutation_db
    other=Usuario(nombre='Other',email='batch-other@example.invalid',whatsapp_id='batch-other')
    session.add(other)
    session.flush()
    for owner,description in [(user,'tv'),(other,'ventilador')]:
        session.add(MovimientoFinanciero(usuario_id=owner.id,tipo='egreso',cantidad=1000,moneda='ARS',descripcion=description,fecha_movimiento=date(2026,9,17)))
    session.commit()
    selected=[shown(FinanceService.find_movement_candidates(owner.whatsapp_id)[0]) for owner in (user,other)]
    assert FinanceService.annul_movements(user.whatsapp_id,selected).status=='not_found'
    session.expire_all()
    assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.is_not(None)).count()==0
