"""Real HTTP → dispatcher → native plan → delivery → confirmed persistence, offline."""
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models.database import AssistantWriteReceipt,MovimientoFinanciero,OnboardingInvitacion,Recordatorio
from app.api.whatsapp import WhatsAppReplyButtons
from app.services import assistant_runtime
from tests import test_assistant_stage5 as scenarios
from tests.test_assistant_pilot import NativeScript,PHONE
from tests.test_webhook import make_text_message,make_interactive_message,make_webhook_payload
accounts=scenarios.accounts
ingress=scenarios.ingress
client=TestClient(app)


@pytest.fixture
def http_send(ingress,monkeypatch):
    sent=AsyncMock(return_value=True)
    monkeypatch.setattr('app.main.send_whatsapp_message',sent)
    monkeypatch.setattr('app.main._dispatch_whatsapp_typing_indicator',lambda *args:None)
    return sent


def confirmation(sent):
    return next(b.id for call in sent.await_args_list if isinstance(call.args[1],WhatsAppReplyButtons) for b in call.args[1].buttons if b.id.startswith('assistant:confirm:'))


@pytest.mark.parametrize('kind',['egreso','ingreso'])
def test_webhook_integration_valid_movement_requires_delivery_and_confirmation(accounts,http_send,monkeypatch,kind):
    script=NativeScript([[('register_movement',{'economic_effect':'cash_flow','movement_type':kind,'amount':'5000','currency':'ARS','description':'HTTP propio','date_spec':{'kind':'literal','evidence_span':'16/09/2026'}})]])
    monkeypatch.setattr(assistant_runtime,'create_provider',lambda:script)
    payload=make_webhook_payload([make_text_message('Registrá5000 el16/09/2026','http-register',PHONE)])
    assert client.post('/webhook',json=payload).status_code==200
    with accounts[0]() as session:
        assert session.query(AssistantWriteReceipt).count()==0
    action=confirmation(http_send)
    approved=make_webhook_payload([make_interactive_message(action,'http-confirm',PHONE)])
    assert client.post('/webhook',json=approved).status_code==200
    assert client.post('/webhook',json=approved).status_code==200
    with accounts[0]() as session:
        row=session.query(MovimientoFinanciero).filter_by(whatsapp_message_id='http-register').one()
        assert row.tipo==kind and row.cantidad==5000 and str(row.fecha_movimiento)=='2026-09-16'
        assert session.query(AssistantWriteReceipt).count()==1
    assert script.calls==1


def test_webhook_integration_unknown_user_does_not_create_movement(accounts,http_send,monkeypatch):
    script=NativeScript([])
    monkeypatch.setattr(assistant_runtime,'create_provider',lambda:script)
    assert client.post('/webhook',json=make_webhook_payload([make_text_message('Hola','http-unknown','5491100999999')])).status_code==200
    with accounts[0]() as session:
        assert session.query(OnboardingInvitacion).count()==1 and session.query(AssistantWriteReceipt).count()==0
    assert script.calls==0 and http_send.await_count==1


def test_create_reminder_not_processed_twice(accounts,http_send,monkeypatch):
    script=NativeScript([[('create_reminder',{'title':'Internet HTTP','day':15,'amount':'1500','currency':'ARS'})]])
    monkeypatch.setattr(assistant_runtime,'create_provider',lambda:script)
    assert client.post('/webhook',json=make_webhook_payload([make_text_message('Recordatorio','http-reminder',PHONE)])).status_code==200
    action=confirmation(http_send)
    for mid in ('http-reminder-confirm','http-reminder-confirm','http-reminder-replay'):
        assert client.post('/webhook',json=make_webhook_payload([make_interactive_message(action,mid,PHONE)])).status_code==200
    with accounts[0]() as session:
        row=session.query(Recordatorio).one()
        assert row.titulo=='Internet HTTP' and row.dia_del_mes==15 and row.monto==1500
