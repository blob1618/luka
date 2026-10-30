"""HTTP transport ports; interpretation and financial writes use tool scenarios."""
from unittest.mock import AsyncMock,patch
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services.dispatch_contract import DispatchResult

client=TestClient(app)


def make_webhook_payload(messages=None,statuses=None):
    value={'messaging_product':'whatsapp'}
    if messages is not None:
        value['messages']=messages
    if statuses is not None:
        value['statuses']=statuses
    return {'object':'whatsapp_business_account','entry':[{'id':'test','changes':[{'value':value,'field':'messages'}]}]}


def make_text_message(body='Hola',msg_id='http-unit',phone='12345'):
    return {'id':msg_id,'from':phone,'type':'text','text':{'body':body}}


def make_interactive_message(option_id='bound-action',msg_id='http-button',phone='12345'):
    return {'id':msg_id,'from':phone,'type':'interactive','interactive':{'type':'button_reply','button_reply':{'id':option_id,'title':'Elegir'}}}


def test_verify_webhook_success():
    from app.main import VERIFY_TOKEN
    response=client.get('/webhook',params={'hub.mode':'subscribe','hub.verify_token':VERIFY_TOKEN,'hub.challenge':'1158201444'})
    assert response.status_code==200 and response.text=='1158201444'


def test_verify_webhook_failure():
    assert client.get('/webhook',params={'hub.mode':'subscribe','hub.verify_token':'wrong','hub.challenge':'abc'}).status_code==403


def test_handle_webhook_duplicate_message_is_suppressed_before_dispatch():
    with patch('app.services.webhook_idempotency.WebhookIdempotencyService.claim',AsyncMock(return_value=None)),patch('app.main.process_incoming_message',AsyncMock()) as process,patch('app.main.send_whatsapp_message',AsyncMock()) as send:
        response=client.post('/webhook',json=make_webhook_payload([make_text_message()]))
    assert response.status_code==200
    process.assert_not_awaited()
    send.assert_not_awaited()


def test_handle_webhook_routes_interactive_reply_by_id():
    with patch('app.main.process_incoming_interactive_reply',AsyncMock(return_value=DispatchResult('Listo'))) as process,patch('app.main.send_whatsapp_message',AsyncMock(return_value=True)) as send,patch('app.main._dispatch_whatsapp_typing_indicator'):
        response=client.post('/webhook',json=make_webhook_payload([make_interactive_message()]))
    assert response.status_code==200
    assert process.await_args.kwargs=={'sender_phone':'12345','option_id':'bound-action','reply_type':'button_reply','whatsapp_message_id':'http-button'}
    send.assert_awaited_once()


def test_handle_webhook_status_update_without_messages_does_not_fail():
    with patch('app.main.process_incoming_message',AsyncMock()) as process:
        response=client.post('/webhook',json=make_webhook_payload(statuses=[{'id':'status','status':'delivered'}]))
    assert response.status_code==200
    process.assert_not_awaited()


@pytest.mark.parametrize('kind',['audio','image','video','location'])
def test_unsupported_media_never_returns_a_dummy_financial_success(kind):
    with patch('app.main.process_incoming_message',AsyncMock()) as process,patch('app.main.send_whatsapp_message',AsyncMock(return_value=True)) as send:
        response=client.post('/webhook',json=make_webhook_payload([{'id':'unsupported','from':'12345','type':kind,kind:{'id':'synthetic'}}]))
    assert response.status_code==200
    process.assert_not_awaited()
    assert all('registr' not in str(call.args[1]).lower() for call in send.await_args_list)
