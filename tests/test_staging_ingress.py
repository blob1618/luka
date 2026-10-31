"""Ingreso de staging: captura de salida y outbox protegida."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.whatsapp import WhatsAppText, normalize_whatsapp_number, send_whatsapp_message_detailed
from app.main import app
from app.services import staging_ingress

client = TestClient(app)
PHONE = '5491100260001'


@pytest.fixture(autouse=True)
def staging_env(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'staging')
    monkeypatch.setenv('TEST_INGRESS_TOKEN', 'staging-token')
    monkeypatch.setenv('WHATSAPP_TRANSPORT', 'capture')
    monkeypatch.delenv('WHATSAPP_API_TOKEN', raising=False)
    monkeypatch.delenv('WHATSAPP_PHONE_ID', raising=False)


def test_outbox_is_hidden_without_token(monkeypatch):
    monkeypatch.delenv('TEST_INGRESS_TOKEN', raising=False)
    assert client.get('/__test__/outbox', params={'phone': PHONE}).status_code == 404


def test_outbox_rejects_wrong_token():
    response = client.get('/__test__/outbox', params={'phone': PHONE},
                          headers={'x-test-token': 'wrong'})
    assert response.status_code == 401


def test_outbox_is_hidden_in_production(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    assert client.get('/__test__/outbox', params={'phone': PHONE},
                      headers={'x-test-token': 'staging-token'}).status_code == 404


def test_capture_sends_without_network_and_stores_payload(monkeypatch, fake_redis):
    def forbidden_client():
        raise AssertionError('capture no debe abrir el cliente de Graph')

    monkeypatch.setattr('app.api.whatsapp.get_whatsapp_client', forbidden_client)

    result = asyncio.run(send_whatsapp_message_detailed(PHONE, WhatsAppText('hola')))

    assert result.is_success and result.message_id.startswith('wamid.capture.')
    assert asyncio.run(staging_ingress.read_outbox(fake_redis, PHONE)) == [
        {'messaging_product': 'whatsapp', 'to': normalize_whatsapp_number(PHONE),
         'type': 'text', 'text': {'body': 'hola'}},
    ]


def test_outbox_lists_and_clears_captured_messages(fake_redis):
    asyncio.run(staging_ingress.record_outbound(PHONE, {'type': 'text'}))
    response = client.get('/__test__/outbox', params={'phone': PHONE},
                          headers={'x-test-token': 'staging-token'})
    assert response.status_code == 200
    assert response.json()['messages'] == [{'type': 'text'}]

    cleared = client.delete('/__test__/outbox', params={'phone': PHONE},
                            headers={'x-test-token': 'staging-token'})
    assert cleared.status_code == 200 and cleared.json()['removed'] == 1


def test_capture_is_disabled_outside_staging(monkeypatch, fake_redis):
    monkeypatch.setenv('APP_ENV', 'production')

    result = asyncio.run(send_whatsapp_message_detailed(PHONE, WhatsAppText('hola')))

    assert result.is_permanent and result.error_code == 'missing_credentials'
    assert asyncio.run(staging_ingress.read_outbox(fake_redis, PHONE)) == []
