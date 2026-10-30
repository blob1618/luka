"""Functional Stage5 ports through explicit tools, real services and synthetic delivery."""
from datetime import date
import pytest
from app.models.database import LimiteCategoria
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import visible_ref
from tests.test_assistant_stage4 import initial
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
async def test_create_limit_registers_and_stores_last(ingress,accounts):
    def plan(kwargs):return [('create_limit',{'category_ref':next(c['ref'] for c in initial(kwargs)['categories'] if c['name']=='Comida'),'amount':'5000','currency':'ARS','month':11,'year':2026})]
    preview=await ingress.send('Límite',steps=[plan])
    with accounts[0]() as session:
        assert session.query(LimiteCategoria).count()==0
    result=await ingress.confirm(preview)
    assert result.event_key=='limit.created'
    with accounts[0]() as session:
        row=session.query(LimiteCategoria).one()
        assert row.cantidad_max==5000 and row.inicio_periodo==date(2026,11,1)


@pytest.mark.asyncio
async def test_create_limit_past_month_asks_year_confirmation(ingress,accounts):
    def plan(kwargs):return [('create_limit',{'category_ref':next(c['ref'] for c in initial(kwargs)['categories'] if c['name']=='Comida'),'amount':'5000','currency':'ARS','month':9})]
    result=await ingress.send('Límite septiembre',steps=[plan])
    assert 'año' in result.reply_text and result.event_key=='limit.year_confirmation' and not result.assistant_preview_delivery
    with accounts[0]() as session:
        assert session.query(LimiteCategoria).count()==0


@pytest.mark.asyncio
@pytest.mark.parametrize('changes',[{'amount':'8000'},{'month':12,'year':2026},{'currency':'USD'}])
async def test_change_limit_edits_last_created(ingress,accounts,changes):
    await test_create_limit_registers_and_stores_last(ingress,accounts)
    await ingress.tool('list_limits')
    preview=await ingress.send('Cambiar límite',steps=[lambda kw:[('change_limit',{'limit_ref':visible_ref(kw,'limit'),**changes})]])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        rows=session.query(LimiteCategoria).all()
        assert any(row.cantidad_max==int(changes.get('amount','5000')) and row.moneda==changes.get('currency','ARS') and row.inicio_periodo.month==changes.get('month',11) for row in rows)


@pytest.mark.asyncio
async def test_list_limits_empty(ingress):
    result=await ingress.tool('list_limits')
    assert 'No hay límites' in result.reply_text


@pytest.mark.asyncio
async def test_delete_limit_single(ingress,accounts):
    await test_create_limit_registers_and_stores_last(ingress,accounts)
    await ingress.tool('list_limits')
    preview=await ingress.send('Eliminar límite',steps=[lambda kw:[('delete_limits',{'limit_refs':[visible_ref(kw,'limit')]})]])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        assert session.query(LimiteCategoria).count()==0


@pytest.mark.asyncio
async def test_awaiting_limit_data_cancel(ingress,accounts):
    await ingress.tool('create_limit',{'amount':'1000'})
    result=await ingress.send('cancelar')
    assert 'Cancelé' in result.reply_text
    with accounts[0]() as session:
        assert session.query(LimiteCategoria).count()==0
