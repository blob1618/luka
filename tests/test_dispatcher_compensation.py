"""Functional Stage5 ports through explicit tools, real services and synthetic delivery."""
from datetime import date
from decimal import Decimal
import pytest
from app.models.database import Categoria, LimiteCategoria
from app.services import assistant_state
from tests import test_assistant_stage5 as scenarios
accounts=scenarios.accounts
ingress=scenarios.ingress


async def budgets(accounts,donor=True):
    with accounts[0]() as session:
        session.add(LimiteCategoria(usuario_id=accounts[1],categoria_id=accounts[4],cantidad_max=500,moneda='ARS',inicio_periodo=date(2026,10,1),fin_periodo=date(2026,10,31)))
        if donor:
            category=Categoria(usuario_id=accounts[1],nombre='Donante')
            session.add(category)
            session.flush()
            session.add(LimiteCategoria(usuario_id=accounts[1],categoria_id=category.id,cantidad_max=1000,moneda='ARS',inicio_periodo=date(2026,10,1),fin_periodo=date(2026,10,31)))
        session.commit()


def totals(accounts):
    with accounts[0]() as session:
        return sorted(Decimal(row.cantidad_max) for row in session.query(LimiteCategoria))


@pytest.mark.asyncio
async def test_user_request_stores_pending_without_writes(ingress,accounts):
    await budgets(accounts)
    before=totals(accounts)
    preview=await ingress.tool('propose_compensation',{'currency':'ARS'})
    assert preview.assistant_preview_delivery and '328' in preview.reply_text and totals(accounts)==before


@pytest.mark.asyncio
async def test_no_funds_reply_is_constructive(ingress,accounts):
    await budgets(accounts,donor=False)
    result=await ingress.tool('propose_compensation',{'currency':'ARS'})
    assert 'No hay categorías con presupuesto disponible' in result.reply_text
    assert not result.assistant_preview_delivery and totals(accounts)==[500]


@pytest.mark.asyncio
async def test_text_confirmation_applies_and_clears_state(ingress,accounts):
    await budgets(accounts)
    await ingress.tool('propose_compensation',{'currency':'ARS'})
    result=await ingress.send('sí')
    assert 'Apliqué' in result.reply_text and totals(accounts)==[672,828]
    assert (await assistant_state.read(accounts[1]))['workspace']['active_operation_id'] is None


@pytest.mark.asyncio
async def test_confirmation_when_proposal_stale_asks_for_new_calculation(ingress,accounts):
    await budgets(accounts)
    preview=await ingress.tool('propose_compensation',{'currency':'ARS'})
    with accounts[0]() as session:
        row=session.query(LimiteCategoria).filter_by(categoria_id=accounts[4]).one()
        row.cantidad_max=550
        session.commit()
    before=totals(accounts)
    rejected=await ingress.confirm(preview)
    assert 'No hice cambios' in rejected.reply_text and totals(accounts)==before


@pytest.mark.asyncio
@pytest.mark.parametrize('button',[False,True])
async def test_rejection_writes_nothing(ingress,accounts,button):
    await budgets(accounts)
    before=totals(accounts)
    preview=await ingress.tool('propose_compensation',{'currency':'ARS'})
    if button:
        option=next(b.id for m in [preview.reply_message,*preview.followup_messages] if hasattr(m,'buttons') for b in m.buttons if b.id.startswith('assistant:cancel:'))
        result=await ingress.send('Cancelar',option_id=option)
    else:
        result=await ingress.send('cancelar')
    assert 'Cancelé' in result.reply_text and totals(accounts)==before


@pytest.mark.asyncio
async def test_unrelated_message_discards_pending(ingress,accounts):
    # New contract suspends instead of discarding: a new delivery is needed to resume.
    await budgets(accounts)
    before=totals(accounts)
    preview=await ingress.tool('propose_compensation',{'currency':'ARS'})
    await ingress.tool('list_categories')
    rejected=await ingress.confirm(preview)
    assert 'vigente' in rejected.reply_text and totals(accounts)==before
    assert any(op['status']=='suspended' for op in (await assistant_state.read(accounts[1]))['workspace']['operations'].values())


@pytest.mark.asyncio
async def test_confirmation_without_pending_explains_and_does_not_apply(ingress,accounts):
    await budgets(accounts)
    before=totals(accounts)
    result=await ingress.send('sí')
    assert 'vigente' in result.reply_text and totals(accounts)==before


@pytest.mark.asyncio
async def test_reset_context_clears_pending(ingress,accounts):
    await budgets(accounts)
    before=totals(accounts)
    await ingress.tool('propose_compensation',{'currency':'ARS'})
    await ingress.tool('reset_context')
    assert totals(accounts)==before and not (await assistant_state.read(accounts[1]))['workspace']['operations']
