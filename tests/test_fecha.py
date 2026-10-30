"""Explicit tool scenarios preserve financial effects after the global cut."""
import pytest
from app.models.database import AssistantWriteReceipt, MovimientoFinanciero
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import register_now
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
@pytest.mark.parametrize('date_spec,phrase,expected',[({'kind':'relative_days','evidence_span':'ayer','day_offset':-1},'Gasté ayer','2026-10-26'),({'kind':'literal','evidence_span':'16/09/2026'},'Gasté el16/09/2026','2026-09-16')])
async def test_single_expense_direct_path_passes_ayer(ingress,accounts,date_spec,phrase,expected):
    preview=await ingress.tool('register_movement',register_now(date_spec=date_spec),phrase)
    await ingress.confirm(preview)
    with accounts[0]() as session:
        row=session.query(MovimientoFinanciero).filter(MovimientoFinanciero.whatsapp_message_id.like('stage5-%')).one()
        assert str(row.fecha_movimiento)==expected


@pytest.mark.asyncio
async def test_invalid_date_collects_and_never_silently_becomes_today(ingress,accounts):
    result=await ingress.tool('register_movement',register_now(date_spec={'kind':'literal','evidence_span':'31/02/2026'}),'Gasté el31/02/2026')
    assert not result.assistant_preview_delivery and 'fecha' in result.reply_text
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0
