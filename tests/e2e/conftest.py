"""Arnés E2E: app real, webhook firmado y Meta simulado (sin red ni WhatsApp)."""
import hashlib
import hmac
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Billetera, Usuario
from app.services import assistant_runtime
from tests.e2e.support import PHONE, SECRET, ScriptedProvider


@pytest.fixture(autouse=True)
def whatsapp_env(monkeypatch):
    monkeypatch.setenv('WHATSAPP_API_TOKEN', 'e2e-token')
    monkeypatch.setenv('WHATSAPP_PHONE_ID', '1234567890')
    monkeypatch.setenv('WHATSAPP_GRAPH_API_VERSION', 'v26.0')


@pytest.fixture
def script(monkeypatch):
    def install(steps):
        monkeypatch.setattr(assistant_runtime, 'create_provider', lambda: ScriptedProvider(steps))
    return install


@pytest.fixture
def user(isolated_db):
    owner = uuid4()
    with isolated_db.begin() as session:
        session.add(Usuario(id=owner, nombre='E2E', email='e2e@example.invalid',
                            whatsapp_id=PHONE, auth_user_id=uuid4()))
        session.add(Billetera(usuario_id=owner, nombre='Efectivo', moneda='ARS'))
    return owner


@pytest.fixture
def graph(monkeypatch):
    sent = []

    def handler(request):
        payload = json.loads(request.content.decode('utf-8'))
        sent.append(payload)
        return httpx.Response(200, json={'messages': [{'id': f'wamid.mock.{len(sent)}'}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr('app.api.whatsapp.get_whatsapp_client', lambda: client)
    return sent


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv('WHATSAPP_APP_SECRET', SECRET)
    client = TestClient(app)

    def post(payload):
        body = json.dumps(payload).encode('utf-8')
        signature = 'sha256=' + hmac.new(SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()
        return client.post('/webhook', content=body, headers={
            'content-type': 'application/json',
            'x-hub-signature-256': signature,
        })

    return post
