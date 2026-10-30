"""Estado de despliegue: drift de migraciones y metadata de versión."""
from app.services import deployment_status


def test_local_migrations_include_receipt_migrations():
    versions = deployment_status.local_migration_versions()
    assert '20261028052643' in versions
    assert '20261028203745' in versions
    assert versions == sorted(versions)


def test_unknown_state_when_supabase_table_is_unavailable(monkeypatch):
    monkeypatch.setattr(deployment_status, 'applied_migration_versions', lambda: None)
    status = deployment_status.migration_status()
    assert status['state'] == 'unknown' and status['pending'] == []


def test_pending_lists_local_migrations_not_applied(monkeypatch):
    local = deployment_status.local_migration_versions()
    monkeypatch.setattr(deployment_status, 'applied_migration_versions', lambda: local[:-1])
    status = deployment_status.migration_status()
    assert status['state'] == 'pending' and status['pending'] == [local[-1]]


def test_ok_when_every_local_migration_is_applied(monkeypatch):
    local = deployment_status.local_migration_versions()
    monkeypatch.setattr(deployment_status, 'applied_migration_versions', lambda: local)
    assert deployment_status.migration_status()['state'] == 'ok'


def test_commit_prefers_render_environment(monkeypatch):
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'abc123')
    monkeypatch.setenv('GIT_COMMIT', 'other')
    assert deployment_status.commit_sha() == 'abc123'


def test_commit_is_unknown_without_environment(monkeypatch):
    monkeypatch.delenv('RENDER_GIT_COMMIT', raising=False)
    monkeypatch.delenv('GIT_COMMIT', raising=False)
    assert deployment_status.commit_sha() == 'unknown'
