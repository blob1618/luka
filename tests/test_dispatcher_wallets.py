"""Explicit tool scenarios preserve financial effects after the global cut."""
import pytest
from app.models.database import (
    AssistantWriteReceipt, Billetera, LimiteCategoria, MovimientoFinanciero, Recordatorio,
)
from app.services import assistant_state
from app.services.wallet import WalletService
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_stage5 import register_now
from tests.test_assistant_stage4 import initial
accounts=scenarios.accounts
ingress=scenarios.ingress


@pytest.mark.asyncio
@pytest.mark.parametrize('with_category',[False,True])
async def test_registration_uses_single_wallet_without_asking(ingress,accounts,with_category):
    with accounts[0]() as session:
        before=session.query(MovimientoFinanciero).count()
    def plan(kwargs):
        patch={'category_ref':initial(kwargs)['categories'][0]['ref']} if with_category else {}
        return [('register_movement',register_now(**patch))]
    preview=await ingress.send('Gasto',steps=[plan])
    assert 'billetera Caja A' in preview.reply_text and 'Segunda' not in preview.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count()==before
    await ingress.confirm(preview)
    with accounts[0]() as session:
        new=session.query(MovimientoFinanciero).filter(MovimientoFinanciero.whatsapp_message_id.like('stage5-%')).one()
        assert new.billetera_id==accounts[3] and new.cantidad==106


@pytest.mark.asyncio
async def test_cancel_pending_registration_clears_state(ingress,accounts):
    await ingress.tool('register_movement',register_now())
    await ingress.send('cancelar')
    state=await assistant_state.read(accounts[1])
    assert state['workspace']['active_operation_id'] is None and not state.get('selection_options')
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_wallet_deleted_after_preview_prevents_write(ingress,accounts):
    def plan(kwargs):
        wallet=next(w['ref'] for w in initial(kwargs)['wallets'] if w['name']=='Caja A')
        return [('register_movement',register_now(wallet_ref=wallet))]
    preview=await ingress.send('Gasté',steps=[plan])
    with accounts[0]() as session:
        row=session.get(Billetera,accounts[3])
        session.query(MovimientoFinanciero).filter_by(billetera_id=row.id).delete()
        session.delete(row)
        session.commit()
        before=session.query(MovimientoFinanciero).count()
    rejected=await ingress.confirm(preview)
    assert 'No hice cambios' in rejected.reply_text
    with accounts[0]() as session:
        assert session.query(MovimientoFinanciero).count()==before and session.query(AssistantWriteReceipt).count()==0


@pytest.mark.asyncio
async def test_create_wallet_reports_existing_wallet(ingress,accounts):
    result = await ingress.tool('create_wallet',{'name':'Nueva','currency':'USD'})
    assert result.assistant_preview_delivery is None
    assert 'USD A' in result.reply_text
    with accounts[0]() as session:
        assert session.query(Billetera).filter_by(usuario_id=accounts[1],nombre='Nueva',moneda='USD').count()==0


@pytest.mark.asyncio
async def test_rename_wallet_intent(ingress,accounts):
    def plan(kwargs):
        ref=next(w['ref'] for w in initial(kwargs)['wallets'] if w['name']=='Caja A')
        return [('rename_wallet',{'wallet_ref':ref,'name':'Caja principal'})]
    preview=await ingress.send('Renombrá Caja A',steps=[plan])
    assert 'Caja principal' in preview.reply_text
    await ingress.confirm(preview)
    with accounts[0]() as session:
        assert session.query(Billetera).filter_by(usuario_id=accounts[1],nombre='Caja principal').count()==1


@pytest.mark.asyncio
async def test_list_wallets_intent_and_isolation(ingress):
    result=await ingress.tool('wallet_balances')
    assert 'Caja A' in result.reply_text and 'USD A' in result.reply_text
    assert 'Caja privada B' not in result.reply_text and '999.999' not in result.reply_text


@pytest.mark.asyncio
async def test_wallet_balances_filters_requested_currency(ingress):
    usd = await ingress.tool('wallet_balances', {'currency': 'USD'})
    assert 'USD A' in usd.reply_text and 'Caja A' not in usd.reply_text
    ars = await ingress.tool('wallet_balances', {'currency': 'ARS'})
    assert 'Caja A' in ars.reply_text and 'USD A' not in ars.reply_text


