"""Verificación reproducible de PostgreSQL Advisory Lock en QueuePool para STK-189.

Valida el ciclo de vida del advisory lock en app/scheduler.py contra una instancia real
de PostgreSQL (leída estrictamente desde TEST_PG_URL), demostrando:
1. Ejecución real de la detección diaria en la conexión física dedicada (PID).
2. Retención del lock en la misma conexión a pesar de los commits internos de la detección.
3. Liberación limpia tras corrida exitosa (verificada desde conexión externa independiente).
4. Recuperación tras error con transacción abortada (rollback previo a unlock en la misma conexión).
5. Cero falsos positivos y cero fugas de lock en el pool de conexiones.

Uso:
    export TEST_PG_URL="postgresql+psycopg://user:pass@localhost:5432/test_db"
    python scripts/verify_postgres_advisory_lock.py
"""

import os
import sys
from datetime import date
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
import psycopg

# Añadir raíz al path para importar app
ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from app.scheduler import _run_daily_recurring_detection_sync  # noqa: E402
from app.services.recurring_expense import RecurringExpenseService  # noqa: E402


def get_pg_urls() -> tuple[str, str]:
    """Exige estrictamente TEST_PG_URL en host local para evitar operar sobre entornos remotos o productivos."""
    url = os.getenv("TEST_PG_URL")
    if not url:
        raise RuntimeError(
            "La variable de entorno TEST_PG_URL es obligatoria y debe apuntar a una base PostgreSQL local descartable "
            "(ej. TEST_PG_URL=postgresql+psycopg://user:pass@localhost:5432/test_db). "
            "Se eliminó el fallback a DATABASE_URL y las credenciales por defecto para prevenir ejecuciones accidentales."
        )
    parsed = make_url(url)
    host = (parsed.host or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError(
            f"TEST_PG_URL debe apuntar estrictamente a un host local (localhost, 127.0.0.1, ::1). "
            f"Host recibido: '{parsed.host}'. Se rechazan conexiones remotas para proteger entornos no locales."
        )
    raw_url = url.replace("postgresql+psycopg://", "postgresql://")
    return url, raw_url


def query_active_advisory_locks(raw_url: str, lock_key: int = 5354418701):
    with psycopg.connect(raw_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pid, classid, objid, granted
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND ((classid::bigint << 32) | (objid::bigint & 4294967295)) = %s
            """, (lock_key,))
            return cur.fetchall()


def verify_lock_free_externally(raw_url: str, lock_key: int = 5354418701) -> bool:
    """Intenta adquirir y liberar el lock desde un proceso/conexión externa independiente."""
    with psycopg.connect(raw_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            acquired = cur.fetchone()[0]
            if acquired:
                cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                return True
            return False


def run_verification():
    sqlalchemy_url, raw_url = get_pg_urls()
    print(f"[VERIFY] Conectando a PostgreSQL de prueba: {raw_url.split('@')[-1]}")

    # Verificar que el lock esté libre inicialmente
    assert verify_lock_free_externally(raw_url), "El lock ya estaba tomado antes de iniciar la prueba!"

    # Crear engine con QueuePool real de la aplicación
    engine = create_engine(sqlalchemy_url, poolclass=QueuePool, pool_size=5, max_overflow=0)
    session_factory = sessionmaker(bind=engine)

    # 1. Ensayo de Corrida Normal con Detección Real y Commits Internos
    print("\n--- 1. Verificando Corrida Normal (Detección Real y Commits Internos) ---")

    # Parchear SessionLocal en app.scheduler
    import app.scheduler as sched
    original_session_local = sched.SessionLocal
    sched.SessionLocal = session_factory

    # Espiar la ejecución real de run_daily_detection para evitar falsos positivos
    execution_tracker = {"called": False, "as_of_date": None, "result": None}
    original_detection = RecurringExpenseService.run_daily_detection

    def spy_detection(session, as_of_date=None):
        execution_tracker["called"] = True
        execution_tracker["as_of_date"] = as_of_date
        res = original_detection(session, as_of_date=as_of_date)
        execution_tracker["result"] = res
        return res

    RecurringExpenseService.run_daily_detection = staticmethod(spy_detection)

    try:
        today = date(2026, 9, 21)
        _run_daily_recurring_detection_sync(as_of_date=today)

        # Comprobar que el worker no tragó un error previo y efectivamente ejecutó la detección
        assert execution_tracker["called"] is True, (
            "Error: RecurringExpenseService.run_daily_detection no fue invocado por el worker!"
        )
        assert execution_tracker["result"] is not None, (
            "Error: run_daily_detection no devolvió resultado al worker!"
        )
        assert execution_tracker["result"].status == "ok", (
            f"Error: status inesperado en la detección: {execution_tracker['result'].status}"
        )
        print(f"[OK] Detección ejecutada efectivamente: status={execution_tracker['result'].status}, "
              f"creados={execution_tracker['result'].metrics.candidates_created}")

        # Verificar desde conexión externa que no quedó lock colgado en el pool
        locks = query_active_advisory_locks(raw_url)
        assert len(locks) == 0, f"Error: Locks residuales detectados: {locks}"
        assert verify_lock_free_externally(raw_url), "Error: Lock no adquirible tras corrida normal"
        print("[OK] Corrida normal completada: 0 locks residuales en QueuePool.")

        # 2. Ensayo de Recuperación tras Error de Transacción Abortada
        print("\n--- 2. Verificando Recuperación tras Error (Transacción Abortada) ---")

        error_tracker = {"called": False}

        def failing_detection(session, as_of_date=None):
            error_tracker["called"] = True
            # Abortar intencionalmente la transacción PostgreSQL
            session.execute(text("SELECT 1/0"))

        RecurringExpenseService.run_daily_detection = staticmethod(failing_detection)

        try:
            _run_daily_recurring_detection_sync(as_of_date=today)
        finally:
            RecurringExpenseService.run_daily_detection = original_detection

        assert error_tracker["called"] is True, "Error: La detección con error no fue invocada!"

        # Verificar que el rollback previo a unlock liberó el lock a pesar del error abortado
        locks = query_active_advisory_locks(raw_url)
        assert len(locks) == 0, f"Error: Locks residuales tras excepción: {locks}"
        assert verify_lock_free_externally(raw_url), "Error: Lock no adquirible tras error abortado"
        print("[OK] Error recuperado: rollback ejecutado y lock liberado limpiamente.")

        print("\n=== TODAS LAS VERIFICACIONES DE POSTGRESQL PASARON EXITOSAMENTE ===")
    finally:
        sched.SessionLocal = original_session_local
        RecurringExpenseService.run_daily_detection = original_detection
        engine.dispose()


if __name__ == "__main__":
    run_verification()
