"""Endpoints /health, /ready y /version para verificar despliegues."""
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def patch_checks(monkeypatch, *, db=(True, 'ok'), redis=(True, 'ok'), migrations=None):
    monkeypatch.setattr('app.main._database_check', lambda: db)
    monkeypatch.setattr('app.main._redis_check', AsyncMock(return_value=redis))
    monkeypatch.setattr('app.main.deployment_status.migration_status',
                        lambda: migrations or {'state': 'ok', 'local': 2, 'applied': 2, 'pending': []})


def test_health_ok_reports_checks(monkeypatch):
    patch_checks(monkeypatch)
    response = client.get('/health')
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert body['checks']['database'] == {'ok': True, 'detail': 'ok'}
    assert body['checks']['redis'] == {'ok': True, 'detail': 'ok'}
    assert body['checks']['migrations']['state'] == 'ok'


def test_health_stays_200_but_degraded_when_migrations_pending(monkeypatch):
    patch_checks(monkeypatch, migrations={'state': 'pending', 'local': 3, 'applied': 2,
                                          'pending': ['20261028203745']})
    response = client.get('/health')
    assert response.status_code == 200 and response.json()['status'] == 'degraded'


def test_ready_fails_with_pending_migrations(monkeypatch):
    patch_checks(monkeypatch, migrations={'state': 'pending', 'local': 3, 'applied': 2,
                                          'pending': ['20261028203745']})
    response = client.get('/ready')
    assert response.status_code == 503 and response.json()['ready'] is False


def test_ready_fails_without_database(monkeypatch):
    patch_checks(monkeypatch, db=(False, 'OperationalError'))
    assert client.get('/ready').status_code == 503


def test_ready_fails_without_redis(monkeypatch):
    patch_checks(monkeypatch, redis=(False, 'ConnectionError'))
    assert client.get('/ready').status_code == 503


def test_ready_allows_unknown_migration_state_locally(monkeypatch):
    patch_checks(monkeypatch, migrations={'state': 'unknown', 'local': 2, 'applied': None,
                                          'pending': []})
    assert client.get('/ready').status_code == 200


def test_version_reports_commit_and_migration_count(monkeypatch):
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'deadbeef')
    monkeypatch.setenv('RENDER_SERVICE_NAME', 'luka-web')
    body = client.get('/version').json()
    assert body['commit'] == 'deadbeef' and body['service'] == 'luka-web'
    assert body['migrations_local'] >= 2
