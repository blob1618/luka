import re
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).parents[1] / "supabase" / "migrations"


def _migration(suffix: str) -> Path:
    matches = sorted(MIGRATIONS_DIR.glob(f"*_{suffix}.sql"))
    assert len(matches) == 1, f"Se esperaba una migracion {suffix}, encontradas {len(matches)}"
    return matches[0]


def _sql_code(path: Path) -> str:
    """SQL sin lineas de comentario, normalizado a mayusculas."""
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    ]
    return "\n".join(lines).upper()


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


def test_billetera_expansion_migration_contains_schema_guards():
    expansion = _migration("add_billetera_expansion_stk237")
    sql = expansion.read_text(encoding="utf-8").upper()

    assert "CREATE TABLE IF NOT EXISTS PUBLIC.BILLETERA" in sql
    assert "CONSTRAINT BILLETERA_ID_USUARIO_MONEDA_KEY UNIQUE (ID, USUARIO_ID, MONEDA)" in sql
    assert "ALTER TABLE PUBLIC.MOVIMIENTOS_FINANCIEROS" in sql
    assert "ADD COLUMN IF NOT EXISTS BILLETERA_ID UUID" in sql
    assert "FOREIGN KEY (BILLETERA_ID, USUARIO_ID, MONEDA)" in sql
    assert "REFERENCES PUBLIC.BILLETERA (ID, USUARIO_ID, MONEDA)" in sql
    assert "NOT VALID" in sql
    assert "ALTER TABLE PUBLIC.BILLETERA ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON TABLE PUBLIC.BILLETERA FROM ANON, AUTHENTICATED" in sql


def test_billetera_backfill_migration_preserves_expansion_phase():
    backfill = _migration("backfill_billetera_historica_stk237")
    sql = backfill.read_text(encoding="utf-8").upper()

    assert "INSERT INTO PUBLIC.BILLETERA (USUARIO_ID, NOMBRE, MONEDA)" in sql
    assert "'INICIAL'" in sql
    assert "WHERE M.BILLETERA_ID IS NULL" in sql
    assert "SET BILLETERA_ID = B.ID" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "VALIDATE CONSTRAINT MOVIMIENTOS_FINANCIEROS_BILLETERA_FKEY" in sql


def test_billetera_id_index_is_a_later_separate_migration():
    expansion = _migration("add_billetera_expansion_stk237")
    backfill = _migration("backfill_billetera_historica_stk237")
    index_migration = _migration("add_movimientos_billetera_id_index_stk237")

    # El indice debe vivir en un archivo posterior para que la transaccion de
    # expansion libere ACCESS EXCLUSIVE antes de construirlo.
    assert expansion.name < backfill.name < index_migration.name

    index_sql = _sql_code(index_migration)
    assert "CREATE INDEX IF NOT EXISTS MOVIMIENTOS_FINANCIEROS_BILLETERA_ID_IDX" in index_sql
    assert "MOVIMIENTOS_FINANCIEROS (BILLETERA_ID)" in index_sql
    assert "CONCURRENTLY" not in index_sql

    assert "MOVIMIENTOS_FINANCIEROS_BILLETERA_ID_IDX" not in _sql_code(expansion)
    assert "MOVIMIENTOS_FINANCIEROS_BILLETERA_ID_IDX" not in _sql_code(backfill)


def test_billetera_migrations_do_not_apply_final_contract():
    migration_suffixes = (
        "add_billetera_expansion_stk237",
        "backfill_billetera_historica_stk237",
        "add_movimientos_billetera_id_index_stk237",
    )

    for suffix in migration_suffixes:
        sql = _sql_code(_migration(suffix))
        # El contrato final (NOT NULL, contraccion) pertenece al corte STK-240.
        assert "SET NOT NULL" not in sql
        assert "BILLETERA_ID UUID NOT NULL" not in sql
        assert "DROP COLUMN" not in sql
        assert "DROP CONSTRAINT" not in sql

    # El backfill historico no debe filtrar movimientos anulados.
    assert "ANULADO_EN" not in _sql_code(_migration("backfill_billetera_historica_stk237"))


def test_billetera_harness_runs_each_migration_in_its_own_transaction():
    harness = (MIGRATIONS_DIR.parent / "tests" / "stk237_billetera.sql").read_text(
        encoding="utf-8"
    )
    migration_runs = (
        ("20261012174855_add_billetera_expansion_stk237.sql", 1),
        ("20261012174901_backfill_billetera_historica_stk237.sql", 2),
        ("20261012181524_add_movimientos_billetera_id_index_stk237.sql", 1),
    )

    for filename, expected_runs in migration_runs:
        transaction = f"BEGIN;\n\\i supabase/migrations/{filename}\nCOMMIT;"
        assert harness.count(transaction) == expected_runs
