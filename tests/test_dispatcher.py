"""Global ingress functional contract, independent of retired classifier responses."""
from unittest.mock import Mock
import pytest
from app.models.database import AssistantWriteReceipt,MovimientoFinanciero,Usuario
from app.services import dispatcher
from app.services.onboarding import OnboardingDecision,OnboardingResult
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import register_now
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
@pytest.mark.parametrize('decision',[OnboardingDecision.SUPPRESS_RESPONSE,OnboardingDecision.ERROR])
async def test_onboarding_precedes_model_and_writes(ingress,accounts,monkeypatch,decision):
    monkeypatch.setattr(dispatcher.OnboardingService,'prepare_whatsapp_message',Mock(return_value=OnboardingResult(decision)))
    result=await ingress.send('Hola')
    assert result.service_invoked=='onboarding' and ingress.steps.calls==0
    if decision==OnboardingDecision.SUPPRESS_RESPONSE:
        assert not result.reply_text
    else:
        assert 'No pude verificar' in result.reply_text
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_greeting_returns_llm_reply(ingress,accounts):
    result=await ingress.tool('conversation_reply',{'kind':'greeting'})
    assert 'Hola' in result.reply_text and result.protected_presentation
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['help','out_of_scope'])
async def test_non_financial_requests_cannot_implicitly_write(ingress,accounts,kind):
    result=await ingress.tool('conversation_reply',{'kind':kind})
    assert result.reply_text and not result.assistant_preview_delivery
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_expense_without_category_registers_single_movement(ingress,accounts):
    preview=await ingress.tool('register_movement',register_now(description='Sin categoría'))
    result=await ingress.confirm(preview)
    assert 'Sin categoría' in result.reply_text
    with accounts[0]() as session:
        row=session.query(MovimientoFinanciero).filter_by(descripcion='Sin categoría').one()
        assert row.categoria_id is None and row.cantidad==106
        assert session.query(AssistantWriteReceipt).count()==1


@pytest.mark.asyncio
async def test_success_commits(ingress,accounts):
    result=await ingress.tool('conversation_reply',{'kind':'help'})
    assert result.service_invoked=='assistant'
    with accounts[0]() as session:
        assert session.get(Usuario,accounts[1]).ultimo_mensaje_en is not None
