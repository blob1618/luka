"""Explicit tool scenarios preserve financial effects after the global cut."""
import pytest
from app.models.database import AssistantWriteReceipt
from tests import test_assistant_stage5 as scenarios
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
@pytest.mark.parametrize('selector,expected',[({'kind':'all'},9),({'kind':'month','month':'2026-10'},8),({'kind':'period','period':{'start':'2026-10-10','end':'2026-10-17'}},8)])
async def test_query_filters_keep_owned_total_and_limit_five(ingress,accounts,selector,expected):
    result=await ingress.tool('find_movements',{'selector':selector,'sort_by':'date_desc','limit':5})
    output=result.tool_trace[0]['outcome']['data']
    assert output['total_found']==expected and output['returned_count']==5
    assert 'privado' not in result.reply_text
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_shown_scope_never_expands_to_unshown_movements(ingress):
    first=await ingress.tool('find_movements',{'selector':{'kind':'month','month':'2026-10'},'sort_by':'date_desc','limit':2})
    refs=[item['movement_ref'] for item in first.tool_trace[0]['outcome']['data']['items']]
    second=await ingress.tool('find_movements',{'selector':{'kind':'shown','refs':refs},'sort_by':'date_desc','limit':5})
    output=second.tool_trace[0]['outcome']['data']
    assert output['total_found']==2 and {item['movement_ref'] for item in output['items']}==set(refs)


@pytest.mark.asyncio
async def test_no_data_does_not_invent_a_monetary_total(ingress):
    result=await ingress.tool('find_movements',{'selector':{'kind':'all'},'currency':'USD','sort_by':'date_desc'})
    assert result.tool_trace[0]['outcome']['data']['total_found']==0
    assert 'No encontré movimientos con esos filtros.' in result.reply_text
    assert 'total monetario' not in result.reply_text


@pytest.mark.asyncio
async def test_repeated_no_data_query_presents_message_once(ingress):
    args = {'selector': {'kind': 'all'}, 'description': 'no-existe', 'sort_by': 'date_desc', 'currency': 'ARS'}
    result = await ingress.send('Buscar', steps=[[('find_movements', args), ('find_movements', args)], [('present_result', {})]])
    assert result.reply_text.count('No encontré movimientos con esos filtros.') == 1
    assert 'Faltan los hechos obligatorios' not in result.reply_text
