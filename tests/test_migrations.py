"""Tests exhaustivos para la configuracion de Alembic, baseline, auditoria y adopcion (STK-210)."""

import json
import os
from pathlib import Path
import sys
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic import command
import sqlalchemy as sa

import pytest
from app.models.database import Base
from scripts.audit_schema_adoption import (
    EXPECTED_CHECK_CONSTRAINTS,
    EXPECTED_COLUMNS_CONTRACT,
    audit_schema,
    check_expressions_match,
    compare_audit_reports,
    introspect_schema,
    normalize_database_url,
)
from scripts.adopt_existing_database import adopt_database


def get_alembic_config(connection=None) -> Config:
    root_dir = Path(__file__).resolve().parent.parent
    ini_path = str(root_dir / "alembic.ini")
    config = Config(ini_path)
    config.set_main_option("script_location", str(root_dir / "alembic"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


# ---------------------------------------------------------------------------
# 1. Pruebas de Maquinaria de Alembic
# ---------------------------------------------------------------------------

def test_alembic_single_head():
    """Verifica que el historial de revisiones Alembic tenga exactamente una sola head."""
    config = get_alembic_config()
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, f"Se esperaba exactamente 1 head en Alembic, pero hay {len(heads)}: {heads}"
    assert heads[0] == "20260922_0001"


def test_alembic_revision_chain():
    """Verifica que la revision inicial apunte a base y no tenga dependencias circulares."""
    config = get_alembic_config()
    script = ScriptDirectory.from_config(config)
    rev = script.get_revision("20260922_0001")
    assert rev is not None
    assert rev.down_revision is None


def test_alembic_offline_rendering(capsys):
    """Verifica que Alembic pueda renderizar el DDL completo en modo offline sin errores."""
    config = get_alembic_config()
    command.upgrade(config, "head", sql=True)
    captured = capsys.readouterr()
    assert "CREATE TABLE usuario" in captured.out
    assert "CREATE TABLE movimientos_financieros" in captured.out
    assert "INSERT INTO alembic_version" in captured.out


def test_alembic_sqlite_upgrade_and_downgrade():
    """Verifica que la baseline de Alembic aplique y revierta sobre SQLite sin romper."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    with engine.connect() as connection:
        config = get_alembic_config(connection=connection)
        command.upgrade(config, "head")

        inspector = sa.inspect(connection)
        tables = set(inspector.get_table_names())

        expected_tables = {
            "usuario",
            "onboarding_invitacion",
            "dashboard_login_link",
            "acuerdo_version",
            "acuerdo_aceptado",
            "categorias",
            "limite_categoria",
            "candidato_gasto_recurrente",
            "recordatorio",
            "evento",
            "movimientos_financieros",
            "conversation_flow",
            "conversation_flow_version",
            "aviso_recordatorio",
            "cron_job_claim",
            "alembic_version",
        }
        missing = expected_tables - tables
        assert not missing, f"Faltan tablas tras upgrade en SQLite: {missing}"

        command.downgrade(config, "base")
        inspector = sa.inspect(connection)
        tables_after = set(inspector.get_table_names()) - {"alembic_version"}
        assert not tables_after, f"Quedaron tablas tras downgrade en SQLite: {tables_after}"


# ---------------------------------------------------------------------------
# 2. Pruebas de Auditoria de Esquema y Deteccion de Drift Material
# ---------------------------------------------------------------------------

def test_audit_detects_missing_tables():
    """Demuestra que la falta de tablas materiales genera paridad falsa."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    # Creamos solo 2 tablas de las 15
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE usuario (id text primary key, nombre text, email text);"))
        conn.execute(sa.text("CREATE TABLE categorias (id text primary key, nombre text);"))

    with engine.connect() as conn:
        report = audit_schema(conn)
        assert report["is_parity_confirmed"] is False
        assert any("Tablas faltantes" in d for d in report["discrepancies"])


def test_audit_detects_missing_column(monkeypatch):
    """Demuestra que la omision de una columna requerida (ej. anulado_en) genera paridad falsa y reporte."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        report_orig = audit_schema(conn)
        assert report_orig["is_parity_confirmed"] is True

        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            data["tables"]["movimientos_financieros"].pop("anulado_en", None)
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report_tampered = audit_schema(conn)

        assert report_tampered["is_parity_confirmed"] is False
        assert any("movimientos_financieros" in d and "anulado_en" in d for d in report_tampered["discrepancies"])
        assert report_orig["fingerprint_sha256"] != report_tampered["fingerprint_sha256"]


def test_audit_detects_missing_rls_and_auth_fk_in_postgres_simulation(monkeypatch):
    """Demuestra que en PostgreSQL la ausencia de RLS o auth_fk arroja paridad falsa y discrepancias."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            data["dialect"] = "postgresql"
            data["rls"] = {"usuario": True}  # Solo 1 de 12 tiene RLS
            data["auth_fk_present"] = False  # Falta FK a auth.users
            data["extensions"] = ["uuid-ossp"]
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("Tabla protegida sin RLS activo" in d for d in report["discrepancies"])
        assert any("Falta FK usuario.auth_user_id -> auth.users(id)" in d for d in report["discrepancies"])
        assert len(report["fingerprint_sha256"]) == 64


def test_tampered_column_type_fails_parity_and_blocks_adoption(monkeypatch):
    """Demuestra que la alteracion de un tipo de columna en PostgreSQL genera paridad falsa y bloquea adopcion."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            data["dialect"] = "postgresql"
            # Simulamos que usuario.nombre cambio de text a int
            data["tables"]["usuario"]["nombre"]["type"] = "int"
            data["rls"] = {t: True for t in Base.metadata.tables}
            data["auth_fk_present"] = True
            data["extensions"] = ["uuid-ossp"]
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("type mismatch" in d for d in report["discrepancies"])

        # Intento de adopcion debe ser bloqueado
        blocked = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=True,
        )
        assert blocked is False


def test_tampered_column_default_fails_parity_and_blocks_adoption(monkeypatch):
    """Demuestra que la alteracion o perdida de un server default genera paridad falsa y bloquea adopcion."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            data["dialect"] = "postgresql"
            data["rls"] = {t: True for t in Base.metadata.tables}
            data["auth_fk_present"] = True
            data["extensions"] = ["uuid-ossp"]
            for t, cols in EXPECTED_COLUMNS_CONTRACT.items():
                if t in data["tables"]:
                    for col_name, meta in cols.items():
                        if col_name in data["tables"][t]:
                            data["tables"][t][col_name]["default"] = meta.get("default")
                            data["tables"][t][col_name]["has_default"] = meta.get("default") is not None
                            data["tables"][t][col_name]["type"] = meta.get("type")
            # Simulamos que recordatorio.estado perdio su default 'activo'
            data["tables"]["recordatorio"]["estado"]["has_default"] = False
            data["tables"]["recordatorio"]["estado"]["default"] = None
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("default faltante" in d for d in report["discrepancies"])

        blocked = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=True,
        )
        assert blocked is False


def test_tampered_check_constraint_fails_parity_and_blocks_adoption(monkeypatch):
    """Demuestra que la eliminacion o alteracion de un check constraint genera paridad falsa y bloquea adopcion."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            # Eliminamos el check constraint de limite_categoria
            data["check_constraints"]["limite_categoria"] = []
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("faltan check constraints" in d for d in report["discrepancies"])

        blocked = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=True,
        )
        assert blocked is False


def test_tampered_index_predicate_fails_parity_and_blocks_adoption(monkeypatch):
    """Demuestra que la alteracion del predicado WHERE en un indice parcial genera paridad falsa y bloquea adopcion."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            # Alteramos el predicado de recordatorio_usuario_estado_idx
            for idx in data["indexes"].get("recordatorio", []):
                if idx.get("name") == "recordatorio_usuario_estado_idx":
                    idx["predicate"] = "estado = 'pausado'"
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("predicado mismatch" in d for d in report["discrepancies"])

        blocked = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=True,
        )
        assert blocked is False


