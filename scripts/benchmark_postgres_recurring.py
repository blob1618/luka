"""Benchmark reproducible de PostgreSQL y EXPLAIN ANALYZE para STK-189.

Genera dataset sintético representativo (50 usuarios, 10.000 movimientos financieros)
sobre una base PostgreSQL descartable vacía (con las 9 migraciones canónicas aplicadas)
y ejecuta las consultas reales compiladas por SQLAlchemy con EXPLAIN (ANALYZE, BUFFERS).

Uso:
    export TEST_PG_URL="postgresql+psycopg://user:pass@localhost:5432/test_db"
    python scripts/benchmark_postgres_recurring.py
"""

import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from app.models.database import CandidatoGastoRecurrente, MovimientoFinanciero, Usuario  # noqa: E402
from app.services.recurring_expense import RecurringExpenseService  # noqa: E402


def get_db_url() -> str:
    """Exige estrictamente TEST_PG_URL en host local para evitar operar sobre bases remotas o productivas."""
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
    return url


def run_benchmark():
    db_url = get_db_url()
    print(f"[BENCHMARK] Conectando a {db_url.split('@')[-1]}")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # 1. Comprobación estricta de preexistencia de datos:
        # El benchmark no ejecuta TRUNCATE ni destruye datos. Si la base contiene filas, aborta.
        counts = {}
        for table_name in ("movimientos_financieros", "candidato_gasto_recurrente", "usuario"):
            try:
                cnt = session.execute(text(f"SELECT count(*) FROM public.{table_name}")).scalar()
                counts[table_name] = cnt
            except Exception as exc:
                raise RuntimeError(
                    f"Error al verificar la tabla public.{table_name}: {exc}. "
                    f"Asegúrese de haber aplicado las 9 migraciones canónicas de supabase/migrations/ antes de ejecutar el benchmark."
                ) from exc

        if any(c > 0 for c in counts.values()):
            raise RuntimeError(
                f"El benchmark requiere una base de datos vacía y descartable. "
                f"Se detectaron registros preexistentes: {counts}. Abortando para evitar sobreescrituras."
            )

        # 2. Generar 50 usuarios sintéticos
        print("[BENCHMARK] Insertando 50 usuarios sintéticos...")
        users = []
        for i in range(50):
            u = Usuario(
                id=uuid.uuid4(),
                whatsapp_id=f"549110000{i:04d}",
                nombre=f"Usuario Sintetico {i}",
                email=f"synthetic_user_{i}@example.com",
                proactivo_habilitado=True,
                creado_en=datetime.now(timezone.utc),
            )
            session.add(u)
            users.append(u)
        session.commit()

        # 3. Generar 10.000 movimientos
        # - 7.500 egresos activos en ventana 2026-06-01 a 2026-09-21
        # - 1.500 ingresos
        # - 1.000 egresos anulados
        print("[BENCHMARK] Insertando 10.000 movimientos sintéticos...")
        start_dt = date(2026, 6, 1)
        end_dt = date(2026, 9, 21)
        days_span = (end_dt - start_dt).days

        movements = []
        user_ids = [u.id for u in users]

        # 7.500 egresos activos
        for i in range(7500):
            uid = user_ids[i % 50]
            day_offset = (i * 7) % days_span
            m_date = start_dt + timedelta(days=day_offset)
            m = MovimientoFinanciero(
                id=uuid.uuid4(),
                usuario_id=uid,
                tipo="egreso",
                cantidad=Decimal(str(1000 + (i % 50) * 100)),
                moneda="ARS",
                descripcion=f"Servicio Recurrente {i % 5}" if (i % 50 < 30) else f"Gasto Vario {i}",
                fecha_movimiento=m_date,
                origen="whatsapp",
                creado_en=datetime.now(timezone.utc),
            )
            movements.append(m)

        # 1.500 ingresos
        for i in range(1500):
            uid = user_ids[i % 50]
            day_offset = (i * 11) % days_span
            m_date = start_dt + timedelta(days=day_offset)
            m = MovimientoFinanciero(
                id=uuid.uuid4(),
                usuario_id=uid,
                tipo="ingreso",
                cantidad=Decimal("50000.00"),
                moneda="ARS",
                descripcion="Cobro Sueldo",
                fecha_movimiento=m_date,
                origen="whatsapp",
                creado_en=datetime.now(timezone.utc),
            )
            movements.append(m)

        # 1.000 egresos anulados
        for i in range(1000):
            uid = user_ids[i % 50]
            day_offset = (i * 13) % days_span
            m_date = start_dt + timedelta(days=day_offset)
            m = MovimientoFinanciero(
                id=uuid.uuid4(),
                usuario_id=uid,
                tipo="egreso",
                cantidad=Decimal("2500.00"),
                moneda="ARS",
                descripcion="Gasto Anulado Error",
                fecha_movimiento=m_date,
                origen="whatsapp",
                creado_en=datetime.now(timezone.utc),
                anulado_en=datetime.now(timezone.utc),
            )
            movements.append(m)

        session.bulk_save_objects(movements)
        session.commit()

        # 4. Insertar un candidato pendiente real para medir el lookup sincrónico de webhook con match efectivo
        target_uid = user_ids[0]
        target_patron_hash = "patron_hash_internet_fibertel"
        real_candidate = CandidatoGastoRecurrente(
            id=uuid.uuid4(),
            usuario_id=target_uid,
            patron_hash=target_patron_hash,
            concepto="Internet Fibertel",
            descripcion_normalizada="internet fibertel",
            categoria_id=None,
            moneda="ARS",
            monto_promedio=Decimal("15000.00"),
            dia_estimado=10,
            intervalo_dias_promedio=Decimal("30.0"),
            confianza=Decimal("0.95"),
            estado="pendiente",
            proxima_fecha_estimada=date(2026, 10, 10),
            creado_en=datetime.now(timezone.utc),
            actualizado_en=datetime.now(timezone.utc),
        )
        session.add(real_candidate)
        session.commit()

        print("[BENCHMARK] Inserción completada. Ejecutando ANALYZE...")
        session.execute(text("ANALYZE public.movimientos_financieros; ANALYZE public.usuario; ANALYZE public.candidato_gasto_recurrente;"))
        session.commit()

        # Consulta 1: Streaming Detección Batch
        print("\n=== CONSULTA 1: STREAMING DETECCIÓN BATCH ===")
        sql_batch = """
        EXPLAIN (ANALYZE, BUFFERS)
        SELECT movimientos_financieros.id, movimientos_financieros.usuario_id, movimientos_financieros.categoria_id,
               movimientos_financieros.tipo, movimientos_financieros.cantidad, movimientos_financieros.moneda,
               movimientos_financieros.descripcion, movimientos_financieros.fecha_movimiento, movimientos_financieros.origen,
               movimientos_financieros.whatsapp_message_id, movimientos_financieros.creado_en,
               movimientos_financieros.actualizado_en, movimientos_financieros.anulado_en
        FROM public.movimientos_financieros
        WHERE movimientos_financieros.tipo = 'egreso'
          AND movimientos_financieros.anulado_en IS NULL
          AND movimientos_financieros.fecha_movimiento >= '2026-06-01'
          AND movimientos_financieros.fecha_movimiento <= '2026-09-21'
          AND movimientos_financieros.descripcion IS NOT NULL
        ORDER BY movimientos_financieros.usuario_id, movimientos_financieros.fecha_movimiento ASC, movimientos_financieros.id ASC;
        """
        res = session.execute(text(sql_batch)).fetchall()
        for row in res:
            print(row[0])

        # Medir tiempo total de Python RecurringExpenseService.detect_candidates
        print("\n=== TIEMPO TOTAL DETECT_CANDIDATES EN PYTHON ===")
        t0 = time.perf_counter()
        candidates = RecurringExpenseService.detect_candidates(
            session=session,
            as_of_date=date(2026, 9, 21),
            lookback_months=4,
            dry_run=True,
        )
        t_py = (time.perf_counter() - t0) * 1000
        print(f"Candidatos detectados: {len(candidates.candidates)} (creados={candidates.metrics.candidates_created}, actualizados={candidates.metrics.candidates_updated}) en {t_py:.2f} ms")

        # Consulta 2: Webhook Candidate Lookup con Candidato Pendiente Real (Devuelve 1 fila)
        print("\n=== CONSULTA 2: WEBHOOK CANDIDATE LOOKUP (MATCH REAL: 1 FILA) ===")
        sql_webhook = f"""
        EXPLAIN (ANALYZE, BUFFERS)
        SELECT candidato_gasto_recurrente.id, candidato_gasto_recurrente.usuario_id, candidato_gasto_recurrente.patron_hash,
               candidato_gasto_recurrente.concepto, candidato_gasto_recurrente.dia_estimado
        FROM public.candidato_gasto_recurrente
        JOIN public.usuario ON public.usuario.id = public.candidato_gasto_recurrente.usuario_id
        WHERE candidato_gasto_recurrente.usuario_id = '{target_uid}'::uuid
          AND candidato_gasto_recurrente.patron_hash = '{target_patron_hash}'
          AND candidato_gasto_recurrente.estado = 'pendiente'
          AND public.usuario.proactivo_habilitado IS TRUE
        LIMIT 1;
        """
        res_webhook = session.execute(text(sql_webhook)).fetchall()
        for row in res_webhook:
            print(row[0])

        # Consulta 3: Evaluación de Gasto Registrado en Período (Scheduler check_period_expense_registered)
        print("\n=== CONSULTA 3: CHECK PERIOD EXPENSE REGISTERED ===")
        sql_check_period = f"""
        EXPLAIN (ANALYZE, BUFFERS)
        SELECT movimientos_financieros.id, movimientos_financieros.cantidad, movimientos_financieros.fecha_movimiento,
               movimientos_financieros.descripcion, movimientos_financieros.categoria_id
        FROM public.movimientos_financieros
        WHERE movimientos_financieros.usuario_id = '{target_uid}'::uuid
          AND movimientos_financieros.tipo = 'egreso'
          AND movimientos_financieros.anulado_en IS NULL
          AND movimientos_financieros.fecha_movimiento >= '2026-09-01'
          AND movimientos_financieros.fecha_movimiento <= '2026-09-30'
        ORDER BY movimientos_financieros.fecha_movimiento ASC;
        """
        res_period = session.execute(text(sql_check_period)).fetchall()
        for row in res_period:
            print(row[0])

        print("\n=== BENCHMARK FINALIZADO CON ÉXITO ===")
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    run_benchmark()