def test_wallet_selector_labels_include_currency(accounts):
    from app.services.assistant_capabilities.conversation import _wallet_label
    assert _wallet_label(accounts[1], str(accounts[3]), 'Caja A') == 'Caja A (ARS)'


@pytest.mark.asyncio
async def test_brl_wallet_can_be_created_and_queried(ingress, accounts):
    preview = await ingress.tool('create_wallet', {'name': 'Reales', 'currency': 'BRL'})
    assert preview.assistant_preview_delivery is not None
    await ingress.confirm(preview)
    with accounts[0]() as session:
        wallet = session.query(Billetera).filter_by(usuario_id=accounts[1], moneda='BRL').one()
        assert wallet.nombre == 'Reales'
    result = await ingress.tool('wallet_balances', {'currency': 'BRL'})
    assert 'Reales' in result.reply_text and 'BRL' in result.reply_text


@pytest.mark.asyncio
async def test_register_brl_movement_uses_brl_wallet(ingress, accounts):
    WalletService.create_wallet(accounts[1], 'Reales', 'BRL')
    with accounts[0]() as session:
        wallet = session.query(Billetera).filter_by(usuario_id=accounts[1], moneda='BRL').one()
        wallet_id = wallet.id
    def plan(kwargs):
        return [('register_movement', {**register_now(), 'currency': 'BRL',
                                       'amount': '20', 'description': 'caipirinha'})]
    preview = await ingress.send('Gasté 20 reales', steps=[plan])
    assert 'BRL' in preview.reply_text
    await ingress.confirm(preview)
    with accounts[0]() as session:
        row = session.query(MovimientoFinanciero).filter_by(descripcion='caipirinha').one()
        assert row.moneda == 'BRL' and row.billetera_id == wallet_id


@pytest.mark.asyncio
async def test_limit_in_brl_requires_brl_wallet(ingress, accounts):
    def plan(kwargs):
        ref = next(item['ref'] for item in initial(kwargs)['categories'] if item['name'] == 'Comida')
        return [('create_limit', {'category_ref': ref, 'amount': '1000', 'month': 12,
                                  'year': 2026, 'currency': 'BRL'})]
    result = await ingress.send('Límite de comida en reales', steps=[plan])
    assert result.assistant_preview_delivery is None
    assert 'No tenés una billetera en BRL' in result.reply_text


@pytest.mark.asyncio
async def test_limit_in_brl_with_wallet_is_created(ingress, accounts):
    WalletService.create_wallet(accounts[1], 'Reales', 'BRL')
    def plan(kwargs):
        ref = next(item['ref'] for item in initial(kwargs)['categories'] if item['name'] == 'Comida')
        return [('create_limit', {'category_ref': ref, 'amount': '1000', 'month': 12,
                                  'year': 2026, 'currency': 'BRL'})]
    preview = await ingress.send('Límite de comida en reales', steps=[plan])
    assert preview.assistant_preview_delivery is not None
    await ingress.confirm(preview)
    with accounts[0]() as session:
        row = session.query(LimiteCategoria).filter_by(usuario_id=accounts[1], moneda='BRL').one()
        assert row.cantidad_max == 1000


@pytest.mark.asyncio
async def test_reminder_in_brl_does_not_require_wallet(ingress, accounts):
    preview = await ingress.tool('create_reminder', {'title': 'Alquiler', 'day': 10,
                                                     'amount': '500', 'currency': 'BRL'})
    assert preview.assistant_preview_delivery is not None
    await ingress.confirm(preview)
    with accounts[0]() as session:
        reminder = session.query(Recordatorio).filter_by(usuario_id=accounts[1], titulo='Alquiler').one()
        assert reminder.moneda == 'BRL'


@pytest.mark.asyncio
async def test_wallet_balances_rejects_unsupported_currency(ingress):
    result = await ingress.tool('wallet_balances', {'currency': 'JPY'})
    assert 'Moneda no soportada' in result.reply_text
    assert 'ARS, USD, BRL o EUR' in result.reply_text


@pytest.mark.asyncio
async def test_create_wallet_unsupported_currency_is_explained(ingress):
    result = await ingress.tool('create_wallet', {'name': 'Yenes', 'currency': 'JPY'})
    assert result.assistant_preview_delivery is None
    assert 'Moneda no soportada' in result.reply_text
    assert 'ARS, USD, BRL o EUR' in result.reply_text