def test_tampered_privilege_fails_parity_and_blocks_adoption(monkeypatch):
    """Demuestra que privilegios no revocados para anon/authenticated en tablas sensibles bloquean adopcion."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            data["dialect"] = "postgresql"
            # Simulamos que anon recibio SELECT en candidato_gasto_recurrente
            data["revoked_privileges"] = ["candidato_gasto_recurrente:anon:SELECT"]
            data["rls"] = {t: True for t in Base.metadata.tables}
            data["auth_fk_present"] = True
            data["extensions"] = ["uuid-ossp"]
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("Privilegios no revocados" in d for d in report["discrepancies"])

        blocked = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=True,
        )
        assert blocked is False


def test_alembic_version_table_does_not_alter_fingerprint_or_cause_parity_failure():
    """Verifica que la presencia de la tabla alembic_version sea excluida y no altere la huella digital ni la paridad."""
    engine = sa.create_engine(
        "sqlite://",
        poolclass=sa.pool.StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)

    # 1. Auditoria antes de alembic_version
    with engine.connect() as conn:
        report_before = audit_schema(conn)
        assert report_before["is_parity_confirmed"] is True
        fp_before = report_before["fingerprint_sha256"]

    # 2. Creamos y poblamos alembic_version
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);"))
        conn.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES ('20260922_0001');"))

    # 3. Auditoria despues de alembic_version
    with engine.connect() as conn:
        report_after = audit_schema(conn)
        assert report_after["is_parity_confirmed"] is True
        fp_after = report_after["fingerprint_sha256"]

        # La huella debe ser exactamente identica
        assert fp_before == fp_after
        # No debe haber discrepancias de tablas no reconocidas
        assert not any("alembic_version" in d for d in report_after["discrepancies"])


def test_audit_schema_from_snapshot_json(tmp_path):
    """Verifica que audit_schema pueda procesar un snapshot JSON offline y detectar discrepancias."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_report = audit_schema(conn)
        schema_snapshot = orig_report["schema_data"]

    # 1. Snapshot valido
    snap_file = tmp_path / "valid_snapshot.json"
    snap_file.write_text(json.dumps({"version": "1.0", "schema_data": schema_snapshot}), encoding="utf-8")

    with open(snap_file, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    rep_snap = audit_schema(snapshot_data=loaded)
    assert rep_snap["is_parity_confirmed"] is True
    assert rep_snap["fingerprint_sha256"] == orig_report["fingerprint_sha256"]

    # 2. Snapshot con tabla faltante
    tampered_snapshot = json.loads(json.dumps(schema_snapshot))
    tampered_snapshot["tables"].pop("usuario")
    bad_snap_file = tmp_path / "bad_snapshot.json"
    bad_snap_file.write_text(json.dumps(tampered_snapshot), encoding="utf-8")

    with open(bad_snap_file, "r", encoding="utf-8") as f:
        loaded_bad = json.load(f)
    rep_bad = audit_schema(snapshot_data=loaded_bad)
    assert rep_bad["is_parity_confirmed"] is False
    assert any("Tablas faltantes" in d and "usuario" in d for d in rep_bad["discrepancies"])


def test_reflection_error_causes_parity_failure(monkeypatch):
    """Demuestra que ante reflexion incompleta o errores, la auditoria falla sin declarar paridad."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            data["reflection_errors"] = ["Error de reflexion en tabla 'usuario': connection error"]
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("Error de reflexion en tabla 'usuario'" in d for d in report["discrepancies"])


def test_normalize_database_url():
    """Verifica que postgresql:// y postgres:// se normalicen a postgresql+psycopg://."""
    assert normalize_database_url("postgresql://user:pass@localhost:5432/db") == "postgresql+psycopg://user:pass@localhost:5432/db"
    assert normalize_database_url("postgres://user:pass@localhost:5432/db") == "postgresql+psycopg://user:pass@localhost:5432/db"
    assert normalize_database_url("sqlite:///./luka.db") == "sqlite:///./luka.db"


def test_compare_audit_reports(tmp_path):
    """Verifica la funcion compare_audit_reports tanto en caso de paridad exitosa como de discrepancia."""
    rep1 = {
        "is_parity_confirmed": True,
        "fingerprint_sha256": "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "discrepancies": [],
    }
    rep2 = dict(rep1)

    f1 = tmp_path / "rep1.json"
    f2 = tmp_path / "rep2.json"
    f1.write_text(json.dumps(rep1), encoding="utf-8")
    f2.write_text(json.dumps(rep2), encoding="utf-8")

    assert compare_audit_reports(str(f1), str(f2)) is True

    # Reporte con discrepancia de huella
    rep_diff = dict(rep1)
    rep_diff["fingerprint_sha256"] = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    f_diff = tmp_path / "diff.json"
    f_diff.write_text(json.dumps(rep_diff), encoding="utf-8")

    assert compare_audit_reports(str(f1), str(f_diff)) is False


def test_adopt_script_import_without_scripts_in_syspath(monkeypatch):
    """Verifica que adopt_existing_database resuelva sys.path correctamente sin requerir 'scripts' en root."""
    import importlib.util

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "adopt_existing_database.py"
    # Simulamos que 'scripts' no esta en sys.path
    monkeypatch.setattr(sys, "path", [str(Path(__file__).resolve().parent.parent / "scripts")])
    spec = importlib.util.spec_from_file_location("adopt_existing_database_module", str(script_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "adopt_database")


# ---------------------------------------------------------------------------
# 3. Pruebas de Adopcion Verificable y Guardas Estrictas
# ---------------------------------------------------------------------------

def test_adopt_blocked_without_verified_backup():
    """Verifica que la adopcion aborte si no se confirma el respaldo manual (backup_verified=False)."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        report = audit_schema(conn)
        # backup_verified es False
        success = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=False,
        )
        assert success is False


def test_adopt_blocked_without_valid_parity_report():
    """Verifica que la adopcion aborte si el reporte no confirma paridad."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        report = audit_schema(conn)
        report["is_parity_confirmed"] = False  # Simulamos paridad rechazada
        success = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=True,
        )
        assert success is False


def test_adopt_blocked_on_missing_or_mismatched_fingerprint():
    """Verifica que la adopcion aborte ante ausencia o discordancia de fingerprint."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        report = audit_schema(conn)

        # 1. Ausencia de expected_fingerprint
        assert adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=None,
            backup_verified=True,
        ) is False

        # 2. Mismatch con reporte
        assert adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint="fingerprint_falso_123456",
            backup_verified=True,
        ) is False

        # 3. Mismatch en vivo: reporte dice A, pero la base en vivo tiene B
        tampered_report = dict(report)
        tampered_report["fingerprint_sha256"] = "fingerprint_antiguo_invalido"
        assert adopt_database(
            connection=conn,
            parity_report=tampered_report,
            expected_fingerprint="fingerprint_antiguo_invalido",
            backup_verified=True,
        ) is False


def test_adopt_successful_and_persisted_in_fresh_connection():
    """Verifica adopcion exitosa con todas las guardas y comprueba persistencia desde una conexion nueva."""
    engine = sa.create_engine(
        "sqlite://",
        poolclass=sa.pool.StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)

    # 1. Obtener reporte y fingerprint legitimos
    with engine.connect() as conn1:
        report = audit_schema(conn1)
        valid_fp = report["fingerprint_sha256"]
        report["is_parity_confirmed"] = True

        success = adopt_database(
            connection=conn1,
            parity_report=report,
            expected_fingerprint=valid_fp,
            backup_verified=True,
        )
        assert success is True

    # 2. Verificar persistencia de alembic_version desde una CONEXION COMPLETAMENTE NUEVA
    with engine.connect() as fresh_conn:
        inspector = sa.inspect(fresh_conn)
        assert "alembic_version" in inspector.get_table_names()
        res = fresh_conn.execute(sa.text("SELECT version_num FROM alembic_version;"))
        rows = [r[0] for r in res]
        assert rows == ["20260922_0001"]

        # 3. Reintento de adopcion sobre la misma base debe abortar por estado conocido previo
        retry_success = adopt_database(
            connection=fresh_conn,
            parity_report=report,
            expected_fingerprint=valid_fp,
            backup_verified=True,
        )
        assert retry_success is False


def test_unexpected_column_default_fails_parity_and_blocks_adoption(monkeypatch):
    """Demuestra que la presencia de un default inesperado en una columna sin default genera paridad falsa."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            data["dialect"] = "postgresql"
            data["rls"] = {t: True for t in Base.metadata.tables}
            data["auth_fk_present"] = True
            data["extensions"] = ["uuid-ossp"]
            for t, cols in EXPECTED_COLUMNS_CONTRACT.items():
                if t in data["tables"]:
                    for col_name, meta in cols.items():
                        if col_name in data["tables"][t]:
                            data["tables"][t][col_name]["default"] = meta.get("default")
                            data["tables"][t][col_name]["has_default"] = meta.get("default") is not None
                            data["tables"][t][col_name]["type"] = meta.get("type")
            # Inyectamos default inesperado en usuario.nombre (que espera default: None)
            data["tables"]["usuario"]["nombre"]["has_default"] = True
            data["tables"]["usuario"]["nombre"]["default"] = "'invitado_anonimo'"
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("default inesperado" in d and "nombre" in d for d in report["discrepancies"])

        blocked = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=True,
        )
        assert blocked is False


