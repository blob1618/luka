"""Unit and privacy tests for SQLAlchemy cursor telemetry (STK-232)."""

import asyncio
import logging
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

import app.models.database  # noqa: F401
from app.models.database import (
    receive_after_cursor_execute,
    receive_before_cursor_execute,
)
from app.services.telemetry import (
    finish_message_telemetry,
    get_current_telemetry,
    start_message_telemetry,
)


@pytest.fixture
def sqlite_engine():
    """Isolated local in-memory SQLite engine for unit tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return engine


def test_sql_telemetry_single_query_count_and_timing(sqlite_engine):
    """Verifica que una consulta incremente sql_count en 1 y acumule tiempo en sql_exec_ms."""
    start_message_telemetry("msg-query-timing")
    telemetry = get_current_telemetry()
    assert telemetry is not None
    assert telemetry.sql_count == 0
    assert telemetry.sql_exec_ms == 0.0

    with sqlite_engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    assert telemetry.sql_count == 1
    assert telemetry.sql_exec_ms > 0.0

    finish_message_telemetry("completed")
    assert get_current_telemetry() is None


def test_sql_telemetry_executemany_single_event(sqlite_engine):
    """Verifica que una inserción masiva con executemany compute exactamente 1 en sql_count."""
    with sqlite_engine.begin() as conn:
        conn.execute(text("CREATE TABLE test_batch (id INTEGER, val TEXT)"))

    with sqlite_engine.connect() as conn:
        start_message_telemetry("msg-executemany")
        telemetry = get_current_telemetry()
        assert telemetry is not None
        assert telemetry.sql_count == 0

        conn.execute(
            text("INSERT INTO test_batch (id, val) VALUES (:id, :val)"),
            [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}, {"id": 3, "val": "c"}],
        )

        assert telemetry.sql_count == 1
        assert telemetry.sql_exec_ms > 0.0

        finish_message_telemetry("completed")


def test_sql_telemetry_context_none_graceful_omission():
    """Simula eventos con context=None, confirmando que se incremente sql_count y se omita tiempo sin error."""
    start_message_telemetry("msg-context-none")
    telemetry = get_current_telemetry()
    assert telemetry is not None

    # Simular before_cursor_execute con context=None
    receive_before_cursor_execute(
        conn=None,
        cursor=None,
        statement="SELECT 1",
        parameters={},
        context=None,
        executemany=False,
    )
    assert telemetry.sql_count == 1
    assert telemetry.sql_exec_ms == 0.0

    # Simular after_cursor_execute con context=None
    receive_after_cursor_execute(
        conn=None,
        cursor=None,
        statement="SELECT 1",
        parameters={},
        context=None,
        executemany=False,
    )
    assert telemetry.sql_count == 1
    assert telemetry.sql_exec_ms == 0.0

    finish_message_telemetry("completed")


def test_sql_telemetry_handle_error_no_double_count(sqlite_engine):
    """Provoca un error forzado de BD (IntegrityError) y verifica que se acumule tiempo sin doble conteo."""
    with sqlite_engine.begin() as conn:
        conn.execute(text("CREATE TABLE test_pk (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO test_pk (id) VALUES (1)"))

    with sqlite_engine.connect() as conn:
        start_message_telemetry("msg-error")
        telemetry = get_current_telemetry()
        assert telemetry is not None
        assert telemetry.sql_count == 0

        with pytest.raises(IntegrityError):
            conn.execute(text("INSERT INTO test_pk (id) VALUES (1)"))

        # sql_count debe ser exactamente 1 (no duplicado por handle_error)
        assert telemetry.sql_count == 1
        # La duración debe haberse acumulado en handle_error
        assert telemetry.sql_exec_ms > 0.0

        finish_message_telemetry("error")


def test_sql_telemetry_noop_outside_pipeline(sqlite_engine):
    """Comprueba que consultas ejecutadas sin telemetría activa (None) no generen llamadas ni sobrecarga."""
    assert get_current_telemetry() is None
    with sqlite_engine.connect() as conn:
        result = conn.execute(text("SELECT 42")).scalar()
        assert result == 42
    assert get_current_telemetry() is None


def test_sql_telemetry_concurrency_isolation(sqlite_engine):
    """Ejecuta dos contextos concurrentes mediante asyncio.gather validando que las métricas queden aisladas."""

    async def worker_one():
        start_message_telemetry("worker-1")
        t1 = get_current_telemetry()
        assert t1 is not None

        with sqlite_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            await asyncio.sleep(0.01)
            conn.execute(text("SELECT 2"))

        assert t1.sql_count == 2
        finish_message_telemetry("completed")
        return t1.sql_count

    async def worker_two():
        start_message_telemetry("worker-2")
        t2 = get_current_telemetry()
        assert t2 is not None

        with sqlite_engine.connect() as conn:
            conn.execute(text("SELECT 10"))
            await asyncio.sleep(0.005)
            conn.execute(text("SELECT 20"))
            await asyncio.sleep(0.005)
            conn.execute(text("SELECT 30"))

        assert t2.sql_count == 3
        finish_message_telemetry("completed")
        return t2.sql_count

    async def main():
        return await asyncio.gather(worker_one(), worker_two())

    counts = asyncio.run(main())
    assert counts == [2, 3]


def test_sql_telemetry_strict_privacy_no_identifiers(sqlite_engine, caplog):
    """Inspecciona emisiones y estructuras verificando la ausencia total de message_id, SQL y datos de usuario."""
    caplog.set_level(logging.INFO)
    sensitive_message_id = "wamid.HBgLMzQ5OTk5OTk5ORUCABEYEjEyMzQ1Njc4OTAxMjM0NQ=="
    user_phone = "+5491199998888"
    user_id_str = "user-uuid-secret-1234"
    secret_text = "Compra super secreta en Farmacia 9999"

    start_message_telemetry(sensitive_message_id)
    with sqlite_engine.connect() as conn:
        conn.execute(text("SELECT :secret_val"), {"secret_val": secret_text})

    metrics = finish_message_telemetry("completed")

    # 1. El diccionario retornado por finish() no debe incluir sql_count ni sql_exec_ms
    assert "sql_count" not in metrics
    assert "sql_exec_ms" not in metrics

    # 2. El log de luka.metrics no debe incluir sql_count ni sql_exec_ms
    metrics_records = [r for r in caplog.records if r.name == "luka.metrics"]
    assert len(metrics_records) >= 1
    for r in metrics_records:
        assert "sql_count" not in r.message
        assert "sql_exec_ms" not in r.message

    # 3. Debe existir la emisión anónima en luka.telemetry.sql
    sql_records = [r for r in caplog.records if r.name == "luka.telemetry.sql"]
    assert len(sql_records) == 1
    sql_msg = sql_records[0].message
    assert sql_msg.startswith("[SQL_METRICS_ANONYMOUS]")
    assert "sql_count=1" in sql_msg
    assert "sql_exec_ms=" in sql_msg

    # 4. Prohibición estricta: ninguna información sensible en el canal de SQL
    forbidden_terms = [
        sensitive_message_id,
        user_phone,
        user_id_str,
        secret_text,
        "message_id",
        "wamid",
        "phone",
        "SELECT",
        "secret_val",
        "Farmacia",
    ]
    for term in forbidden_terms:
        assert term not in sql_msg
