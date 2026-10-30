"""Explicit tool scenarios preserve financial effects after the global cut."""
import pytest
from app.models.database import AssistantWriteReceipt, Billetera, MovimientoFinanciero
from app.services import assistant_state
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import register_now
from tests.test_assistant_stage4 import initial
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
@pytest.mark.parametrize('with_category',[False,True])
async def test_ambiguity_prompts_wallet_selection_zero_writes(ingress,accounts,with_category):
    with accounts[0]() as session:
        session.add(Billetera(usuario_id=accounts[1],nombre='Segunda ARS',moneda='ARS'))
        session.commit()
        before=session.query(MovimientoFinanciero).count()
    def plan(kwargs):
        patch={'category_ref':initial(kwargs)['categories'][0]['ref']} if with_category else {}
        return [('register_movement',register_now(**patch))]
    question=await ingress.send('Gasto',steps=[plan])
    assert 'billetera' in question.reply_text and 'Caja A' in question.reply_text and 'Segunda ARS' in question.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count()==before
    def select(kwargs):
        op=initial(kwargs)['operations'][0]
        ref=next(item['ref'] for item in initial(kwargs)['selection_options'].values() if item['label']=='Segunda ARS')
        return [('operation_amend',{'ref':op['ref'],'expected_revision':op['revision'],'changes':{'wallet_ref':ref}})]
    preview=await ingress.send('Segunda ARS',steps=[select])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        new=session.query(MovimientoFinanciero).filter(MovimientoFinanciero.whatsapp_message_id.like('stage5-%')).one()
        assert new.billetera_id==session.query(Billetera).filter_by(nombre='Segunda ARS').one().id and new.cantidad==106


@pytest.mark.asyncio
async def test_cancel_wallet_selection_clears_state(ingress,accounts):
    with accounts[0]() as session:
        session.add(Billetera(usuario_id=accounts[1],nombre='Segunda',moneda='ARS'))
        session.commit()
    await ingress.tool('register_movement',register_now())
    await ingress.send('cancelar')
    state=await assistant_state.read(accounts[1])
    assert state['workspace']['active_operation_id'] is None and not state.get('selection_options')
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['deleted','currency','owner'])
async def test_multiturn_wallet_changed_after_preview_prevents_write(ingress,accounts,change):
    def plan(kwargs):
        wallet=next(w['ref'] for w in initial(kwargs)['wallets'] if w['name']=='Caja A')
        return [('register_movement',register_now(wallet_ref=wallet))]
    preview=await ingress.send('Gasté',steps=[plan])
    with accounts[0]() as session:
        row=session.get(Billetera,accounts[3])
        if change=='deleted':
            # Existing movements reference this wallet; a deletion flag is not in this schema.
            session.query(MovimientoFinanciero).filter_by(billetera_id=row.id).delete()
            session.delete(row)
        elif change=='currency':
            row.moneda='USD'
        else:
            row.usuario_id=accounts[2]
        session.commit()
        before=session.query(MovimientoFinanciero).count()
    rejected=await ingress.confirm(preview)
    assert 'No hice cambios' in rejected.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count()==before and session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_create_wallet_intent(ingress,accounts):
    preview=await ingress.tool('create_wallet',{'name':'Nueva','currency':'USD'})
    with accounts[0]() as session:
        assert session.query(Billetera).filter_by(nombre='Nueva').count()==0
    await ingress.confirm(preview)
    with accounts[0]() as session:
        assert session.query(Billetera).filter_by(usuario_id=accounts[1],nombre='Nueva',moneda='USD').count()==1


@pytest.mark.asyncio
async def test_list_wallets_intent_and_isolation(ingress):
    result=await ingress.tool('wallet_balances')
    assert 'Caja A' in result.reply_text and 'USD A' in result.reply_text
    assert 'Caja privada B' not in result.reply_text and '999.999' not in result.reply_text