def test_tampered_check_constraint_expression_fails_parity_and_blocks_adoption(monkeypatch):
    """Demuestra que un check con mismo nombre pero expresion distinta genera paridad falsa y bloquea adopcion."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            data["dialect"] = "postgresql"
            data["rls"] = {t: True for t in Base.metadata.tables}
            data["auth_fk_present"] = True
            data["extensions"] = ["uuid-ossp"]
            for t, cols in EXPECTED_COLUMNS_CONTRACT.items():
                if t in data["tables"]:
                    for col_name, meta in cols.items():
                        if col_name in data["tables"][t]:
                            data["tables"][t][col_name]["default"] = meta.get("default")
                            data["tables"][t][col_name]["has_default"] = meta.get("default") is not None
                            data["tables"][t][col_name]["type"] = meta.get("type")
            # Alteramos la expresion de movimientos_financieros_cantidad_check manteniendo su nombre
            for chk in data["check_constraints"].get("movimientos_financieros", []):
                if chk.get("name") == "movimientos_financieros_cantidad_check":
                    chk["sqltext"] = "cantidad > 1000"
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("expresion mismatch" in d and "movimientos_financieros_cantidad_check" in d for d in report["discrepancies"])

        blocked = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=True,
        )
        assert blocked is False


def test_dropped_index_predicate_fails_parity_and_blocks_adoption(monkeypatch):
    """Demuestra que la eliminacion del predicado WHERE (convirtiendolo en indice total) genera paridad falsa."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            # Quitamos el predicado de recordatorio_usuario_estado_idx (dejandolo en None/vacio)
            for idx in data["indexes"].get("recordatorio", []):
                if idx.get("name") == "recordatorio_usuario_estado_idx":
                    idx["predicate"] = None
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any("predicado mismatch" in d and "recordatorio_usuario_estado_idx" in d for d in report["discrepancies"])

        blocked = adopt_database(
            connection=conn,
            parity_report=report,
            expected_fingerprint=report["fingerprint_sha256"],
            backup_verified=True,
        )
        assert blocked is False


