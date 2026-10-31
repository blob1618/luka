"""Wallet simulation through real dispatcher, local delivery callbacks and visible history."""
from decimal import Decimal
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from app.models.database import Usuario,Categoria,Billetera,MovimientoFinanciero,AssistantWriteReceipt
from app.services import assistant_runtime,assistant_state
from app.services.conversation import ConversationService
from app.services.wallet import WalletService
from app.services.finance import FinanceService
from testing.services.webhook_mode import WebhookModeService
from tests.test_assistant_pilot import NativeScript,present_all
from tests.test_assistant_stage4 import initial


@pytest.mark.asyncio
async def test_streamlit_simulator_wallet_flow_keeps_selection_currency_and_annulment_isolated(isolated_db,monkeypatch,fake_redis,capsys):
    import importlib
    for name in ('compensation','chart_query','monthly_balance','conversation_flow'):
        monkeypatch.setattr(importlib.import_module('app.services.'+name),'SessionLocal',isolated_db)
    monkeypatch.setattr(ConversationService,'_get_client',AsyncMock(return_value=fake_redis))
    phone,foreign_phone='5491100000240','5491100000241'
    owner,other=uuid4(),uuid4()
    with isolated_db() as session:
        session.add_all([Usuario(id=owner,nombre='Simulador',email='simulator@example.invalid',whatsapp_id=phone,auth_user_id=uuid4()),Usuario(id=other,nombre='Otro',email='simulator-other@example.invalid',whatsapp_id=foreign_phone,auth_user_id=uuid4()),Categoria(usuario_id=owner,nombre='Comida')])
        session.commit()
    service=WebhookModeService()
    async def send_tool(name,args,text='Pedido tipado'):
        monkeypatch.setattr(assistant_runtime,'create_provider',lambda:NativeScript([args if callable(args) else [(name,args)],present_all]))
        result=await service.send_message(text,phone,'gemini','prompts/core_prompt.md')
        assert result.status=='completed' and result.error is None
        return result
    async def confirm(preview):
        option=next(o for o in preview.interactions if o['title']=='Confirmar')
        result=await service.send_message('',phone,'gemini','prompts/core_prompt.md',option_id=option['id'],reply_type=option['reply_type'],title=option['title'])
        assert result.status=='completed' and result.error is None
        return result
    for name,currency in [('Efectivo','ARS'),('Dólares','USD')]:
        preview=await send_tool('create_wallet',{'name':name,'currency':currency})
        await confirm(preview)
    with isolated_db() as session:
        wallets={w.nombre:w for w in session.query(Billetera).filter_by(usuario_id=owner)}
        session.commit()
        ids={name:row.id for name,row in wallets.items()}
    def expense(kwargs):
        category=next(c['ref'] for c in initial(kwargs)['categories'] if c['name']=='Comida')
        return [('register_movement',{'economic_effect':'cash_flow','movement_type':'egreso','amount':'5400','currency':'ARS','description':'almuerzo','category_ref':category,'date_spec':{'kind':'absent'}})]
    preview=await send_tool('register_movement',expense)
    assert 'Efectivo' in preview.reply_text
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count()==0
    await confirm(preview)
    with isolated_db() as session:
        row=session.query(MovimientoFinanciero).one()
        assert row.billetera_id==ids['Efectivo'] and row.cantidad==5400 and row.moneda=='ARS'
    usd=await send_tool('register_movement',{'economic_effect':'cash_flow','movement_type':'ingreso','amount':'375','currency':'USD','description':'trabajo freelance','date_spec':{'kind':'absent'}})
    await confirm(usd)
    before=await send_tool('wallet_balances',{})
    assert all(name in before.reply_text for name in ids) and 'Privada' not in before.reply_text
    balances={b.name:Decimal(str(b.balance)) for b in WalletService.get_balances_own_user(owner)}
    assert balances=={'Efectivo':Decimal(-5400),'Dólares':Decimal(375)}
    def annul(kwargs):
        ref=next(ref for ref,item in initial(kwargs)['references'].items() if item['kind']=='movement' and item['details']['description']=='trabajo freelance')
        return [('annul_movements',{'movement_refs':[ref]})]
    preview=await send_tool('annul_movements',annul)
    await confirm(preview)
    balances={b.name:Decimal(str(b.balance)) for b in WalletService.get_balances_own_user(owner)}
    assert balances=={'Efectivo':Decimal(-5400),'Dólares':Decimal(0)}
    foreign=WalletService.create_wallet(other,'Privada','ARS')
    assert FinanceService.register_movement_with_category(foreign_phone,'foreign-fixture','Ingreso privado','ingreso',Decimal(999999),'ARS','Privado',wallet_id=foreign.wallet_id,economic_effect='cash_flow').status=='registered'
    last=await send_tool('wallet_balances',{})
    assert 'Privada' not in last.reply_text and '999999' not in last.reply_text
    with isolated_db() as session:
        assert session.query(AssistantWriteReceipt).count()==3
    assert (await assistant_state.read(owner))['workspace']['active_operation_id'] is None
