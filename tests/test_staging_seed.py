"""Seed y reset del usuario sintético del E2E de staging."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Billetera, Categoria, LimiteCategoria, MovimientoFinanciero, Usuario

client = TestClient(app)
PHONE = '5491100000001'


@pytest.fixture(autouse=True)
def staging_env(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'staging')
    monkeypatch.setenv('TEST_INGRESS_TOKEN', 'staging-token')
    monkeypatch.setenv('TEST_CAPTURE_PHONES', PHONE)


def _seed(phone=PHONE, token='staging-token'):
    headers = {'x-test-token': token} if token is not None else {}
    return client.post('/__test__/users', json={'phone': phone}, headers=headers)


def test_seed_is_hidden_without_token(monkeypatch):
    monkeypatch.delenv('TEST_INGRESS_TOKEN', raising=False)
    assert _seed().status_code == 404


def test_seed_rejects_wrong_token():
    assert _seed(token='wrong').status_code == 401


def test_seed_rejects_phone_outside_allowlist():
    assert _seed(phone='5491100000999').status_code == 403


def test_seed_rejects_invalid_phone():
    assert _seed(phone='abc').status_code == 400


def test_seed_creates_user_with_one_wallet_and_resets_state(isolated_db):
    created = _seed()
    assert created.status_code == 200
    payload = created.json()
    assert payload['wallet_name'] == 'Pesos' and payload['reset'] is True

    with isolated_db() as session:
        user = session.query(Usuario).filter_by(whatsapp_id=PHONE).one()
        assert user.auth_user_id is None
        wallet = session.query(Billetera).filter_by(usuario_id=user.id).one()
        category = Categoria(usuario_id=user.id, nombre='Comida')
        session.add(category)
        session.flush()
        session.add(MovimientoFinanciero(
            usuario_id=user.id, billetera_id=wallet.id, tipo='egreso',
            cantidad=Decimal('5000'), moneda='ARS', descripcion='nafta',
            fecha_movimiento=date.today()))
        session.add(LimiteCategoria(
            usuario_id=user.id, categoria_id=category.id, cantidad_max=Decimal('3000'),
            moneda='ARS', inicio_periodo=date.today(),
            fin_periodo=date.today() + timedelta(days=30)))
        session.commit()

    reset = _seed()
    assert reset.status_code == 200

    with isolated_db() as session:
        assert session.query(Usuario).filter_by(whatsapp_id=PHONE).count() == 1
        assert session.query(Billetera).filter_by(usuario_id=session.query(Usuario).filter_by(whatsapp_id=PHONE).one().id).count() == 1
        assert session.query(MovimientoFinanciero).count() == 0
        assert session.query(LimiteCategoria).count() == 0
