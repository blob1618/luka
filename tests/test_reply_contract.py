"""Global ingress functional contract, independent of retired classifier responses."""
import pytest
from app.models.database import AssistantWriteReceipt
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import register_now
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['register','reminder','category'])
async def test_user_sees_backend_preview_and_only_backend_confirmed_result(ingress,accounts,kind):
    tool,args={'register':('register_movement',register_now()),'reminder':('create_reminder',{'title':'Internet','day':15}),'category':('create_category',{'name':'Nueva'})}[kind]
    preview=await ingress.tool(tool,args)
    assert preview.assistant_preview_delivery and 'Todavía no hice cambios' in preview.reply_text
    result=await ingress.confirm(preview)
    assert result.protected_presentation and 'Estoy procesando el movimiento' not in result.reply_text
    assert 'Todavía no hice cambios' not in result.reply_text


@pytest.mark.asyncio
async def test_unknown_presentation_reference_never_invents_confirmation(ingress,accounts):
    result=await ingress.send('Presentá',steps=[[('present_result',{'block_refs':['ref_'+'0'*32]})],[]])
    assert result.protected_presentation and not result.assistant_preview_delivery
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0
