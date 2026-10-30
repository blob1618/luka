"""Explicit tool scenarios preserve financial effects after the global cut."""
import pytest
from app.models.database import AssistantWriteReceipt
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import register_now
from tests.test_assistant_pilot import amend_active
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
async def test_single_movement_in_list_reply_not_none_amount(ingress,accounts):
    items=[register_now(description='uno',amount=None),register_now(description='dos',amount='200')]
    question=await ingress.tool('register_movement_batch',{'items':items})
    assert 'None' not in question.reply_text and 'importe' in question.reply_text and not question.assistant_preview_delivery
    items[0]['amount']='100'
    preview=await ingress.send('El primero100',steps=[amend_active({'items':items})])
    assert 'None' not in preview.reply_text and all(x in preview.reply_text for x in ['100','200','uno','dos'])
    await ingress.confirm(preview)
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).filter_by(command_kind='register_batch').count()==1
