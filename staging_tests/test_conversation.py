"""Corre una conversación real contra un entorno de staging desplegado.

Requiere STAGING_BASE_URL, STAGING_APP_SECRET y STAGING_TEST_TOKEN. No se
recolecta en la suite normal (vive fuera de tests/): se ejecuta a mano o desde
el workflow Staging E2E.
"""
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
import pytest

BASE_URL = os.getenv('STAGING_BASE_URL', '').rstrip('/')
APP_SECRET = os.getenv('STAGING_APP_SECRET', '')
TEST_TOKEN = os.getenv('STAGING_TEST_TOKEN', '')
EXPECTED_COMMIT = os.getenv('STAGING_EXPECTED_COMMIT', '').strip()
FIXED_PHONE = os.getenv('STAGING_PHONE', '').strip()

pytestmark = pytest.mark.skipif(
    not (BASE_URL and APP_SECRET and TEST_TOKEN),
    reason='faltan STAGING_BASE_URL, STAGING_APP_SECRET o STAGING_TEST_TOKEN',
)


def _headers():
    return {'x-test-token': TEST_TOKEN}


def _post_webhook(payload):
    body = json.dumps(payload).encode('utf-8')
    signature = 'sha256=' + hmac.new(APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()
    response = httpx.post(f'{BASE_URL}/webhook', content=body, timeout=30, headers={
        'content-type': 'application/json',
        'x-hub-signature-256': signature,
    })
    assert response.status_code == 200, response.text


def _outbox(phone):
    response = httpx.get(f'{BASE_URL}/__test__/outbox', params={'phone': phone},
                         headers=_headers(), timeout=30)
    assert response.status_code == 200, response.text
    return response.json()['messages']


def _clear(phone):
    httpx.delete(f'{BASE_URL}/__test__/outbox', params={'phone': phone},
                 headers=_headers(), timeout=30)


def _wait(phone, predicate, timeout=120):
    deadline = time.time() + timeout
    messages = []
    while time.time() < deadline:
        messages = _outbox(phone)
        if predicate(messages):
            return messages
        time.sleep(2)
    raise AssertionError(f'Tiempo agotado esperando respuesta; outbox={messages}')


def _confirm_id(messages):
    for payload in messages:
        if payload.get('type') != 'interactive':
            continue
        for button in payload.get('interactive', {}).get('action', {}).get('buttons', []):
            identifier = button.get('reply', {}).get('id', '')
            if identifier.startswith('assistant:confirm:'):
                return identifier
    return None


def _unique_phone():
    return FIXED_PHONE or '54911' + str(int(time.time()))[-8:]


def test_version_and_ready():
    version = httpx.get(f'{BASE_URL}/version', timeout=30)
    assert version.status_code == 200
    if EXPECTED_COMMIT:
        assert version.json()['commit'].startswith(EXPECTED_COMMIT[:7])
    ready = httpx.get(f'{BASE_URL}/ready', timeout=30)
    assert ready.status_code == 200 and ready.json()['ready'] is True


def test_register_then_confirm_conversation():
    phone = _unique_phone()
    _clear(phone)
    message_id = f'wamid.staging.{uuid.uuid4().hex}'
    _post_webhook({
        'object': 'whatsapp_business_account',
        'entry': [{'id': 'waba', 'changes': [{'field': 'messages', 'value': {
            'messaging_product': 'whatsapp',
            'metadata': {'phone_number_id': 'staging'},
            'messages': [{'id': message_id, 'from': phone, 'timestamp': '1760000000',
                          'type': 'text', 'text': {'body': 'Gasté 5000 en nafta'}}],
        }}]}],
    })

    preview = _wait(phone, lambda messages: _confirm_id(messages) is not None)
    button_id = _confirm_id(preview)

    _post_webhook({
        'object': 'whatsapp_business_account',
        'entry': [{'id': 'waba', 'changes': [{'field': 'messages', 'value': {
            'messaging_product': 'whatsapp',
            'metadata': {'phone_number_id': 'staging'},
            'messages': [{'id': f'wamid.staging.{uuid.uuid4().hex}', 'from': phone,
                          'timestamp': '1760000001', 'type': 'interactive',
                          'interactive': {'type': 'button_reply',
                                          'button_reply': {'id': button_id,
                                                           'title': 'Confirmar'}}}],
        }}]}],
    })

    confirmed = _wait(phone, lambda messages: any(
        'Registré' in payload.get('text', {}).get('body', '') for payload in messages))
    assert any('Registré' in payload.get('text', {}).get('body', '') for payload in confirmed)
    _clear(phone)
