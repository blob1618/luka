"""Firma HMAC del webhook de Meta (X-Hub-Signature-256)."""
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SECRET = 'test-app-secret'


def payload():
    return {'object': 'whatsapp_business_account', 'entry': []}


def raw_body():
    return json.dumps(payload()).encode()


def sign(body, secret=SECRET):
    return 'sha256=' + hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()


def post(body, header=None):
    headers = {'content-type': 'application/json'}
    if header is not None:
        headers['x-hub-signature-256'] = header
    return client.post('/webhook', content=body, headers=headers)


def test_unsigned_payload_is_rejected_when_secret_configured(monkeypatch):
    monkeypatch.setenv('WHATSAPP_APP_SECRET', SECRET)
    assert post(raw_body()).status_code == 403


def test_wrong_signature_is_rejected(monkeypatch):
    monkeypatch.setenv('WHATSAPP_APP_SECRET', SECRET)
    assert post(raw_body(), 'sha256=' + '0' * 64).status_code == 403


def test_signature_for_another_body_is_rejected(monkeypatch):
    monkeypatch.setenv('WHATSAPP_APP_SECRET', SECRET)
    assert post(raw_body(), sign(b'{"object":"other"}')).status_code == 403


def test_malformed_signature_header_is_rejected(monkeypatch):
    monkeypatch.setenv('WHATSAPP_APP_SECRET', SECRET)
    assert post(raw_body(), 'sha1=abc').status_code == 403


def test_valid_signature_is_accepted(monkeypatch):
    monkeypatch.setenv('WHATSAPP_APP_SECRET', SECRET)
    body = raw_body()
    response = post(body, sign(body))
    assert response.status_code == 200 and response.json() == {'status': 'ok'}


def test_unsigned_payload_allowed_without_secret(monkeypatch):
    monkeypatch.delenv('WHATSAPP_APP_SECRET', raising=False)
    assert post(raw_body()).status_code == 200


def test_invalid_json_is_rejected(monkeypatch):
    monkeypatch.delenv('WHATSAPP_APP_SECRET', raising=False)
    response = client.post('/webhook', content=b'not-json', headers={'content-type': 'application/json'})
    assert response.status_code == 400
