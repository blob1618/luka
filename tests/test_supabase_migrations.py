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