def test_snapshot_matches_direct_introspection_fingerprint_and_parity(tmp_path):
    """Demuestra que un snapshot de catalogo (con tipos/defaults raw de Postgres y filas nulas depuradas) produce la misma huella SHA-256 y paridad que la introspeccion directa."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        direct_data = introspect_schema(conn)
        direct_data["dialect"] = "postgresql"
        direct_data["rls"] = {t: True for t in Base.metadata.tables}
        direct_data["auth_fk_present"] = True
        direct_data["extensions"] = ["uuid-ossp"]
        for t, cols in EXPECTED_COLUMNS_CONTRACT.items():
            if t in direct_data["tables"]:
                for col_name, meta in cols.items():
                    if col_name in direct_data["tables"][t]:
                        direct_data["tables"][t][col_name]["default"] = meta.get("default")
                        direct_data["tables"][t][col_name]["has_default"] = meta.get("default") is not None
                        direct_data["tables"][t][col_name]["type"] = meta.get("type")

        for t, checks in EXPECTED_CHECK_CONSTRAINTS.items():
            direct_data["check_constraints"][t] = [{"name": name, "sqltext": expr} for name, expr in checks.items()]

        direct_report = audit_schema(snapshot_data={"dialect": "postgresql", "schema_data": direct_data})
        assert direct_report["is_parity_confirmed"] is True

        # Simulamos snapshot generado por export_supabase_catalog_snapshot.sql con tipos crudos de Postgres
        # y filas nulas de LEFT JOIN para verificar que normalize_schema_data las depura
        snapshot_payload = json.loads(json.dumps(direct_data))
        snapshot_payload["tables"]["limite_categoria"]["moneda"]["type"] = "character varying(3)"
        snapshot_payload["tables"]["limite_categoria"]["moneda"]["default"] = "'ARS'::text"
        snapshot_payload["tables"]["usuario"]["creado_en"]["type"] = "timestamp with time zone"
        snapshot_payload["foreign_keys"]["usuario"].append({"name": None, "constrained_columns": None, "referred_table": None})
        snapshot_payload["check_constraints"]["usuario"].append({"name": None, "sqltext": None})
        snapshot_payload["unique_constraints"]["usuario"].append({"name": None, "columns": None})
        snapshot_payload["indexes"]["usuario"].append({"name": None, "columns": []})

        snap_file = tmp_path / "pg_catalog_snapshot.json"
        snap_file.write_text(json.dumps({"version": "1.0", "dialect": "postgresql", "schema_data": snapshot_payload}), encoding="utf-8")

        with open(snap_file, "r", encoding="utf-8") as f:
            loaded_snap = json.load(f)

        snap_report = audit_schema(snapshot_data=loaded_snap)
        assert snap_report["is_parity_confirmed"] is True
        # La huella digital DEBE ser 100% identica a la obtenida por introspeccion
        assert snap_report["fingerprint_sha256"] == direct_report["fingerprint_sha256"]


def test_real_postgresql_decompiled_check_expressions_match_contract():
    """Verifica que expresiones reales de PostgreSQL descompiladas por pg_get_constraintdef coincidan con el contrato."""
    pg_decompiled_cases = [
        # 1. dia_estimado BETWEEN 1 AND 31 -> ((dia_estimado >= 1) AND (dia_estimado <= 31))
        ("dia_estimado BETWEEN 1 AND 31", "CHECK (((dia_estimado >= 1) AND (dia_estimado <= 31)))"),
        # 2. dias_anticipacion BETWEEN 1 AND 30 -> ((dias_anticipacion >= 1) AND (dias_anticipacion <= 30))
        ("dias_anticipacion BETWEEN 1 AND 30", "CHECK (((dias_anticipacion >= 1) AND (dias_anticipacion <= 30)))"),
        # 3. trim(slug) <> '' -> btrim(slug) <> ''::text
        ("trim(slug) <> ''", "CHECK ((btrim(slug) <> ''::text))"),
        # 4. trim(token_hash) <> '' -> btrim(token_hash) <> ''::text
        ("trim(token_hash) <> ''", "CHECK ((btrim(token_hash) <> ''::text))"),
        # 5. length(moneda) = 3 AND moneda = upper(moneda) -> char_length(moneda::text) = 3 AND moneda::text = upper(moneda::text)
        (
            "length(moneda) = 3 AND moneda = upper(moneda)",
            "CHECK (((char_length((moneda)::text) = 3) AND ((moneda)::text = upper((moneda)::text))))",
        ),
        # 6. IN (...) -> = ANY (ARRAY[...])
        (
            "estado IN ('pendiente', 'consumido', 'vencido')",
            "CHECK ((estado = ANY (ARRAY['pendiente'::text, 'consumido'::text, 'vencido'::text])))",
        ),
        (
            "estado IN ('pendiente', 'aceptado', 'rechazado', 'pausado', 'desactivado', 'invalidado')",
            "CHECK ((estado = ANY (ARRAY['pendiente'::text, 'aceptado'::text, 'rechazado'::text, 'pausado'::text, 'desactivado'::text, 'invalidado'::text])))",
        ),
        # 7. cantidad_max >= 0 -> cantidad_max >= (0)::numeric
        ("cantidad_max >= 0", "CHECK ((cantidad_max >= (0)::numeric))"),
        # 8. cantidad > 0 -> cantidad > (0)::numeric
        ("cantidad > 0", "CHECK ((cantidad > (0)::numeric))"),
        # 9. version_number > 0 -> version_number > 0
        ("version_number > 0", "CHECK ((version_number > 0))"),
        # 10. periodo regex
        ("periodo ~ '^\\d{4}-(0[1-9]|1[0-2])$'", "CHECK ((periodo ~ '^\\d{4}-(0[1-9]|1[0-2])$'::text))"),
        # 11. intentos >= 0 AND intentos <= max_intentos
        (
            "intentos >= 0 AND intentos <= max_intentos",
            "CHECK (((intentos >= 0) AND (intentos <= max_intentos)))",
        ),
        # 12. campos condicionales compuestos (con y sin parentesis en ANDs)
        (
            "(estado = 'pendiente' AND consumido_en IS NULL) OR (estado = 'consumido' AND consumido_en IS NOT NULL) OR (estado = 'vencido' AND consumido_en IS NULL)",
            "CHECK ((((estado = 'pendiente'::text) AND (consumido_en IS NULL)) OR ((estado = 'consumido'::text) AND (consumido_en IS NOT NULL)) OR ((estado = 'vencido'::text) AND (consumido_en IS NULL))))",
        ),
        (
            "estado = 'pendiente' AND consumido_en IS NULL OR estado = 'consumido' AND consumido_en IS NOT NULL OR estado = 'vencido' AND consumido_en IS NULL",
            "CHECK ((estado = 'pendiente'::text AND consumido_en IS NULL OR estado = 'consumido'::text AND consumido_en IS NOT NULL OR estado = 'vencido'::text AND consumido_en IS NULL))",
        ),
        (
            "(estado = 'pendiente' AND consumido_en IS NULL) OR (estado = 'consumido' AND consumido_en IS NOT NULL) OR (estado = 'vencido' AND consumido_en IS NULL)",
            "CHECK ((estado = 'pendiente'::text AND consumido_en IS NULL OR estado = 'consumido'::text AND consumido_en IS NOT NULL OR estado = 'vencido'::text AND consumido_en IS NULL))",
        ),
        (
            "status = 'draft' AND published_at IS NULL OR status IN ('published', 'retired') AND published_at IS NOT NULL",
            "CHECK ((status = 'draft'::text AND published_at IS NULL OR (status = ANY (ARRAY['published'::text, 'retired'::text])) AND published_at IS NOT NULL))",
        ),
        (
            "(status = 'draft' AND published_at IS NULL) OR (status IN ('published', 'retired') AND published_at IS NOT NULL)",
            "CHECK ((status = 'draft'::text AND published_at IS NULL OR (status = ANY (ARRAY['published'::text, 'retired'::text])) AND published_at IS NOT NULL))",
        ),
        (
            "estado = 'pendiente' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL OR estado = 'consumida' AND usuario_id IS NOT NULL AND consumida_en IS NOT NULL AND revocada_en IS NULL OR estado = 'revocada' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NOT NULL OR estado = 'vencida' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL",
            "CHECK ((estado = 'pendiente'::text AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL OR estado = 'consumida'::text AND usuario_id IS NOT NULL AND consumida_en IS NOT NULL AND revocada_en IS NULL OR estado = 'revocada'::text AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NOT NULL OR estado = 'vencida'::text AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL))",
        ),
        (
            "(estado = 'pendiente' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL) OR (estado = 'consumida' AND usuario_id IS NOT NULL AND consumida_en IS NOT NULL AND revocada_en IS NULL) OR (estado = 'revocada' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NOT NULL) OR (estado = 'vencida' AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL)",
            "CHECK ((estado = 'pendiente'::text AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL OR estado = 'consumida'::text AND usuario_id IS NOT NULL AND consumida_en IS NOT NULL AND revocada_en IS NULL OR estado = 'revocada'::text AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NOT NULL OR estado = 'vencida'::text AND usuario_id IS NULL AND consumida_en IS NULL AND revocada_en IS NULL))",
        ),
    ]

    for exp, act in pg_decompiled_cases:
        assert check_expressions_match(exp, act) is True, f"Fallo coincidencia para: exp='{exp}' vs act='{act}'"


def test_operator_semantic_differences_are_strictly_rejected():
    """Verifica que diferencias semanticas en operadores (<, >, <=, >=, BETWEEN, logica) no sean normalizadas falsamente."""
    semantic_mismatches = [
        # Operador estricto > vs >=
        ("version_number > 0", "CHECK ((version_number >= 0))"),
        # Operador estricto >= vs >
        ("cantidad_max >= 0", "CHECK ((cantidad_max > (0)::numeric))"),
        # Limite inferior de BETWEEN alterado (> en vez de >=)
        ("dia_estimado BETWEEN 1 AND 31", "CHECK (((dia_estimado > 1) AND (dia_estimado <= 31)))"),
        # Limite superior de BETWEEN alterado (< en vez de <=)
        ("dia_estimado BETWEEN 1 AND 31", "CHECK (((dia_estimado >= 1) AND (dia_estimado < 31)))"),
        # Rango numerico de dias alterado (30 vs 31)
        ("dias_anticipacion BETWEEN 1 AND 30", "CHECK (((dias_anticipacion >= 1) AND (dias_anticipacion <= 31)))"),
        # Valores de estado distintos en IN vs ANY ARRAY
        ("estado IN ('draft', 'published')", "CHECK ((estado = ANY (ARRAY['draft'::text, 'published'::text, 'archived'::text])))"),
        ("estado IN ('draft', 'published', 'retired')", "CHECK ((estado = ANY (ARRAY['draft'::text, 'published'::text, 'archived'::text])))"),
        # Precedencia / agrupamiento logico alterado: (A AND B) OR C vs A AND (B OR C)
        ("(a = 1 AND b = 2) OR c = 3", "CHECK ((a = 1 AND (b = 2 OR c = 3)))"),
        ("(a = 1 OR b = 2) AND c = 3", "CHECK ((a = 1 OR (b = 2 AND c = 3)))"),
    ]

    for exp, act in semantic_mismatches:
        assert check_expressions_match(exp, act) is False, f"Se esperaba rechazo semantico para: exp='{exp}' vs act='{act}'"


def test_tampered_operator_in_postgres_simulation_fails_parity(monkeypatch):
    """Demuestra que relajar un operador (> por >=) en la auditoria de PostgreSQL genera paridad falsa."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        orig_introspect = introspect_schema

        def mock_introspect(c):
            data = orig_introspect(c)
            data["dialect"] = "postgresql"
            data["rls"] = {t: True for t in Base.metadata.tables}
            data["auth_fk_present"] = True
            data["extensions"] = ["uuid-ossp"]
            for t, cols in EXPECTED_COLUMNS_CONTRACT.items():
                if t in data["tables"]:
                    for col_name, meta in cols.items():
                        if col_name in data["tables"][t]:
                            data["tables"][t][col_name]["default"] = meta.get("default")
                            data["tables"][t][col_name]["has_default"] = meta.get("default") is not None
                            data["tables"][t][col_name]["type"] = meta.get("type")
            # Simulamos que conversation_flow_version_number_check se relajo a >= 0 en lugar de > 0
            for chk in data["check_constraints"].get("conversation_flow_version", []):
                if chk.get("name") == "conversation_flow_version_number_check":
                    chk["sqltext"] = "version_number >= 0"
            return data

        monkeypatch.setattr("scripts.audit_schema_adoption.introspect_schema", mock_introspect)
        report = audit_schema(conn)

        assert report["is_parity_confirmed"] is False
        assert any(
            "expresion mismatch" in d and "conversation_flow_version_number_check" in d
            for d in report["discrepancies"]
        )


