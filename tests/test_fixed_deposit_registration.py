"""Functional Stage5 ports through explicit tools, real services and synthetic delivery."""
from decimal import Decimal
import pytest
from app.models.database import AssistantWriteReceipt, MovimientoFinanciero
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import register_now
from tests.test_assistant_pilot import PHONE
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
@pytest.mark.parametrize('economic_effect',['investment_principal','placement','existing_investment',None])
@pytest.mark.parametrize('movement_type',['ingreso','egreso'])
async def test_principal_is_rejected_before_financial_validation(ingress,accounts,economic_effect,movement_type):
    with accounts[0]() as session:
        before=session.query(MovimientoFinanciero).count()
    preview=await ingress.tool('register_movement',register_now(description='Capital de plazo fijo',movement_type=movement_type,economic_effect=economic_effect))
    assert not preview.assistant_preview_delivery
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count()==before and session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,description',[('ingreso','Intereses cobrados del plazo fijo'),('egreso','Comisión bancaria del FCI'),('ingreso','Honorarios de inversión'),('egreso','Honorarios del asesor')])
async def test_real_income_and_expenses_remain_registrable(ingress,accounts,kind,description):
    preview=await ingress.tool('register_movement',register_now(description=description,movement_type=kind,economic_effect='cash_flow',amount='500'))
    await ingress.confirm(preview)
    with accounts[0]() as session:
        row=session.query(MovimientoFinanciero).filter_by(descripcion=description).one()
        assert row.cantidad==500 and row.tipo==kind


@pytest.mark.asyncio
@pytest.mark.parametrize('principal_first',[False,True])
async def test_mixed_message_is_validated_before_first_write(ingress,accounts,principal_first):
    items=[register_now(description='Intereses',movement_type='ingreso',economic_effect='cash_flow'),register_now(description='Reinversión',economic_effect='placement')]
    if principal_first:
        items.reverse()
    result=await ingress.tool('register_movement_batch',{'items':items})
    assert not result.assistant_preview_delivery
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.parametrize('effect',[None,'investment_principal','placement','existing_investment'])
def test_service_guard_requires_explicit_economic_effect(accounts,effect):
    from app.services.finance import FinanceService
    result=FinanceService.register_movement_with_category(PHONE,'typed-policy','Texto no interpretado','egreso',Decimal(500),'ARS','Capital',economic_effect=effect)
    assert result.status=='unsupported_operation'
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0
