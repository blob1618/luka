import re
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).parents[1] / "supabase" / "migrations"


def test_supabase_migrations_use_timestamped_forward_only_files():
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))

    assert migrations
    assert all(re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", path.name) for path in migrations)
    assert not list(MIGRATIONS_DIR.glob("*.rollback.sql"))


def test_baseline_contains_production_schema_guards():
    baseline = MIGRATIONS_DIR / "20260911010815_baseline_remote_schema.sql"
    sql = baseline.read_text(encoding="utf-8").upper()

    assert 'CREATE TABLE IF NOT EXISTS "PUBLIC"."MOVIMIENTOS_FINANCIEROS"' in sql
    assert 'CREATE TABLE IF NOT EXISTS "PUBLIC"."USUARIO"' in sql
    assert 'CREATE UNIQUE INDEX "CATEGORIAS_USUARIO_NOMBRE_ACTIVO_UIDX"' in sql
    assert 'ALTER TABLE "PUBLIC"."USUARIO" ENABLE ROW LEVEL SECURITY' in sql

    protections = MIGRATIONS_DIR / "20260911011601_protect_movimientos_financieros.sql"
    protections_sql = protections.read_text(encoding="utf-8").upper()
    assert "ALTER TABLE PUBLIC.MOVIMIENTOS_FINANCIEROS ENABLE ROW LEVEL SECURITY" in protections_sql
    assert "CREATE UNIQUE INDEX MOVIMIENTOS_FINANCIEROS_WHATSAPP_MESSAGE_ID_UIDX" in protections_sql
    assert "CREATE INDEX MOVIMIENTOS_FINANCIEROS_USUARIO_FECHA_IDX" in protections_sql


def test_legacy_financial_tables_are_removed_without_cascade():
    cleanup = MIGRATIONS_DIR / "20260919120000_drop_legacy_metas_movimientos.sql"
    sql = cleanup.read_text(encoding="utf-8").upper()

    assert "DROP TABLE IF EXISTS PUBLIC.METAS;" in sql
    assert "DROP TABLE IF EXISTS PUBLIC.MOVIMIENTOS;" in sql
    assert "CASCADE" not in sql


def test_recurring_expense_candidates_migration_contains_schema_guards():
    migration = MIGRATIONS_DIR / "20260920190000_add_recurring_expense_candidates.sql"
    sql = migration.read_text(encoding="utf-8").upper()

    assert "CREATE TABLE IF NOT EXISTS PUBLIC.CANDIDATO_GASTO_RECURRENTE" in sql
    assert "CANDIDATO_GASTO_RECURRENTE_USUARIO_PATRON_KEY UNIQUE (USUARIO_ID, PATRON_HASH)" in sql
    assert "CANDIDATO_GASTO_RECURRENTE_ESTADO_CHECK" in sql
    assert "'INVALIDADO'" in sql
    assert "CANDIDATO_GASTO_RECURRENTE_PATRON_HASH_LEN_CHECK" in sql
    assert "MOVIMIENTOS_FINANCIEROS_EGRESOS_ACTIVOS_FECHA_IDX" in sql
    assert "ALTER TABLE PUBLIC.CANDIDATO_GASTO_RECURRENTE ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON TABLE PUBLIC.CANDIDATO_GASTO_RECURRENTE FROM ANON, AUTHENTICATED" in sql


def test_recurring_confirmation_delivery_migration_contains_schema_guards():
    migration = next(MIGRATIONS_DIR.glob("*_recurring_confirmation_delivery.sql"))
    sql = migration.read_text(encoding="utf-8").upper()

    assert "CREATE TABLE IF NOT EXISTS PUBLIC.AVISO_RECORDATORIO" in sql
    assert "AVISO_RECORDATORIO_USUARIO_PATRON_PERIODO_KEY" in sql
    assert "AVISO_RECORDATORIO_ESTADO_CHECK" in sql
    assert "RECORDATORIO_CANDIDATO_ID_KEY UNIQUE (CANDIDATO_ID)" in sql
    assert "RECORDATORIO_CANDIDATO_ID_FKEY" in sql
    assert "RECORDATORIO_DIAS_ANTICIPACION_CHECK" in sql
    assert "RECORDATORIO_ORIGEN_CHECK" in sql
    assert "CREATE TABLE IF NOT EXISTS PUBLIC.CRON_JOB_CLAIM" in sql
    assert "ALTER TABLE PUBLIC.AVISO_RECORDATORIO ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON TABLE PUBLIC.AVISO_RECORDATORIO FROM ANON, AUTHENTICATED" in sql
    assert "ALTER TABLE PUBLIC.CRON_JOB_CLAIM ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON TABLE PUBLIC.CRON_JOB_CLAIM FROM ANON, AUTHENTICATED" in sql