def test_real_supabase_catalog_snapshot_parity_regression(pytestconfig):
    """Regresion offline usando un snapshot real de Supabase si se provee explicitamente via --supabase-snapshot o LUKA_SUPABASE_SNAPSHOT_PATH."""
    snapshot_arg = pytestconfig.getoption("--supabase-snapshot", default=None)
    snapshot_env = os.getenv("LUKA_SUPABASE_SNAPSHOT_PATH")
    snapshot_target = snapshot_arg or snapshot_env

    if not snapshot_target:
        pytest.skip(
            "Prueba opcional omitida: No se configuro una ruta de snapshot. "
            "Para ejecutar esta prueba contra un snapshot real fuera del repositorio, use: "
            "`--supabase-snapshot <ruta>` o defina la variable `LUKA_SUPABASE_SNAPSHOT_PATH`."
        )

    snapshot_path = Path(snapshot_target).resolve()
    if not snapshot_path.is_file():
        pytest.skip(
            f"Prueba opcional omitida: El archivo de snapshot especificado en "
            f"'{snapshot_path}' no existe o no es un archivo valido."
        )

    with open(snapshot_path, "r", encoding="utf-8") as f:
        snapshot_data = json.load(f)

    report = audit_schema(snapshot_data=snapshot_data)
    assert report["is_parity_confirmed"] is True, f"Discrepancias inesperadas: {report.get('discrepancies')}"
    assert len(report["discrepancies"]) == 0
    assert report["fingerprint_sha256"] == "f8d46bb4edeaa25b16fd2a0f2a27610a4a9edd8825ab469cd31bd899bc240702"
