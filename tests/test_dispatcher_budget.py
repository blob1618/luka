"""Functional Stage5 ports through explicit tools, real services and synthetic delivery."""
from datetime import date
import pytest
from app.models.database import AssistantWriteReceipt, LimiteCategoria
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import register_now
from tests.test_assistant_stage4 import initial,publish
accounts=scenarios.accounts
ingress=scenarios.ingress


async def seed_budget(accounts,amount=1100):
    with accounts[0]() as session:
        session.add(LimiteCategoria(usuario_id=accounts[1],categoria_id=accounts[4],cantidad_max=amount,moneda='ARS',inicio_periodo=date(2026,10,1),fin_periodo=date(2026,10,31)))
        session.commit()


@pytest.mark.asyncio
async def test_budget_query_for_category_returns_consumed_and_available(ingress,accounts):
    await seed_budget(accounts,1100)
    def plan(kwargs):
        ref=next(x['ref'] for x in initial(kwargs)['categories'] if x['name']=='Comida')
        return [('query_budget',{'category_ref':ref,'currency':'ARS'})]
    result=await ingress.send('Presupuesto',steps=[plan,scenarios.present_all])
    assert all(value in result.reply_text for value in ['828','272','1.100'])
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_budget_query_without_category_lists_all_budgets(ingress,accounts):
    await seed_budget(accounts)
    result=await ingress.tool('query_budget',{'currency':'ARS'})
    assert 'Comida' in result.reply_text and '828' in result.reply_text


@pytest.mark.asyncio
async def test_budget_query_reports_missing_limit(ingress):
    result=await ingress.tool('query_budget',{'currency':'ARS'})
    assert 'No hay presupuestos' in result.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize('batch',[False,True])
async def test_batch_of_expenses_warns_once_for_the_same_80_percent_crossing(ingress,accounts,batch):
    await seed_budget(accounts)
    def plan(kwargs):
        ref=next(x['ref'] for x in initial(kwargs)['categories'] if x['name']=='Comida')
        item=register_now(category_ref=ref)
        return [('register_movement_batch',{'items':[item,dict(item,description='segundo')]})] if batch else [('register_movement',item)]
    preview=await ingress.send('Gastos',steps=[plan])
    assert not preview.budget_threshold_alerts
    confirmed=await ingress.confirm(preview)
    assert len(confirmed.budget_threshold_alerts)==1
    assert confirmed.budget_threshold_alerts[0].spent_amount==(1040 if batch else 934)
    assert 'Gastaste' not in confirmed.reply_text
    replay=await ingress.confirm(preview)
    assert not replay.budget_threshold_alerts


@pytest.mark.asyncio
@pytest.mark.parametrize('crossed,configured_alert',[(True,False),(True,True),(False,False)])
async def test_budget_alert_is_sent_after_confirmation_once(ingress,accounts,crossed,configured_alert):
    from app.api.whatsapp import WhatsAppText
    await seed_budget(accounts,1100 if crossed else 2000)
    if configured_alert:
        publish(accounts,'budget.threshold_crossed',{'start_node':'notice','nodes':[{'id':'notice','type':'text','body':'Aviso: {category} {spent_amount} / {limit_amount}'}]})
    def plan(kwargs):
        ref=next(x['ref'] for x in initial(kwargs)['categories'] if x['name']=='Comida')
        return [('register_movement',register_now(category_ref=ref))]
    preview=await ingress.send('Gasto',steps=[plan])
    result=await ingress.confirm(preview)
    if crossed:
        assert 'Gastaste' not in result.reply_text
    else:
        assert 'Gastaste' in result.reply_text
    alerts=[m for m in result.followup_messages if isinstance(m,WhatsAppText) and ('Aviso:' in m.body or 'Alerta de presupuesto' in m.body)]
    assert len(alerts)==int(crossed)
    if configured_alert:
        assert '934' in alerts[0].body and '1.100' in alerts[0].body
    replay=await ingress.confirm(preview)
    assert not replay.budget_threshold_alerts
