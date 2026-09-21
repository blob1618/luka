# Reporte de Ensayo de Rendimiento y EXPLAIN ANALYZE en PostgreSQL (STK-189)

## 1. Entorno, Configuración y Reproducibilidad

- **Motor de Base de Datos**: PostgreSQL 16 (entorno local aislado en contenedor Docker).
- **Esquema Inicial**: Creado aplicando en orden cronológico el historial completo de las **9 migraciones canónicas** de `supabase/migrations/`:
  1. `20260911010815_baseline_remote_schema.sql`
  2. `20260911011601_protect_movimientos_financieros.sql`
  3. `20260916174000_add_conversation_flows.sql`
  4. `20260917143651_annul_financial_movements.sql`
  5. `20260918120000_add_usuario_proactive_reminders.sql`
  6. `20260919120000_drop_legacy_metas_movimientos.sql`
  7. `20260920190000_add_recurring_expense_candidates.sql`
  8. `20260920210000_allow_zero_limit_amount.sql`
  9. `20260921045538_recurring_confirmation_delivery.sql`
- **Driver y ORM**: Python 3.11, SQLAlchemy 2.0, psycopg 3 (`QueuePool`).
- **Aislamiento y Seguridad**: Sin acceso a Supabase remoto, Meta WhatsApp API ni producción. Se exige estrictamente la variable de entorno `TEST_PG_URL` apuntando a una base PostgreSQL local descartable. Se eliminó cualquier fallback a `DATABASE_URL` y credenciales por defecto.
- **Preparación Efectiva del Esquema**: El esquema se inicializa aplicando en orden secuencial las 9 migraciones canónicas de `supabase/migrations/` sobre una base vacía. El script verifica la existencia de las tablas y comprueba que contengan 0 registros antes de poblar los datos sintéticos; no utiliza `Base.metadata.create_all` ni `TRUNCATE ... CASCADE`, abortando de inmediato ante datos preexistentes.
- **Sin PII ni Rutas Personales**: Dataset 100% sintético con UUIDs generados y teléfonos ficticios.
- **Scripts Versionados de Reproducción**:
  - `python scripts/benchmark_postgres_recurring.py`: Valida esquema vacío, puebla datos sintéticos, corre `ANALYZE` y ejecuta las consultas reales con `EXPLAIN (ANALYZE, BUFFERS)`.
  - `python scripts/verify_postgres_advisory_lock.py`: Valida el ciclo de vida del advisory lock contra el worker real en `QueuePool`, verificando ejecución efectiva de la detección y ausencia de falsos positivos.

---

## 2. Volumen y Distribución de Datos Sintéticos

- **Usuarios**: 50 usuarios con `proactivo_habilitado = True`.
- **Total Movimientos**: 10.000 filas en `public.movimientos_financieros`:
  - **7.500 egresos activos** (`tipo = 'egreso'`, `anulado_en IS NULL`, `cantidad > 0`, `descripcion IS NOT NULL`).
  - **1.500 ingresos** (`tipo = 'ingreso'`).
  - **1.000 egresos anulados** (`tipo = 'egreso'`, `anulado_en = NOW()`).
- **Ventana Calendario de Detección**:
  - Fecha de referencia (`as_of_date`): `2026-09-21`.
  - `lookback_months = 4`: calculada mediante `_calculate_start_date` dando como fecha de inicio **`2026-06-01`** (primer día del mes calendario resultante, junio a septiembre de 2026).
- **Patrones Recurrentes**:
  - 30 usuarios configurados con patrones recurrentes consistentes en meses consecutivos (servicios mensuales).
  - 20 usuarios con gastos dispersos no sistemáticos.
- **Actualización de Estadísticas**: Ejecución previa de `ANALYZE public.movimientos_financieros; ANALYZE public.usuario; ANALYZE public.candidato_gasto_recurrente;`.

---

## 3. Resultados de Consultas Reales de SQLAlchemy y EXPLAIN ANALYZE

### 3.1. Consulta 1: Streaming de Detección Batch (7.500 egresos en ventana)
Consulta exacta compilada por SQLAlchemy a partir de `select(MovimientoFinanciero)` en `RecurringExpenseService.detect_candidates`:

```sql
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
```

#### Plan de Ejecución con Índice Activo (`movimientos_financieros_egresos_activos_fecha_idx`):
```text
Sort  (cost=840.47..859.60 rows=7650 width=151) (actual time=11.286..11.627 rows=7500 loops=1)
  Sort Key: usuario_id, fecha_movimiento, id
  Sort Method: quicksort  Memory: 1185kB
  Buffers: shared hit=172
  ->  Seq Scan on movimientos_financieros  (cost=0.00..347.00 rows=7650 width=151) (actual time=0.008..2.156 rows=7500 loops=1)
        Filter: ((anulado_en IS NULL) AND (descripcion IS NOT NULL) AND (fecha_movimiento >= '2026-06-01'::date) AND (fecha_movimiento <= '2026-09-21'::date) AND (tipo = 'egreso'::text))
        Rows Removed by Filter: 2500
        Buffers: shared hit=172
Planning:
  Buffers: shared hit=133 read=3
Planning Time: 1.418 ms
Execution Time: 12.094 ms
```

#### Análisis de Ingeniería sobre Consulta 1:
- **Selectividad**: Las filas que cumplen la condición representan el **75% de la tabla** (7.500 de 10.000).
- **Decisión del Optimizador**: Con una selectividad tan baja (75% de filas coincidentes), el planificador de costos de PostgreSQL descarta el recorrido de índices porque saltar aleatoriamente por punteros de índice costaría significativamente más que leer secuencialmente los 172 bloques compartidos (2.1 ms).
- **Resolución de Memoria**: El ordenamiento `ORDER BY usuario_id, fecha_movimiento, id` se resolvió en memoria mediante `quicksort` ocupando 1.185 kB, sin volcar a disco.
- **Tiempo Total en Python**: `RecurringExpenseService.detect_candidates(dry_run=True)` procesó las 7.500 filas en **295.78 ms**, creando 30 candidatos en memoria.

---

### 3.2. Consulta de Detección para Usuario Individual (Alta Selectividad: 159 filas / 10.000)
Cuando la detección se parametriza por un usuario individual (`stream_query.where(usuario_id == user_id)`), la selectividad es muy alta (1.6%):

```text
Sort  (cost=221.81..222.23 rows=166 width=157) (actual time=0.274..0.281 rows=159 loops=1)
  Sort Key: movimientos_financieros.fecha_movimiento, movimientos_financieros.id
  Sort Method: quicksort  Memory: 48kB
  Buffers: shared hit=110
  ->  Bitmap Heap Scan on public.movimientos_financieros  (cost=11.09..215.69 rows=166 width=157) (actual time=0.069..0.214 rows=159 loops=1)
        Recheck Cond: ((movimientos_financieros.usuario_id = '...'::uuid) AND (movimientos_financieros.tipo = 'egreso'::text) AND (movimientos_financieros.fecha_movimiento >= '2026-06-01'::date) AND (movimientos_financieros.fecha_movimiento <= '2026-09-21'::date))
        Buffers: shared hit=110
        ->  Bitmap Index Scan on movimientos_financieros_usuario_tipo_fecha_idx  (cost=0.00..11.04 rows=184 width=0) (actual time=0.046..0.047 rows=184 loops=1)
              Buffers: shared hit=3
Planning Time: 0.175 ms
Execution Time: 0.342 ms
```
- **Conclusión**: El motor utiliza `Bitmap Index Scan` de forma inmediata cuando la selectividad lo justifica, resolviendo la consulta en **0.34 ms**.

---

### 3.3. Consulta 2: Webhook Candidate Lookup (`_check_pending_candidate_proposal`)
Consulta sincrónica ejecutada al registrar un egreso para evaluar si adjuntar la propuesta interactiva:

```text
Limit  (cost=0.43..10.08 rows=1 width=64) (actual time=0.038..0.039 rows=1 loops=1)
  Buffers: shared hit=6
  ->  Nested Loop  (cost=0.43..10.08 rows=1 width=64) (actual time=0.037..0.038 rows=1 loops=1)
        Buffers: shared hit=6
        ->  Index Scan using candidato_gasto_recurrente_usuario_patron_key on public.candidato_gasto_recurrente  (cost=0.15..8.17 rows=1 width=64) (actual time=0.024..0.025 rows=1 loops=1)
              Index Cond: ((usuario_id = '...'::uuid) AND (patron_hash = 'patron_hash_internet_fibertel'::text))
              Filter: (estado = 'pendiente'::text)
              Buffers: shared hit=3
        ->  Index Scan using usuario_pkey on public.usuario  (cost=0.28..1.90 rows=1 width=16) (actual time=0.011..0.011 rows=1 loops=1)
              Index Cond: (id = '...'::uuid)
              Filter: (proactivo_habilitado IS TRUE)
              Buffers: shared hit=3
Planning Time: 0.285 ms
Execution Time: 0.058 ms
```
- **Contexto y Medición**: Con un candidato pendiente real registrado, la consulta realiza la búsqueda por índice único (`candidato_gasto_recurrente_usuario_patron_key`) y la verificación por clave primaria del usuario (`usuario_pkey`), resolviendo la coincidencia efectiva en **0.058 ms** (58 microsegundos) con solo 6 buffers en memoria. Demuestra que la verificación en el camino crítico del webhook opera a escala de microsegundos sin impacto perceptible de I/O en base de datos.

---

### 3.4. Consulta 3: Evaluación de Gasto Registrado en Período (`check_period_expense_registered`)
Consulta ejecutada por el scheduler para evaluar si suprime el aviso porque el usuario ya pagó:

```text
Bitmap Heap Scan on public.movimientos_financieros  (cost=4.80..94.69 rows=31 width=54) (actual time=0.046..0.104 rows=38 loops=1)
  Recheck Cond: ((movimientos_financieros.usuario_id = '...'::uuid) AND (movimientos_financieros.tipo = 'egreso'::text) AND (movimientos_financieros.fecha_movimiento >= '2026-09-01'::date) AND (movimientos_financieros.fecha_movimiento <= '2026-09-30'::date))
  Buffers: shared hit=39
  ->  Bitmap Index Scan on movimientos_financieros_usuario_tipo_fecha_idx  (cost=0.00..4.79 rows=34 width=0) (actual time=0.031..0.031 rows=40 loops=1)
        Buffers: shared hit=2
Planning Time: 0.159 ms
Execution Time: 0.198 ms
```
- **Conclusión**: Resuelto en **0.198 ms** mediante `Bitmap Index Scan`.

---

## 4. Ensayo de Advisory Lock en `QueuePool` y Corrección Mínima

### 4.1. Reproducción de la Fuga con Worker Real y Concurrencia de Pool
1. **Comportamiento del Código Original**:
   - `_run_daily_recurring_detection_sync` abría una sesión ordinaria `session = SessionLocal()`.
   - Adquiría el lock de sesión: `SELECT pg_try_advisory_lock(5354418701)` en la conexión física asignada por el pool (ej. PID 84).
   - A continuación, `detect_candidates()` ejecutaba su lógica y realizaba `session.commit()` internamente (en la línea 547 de `app/services/recurring_expense.py`).
   - Al hacer commit, SQLAlchemy desasociaba la conexión física y la devolvía a la lista de inactivas de `QueuePool`.
   - En una aplicación con concurrencia (webhooks, otros jobs), esa conexión física (PID 84) era tomada por otra petición.
   - Cuando el bloque `finally` del scheduler ejecutaba `session.execute("SELECT pg_advisory_unlock(5354418701)")`, SQLAlchemy pedía una conexión al pool y recibía una **conexión diferente** (ej. PID 88).
   - En PostgreSQL, los locks de nivel de sesión pertenecen exclusivamente al backend que los adquirió. En consecuencia, `pg_advisory_unlock` en PID 88 devolvía `False`, y el lock quedaba **permanentemente retenido** en PID 84 dentro del pool, bloqueando todas las corridas futuras del scheduler.
2. **Comportamiento en Caso de Excepción**:
   - Si ocurría una excepción de base de datos dentro de `detect_candidates()`, la transacción quedaba en estado abortado (`InFailedSqlTransaction`).
   - Ejecutar `pg_advisory_unlock` directamente arrojaba excepción sin liberar el lock.

### 4.2. Diseño e Implementación de la Corrección Mínima en `app/scheduler.py`
Para garantizar que el ciclo de vida del lock ocurra estrictamente en la misma conexión física:
```python
with bind.connect() as lock_conn:
    lock_acquired = lock_conn.execute(
        text("SELECT pg_try_advisory_lock(5354418701)")
    ).scalar()
    if not lock_acquired:
        logger.info("[DAILY_DETECTION] Advisory lock 5354418701 ocupado; otro worker está en ejecución.")
        return

    session = SessionLocal(bind=lock_conn)
    try:
        result = RecurringExpenseService.run_daily_detection(session, as_of_date=as_of_date)
        session.commit()
        logger.info(
            "[DAILY_DETECTION] Completado: creados=%d actualizados=%d",
            result.metrics.candidates_created,
            result.metrics.candidates_updated,
        )
    except Exception as exc:
        logger.exception("[DAILY_DETECTION_ERROR] %s: %s", type(exc).__name__, exc)
    finally:
        session.close()
        try:
            lock_conn.rollback()  # Limpia cualquier estado de transacción abortada
            unlocked = lock_conn.execute(text("SELECT pg_advisory_unlock(5354418701)")).scalar()
            if not unlocked:
                logger.warning("[DAILY_DETECTION] Error al liberar advisory lock: pg_advisory_unlock retorno False")
        except Exception as unlock_err:
            logger.warning("[DAILY_DETECTION] Error al liberar advisory lock: %s", unlock_err)
            lock_conn.invalidate()  # Destruye la conexión para evitar reciclar un lock retenido
```

### 4.3. Evidencia Empírica de Verificación
Ejecutando `scripts/verify_postgres_advisory_lock.py` contra PostgreSQL real:
- **Corrida Normal con Commits Internos**:
  - Lock adquirido en `lock_conn`.
  - Commits ejecutados por `detect_candidates()`. La sesión respeta el `bind=lock_conn` y no desasocia la conexión física.
  - Al finalizar, `lock_conn.rollback()` y `pg_advisory_unlock` se ejecutan en el mismo backend PID.
  - Una conexión física independiente confirma inmediatamente que `pg_locks` está vacío y adquiere el lock con éxito (`True`).
- **Corrida con Error (Transacción Abortada `1/0`)**:
  - Se fuerza `SELECT 1/0`.
  - La transacción abortada es restablecida mediante `lock_conn.rollback()`.
  - `pg_advisory_unlock` devuelve `True`.
  - Conexión física independiente confirma `pg_locks` vacío y adquiere el lock con éxito (`True`).
- **Regresión Unitaria en Test Suite**:
  - `tests/test_recurring_integration.py` implementa `test_unpinned_session_fails_and_leaks_lock_under_pool_concurrency`, que demuestra la falla del código original y verifica el paso limpio del código corregido.

### 4.4. Modos de Falla y Consideraciones de Pooler
1. **Muerte Abrupta del Proceso / Desconexión**:
   Si el proceso de la aplicación o el worker muere de forma imprevista (p. ej., OOM killer o desconexión de red), el backend de PostgreSQL detecta la pérdida del socket TCP y el kernel de PostgreSQL libera automáticamente todos los session advisory locks retenidos por ese PID. No queda bloqueo huérfano.
2. **Incompatibilidad con Transaction Pooling (PgBouncer / Supabase Transaction Pooler)**:
   Los advisory locks a nivel de sesión (`pg_try_advisory_lock`) se asocian al backend físico del servidor de base de datos. Si la aplicación se conecta a través de un pooler en modo transacción (puerto 6543 en Supabase), transacciones subsiguientes o declaraciones fuera de transacción pueden ser asignadas a diferentes conexiones del servidor, rompiendo la semántica del lock.
   **Requisito Operacional**: El worker del scheduler debe conectarse directamente a PostgreSQL o mediante Session Pooling (puerto 5432) para garantizar el funcionamiento correcto de los advisory locks.

---

## 5. Matriz de Cobertura de Requisitos y Enlace a Pruebas Previas

| Requisito | Ticket | Pruebas y Aserciones Concretas | Resultado |
| --- | --- | --- | --- |
| Ventana móvil de 4 meses calendario | STK-186 | `tests/test_recurring_expense.py::TestRecurringExpenseLookback::test_lookback_window_uses_calendar_months`<br>Aserción: `_calculate_start_date(date(2026, 9, 21), 4) == date(2026, 6, 1)` | Cubierto |
| Detección de 3 meses consecutivos | STK-186 | `tests/test_recurring_expense.py::TestRecurringExpenseDetection::test_three_consecutive_months_creates_candidate`<br>Aserción: `assert candidate.estado == "pendiente"` y `assert candidate.dia_estimado == 10` | Cubierto |
| Adjunción de propuesta interactiva con 2 botones | STK-187 | `tests/test_recurring_confirmation.py::TestRecurringProposalFlow::test_proposal_attached_to_expense_registration`<br>Aserción: `assert len(reply.interactive_buttons) == 2` | Cubierto |
| Aceptación y creación de recordatorio inteligente | STK-187 | `tests/test_recurring_confirmation.py::TestRecurringConfirmationAction::test_button_payload_interactive_reply_accepts_and_creates_reminder`<br>Aserción: `assert reminder.origen == "recurrente_inteligente"` y `assert reminder.dias_anticipacion == 3` | Cubierto |
| Despacho proactivo con anticipación | STK-188 | `tests/test_recurring_confirmation.py::TestSmartReminderDispatch::test_check_reminders_sends_smart_recurring_reminder`<br>Aserción: `assert aviso.estado == "enviado"` y verificación de template de Meta | Cubierto |
| Supresión inteligente ante pago temprano | STK-188 | `tests/test_recurring_confirmation.py::TestSmartReminderSuppression::test_check_reminders_suppresses_if_already_paid_this_month`<br>Aserción: `assert aviso.estado == "suprimido"` y `assert aviso.motivo_supresion == "gasto_registrado"` | Cubierto |
| Ciclo de vida integrado end-to-end con reloj fijo | STK-189 | `tests/test_recurring_integration.py::TestRecurringIntegratedLifecycle::test_full_lifecycle_detection_to_delivery`<br>Línea temporal fija `2026-09-08` / `2026-07-20`: detección -> propuesta -> aceptación -> recordatorio -> aviso | Cubierto |
| Rechazo persistente y no duplicación | STK-189 | `tests/test_recurring_integration.py::TestRecurringRejectionAndSuppression::test_rejection_prevents_future_proposals_and_reminders` | Cubierto |
| Anti-tampering y seguridad de texto libre | STK-189 | `tests/test_recurring_integration.py::TestSecurityAndConversationalSafety` | Cubierto |
| Resiliencia de advisory lock en pool | STK-189 | `tests/test_recurring_integration.py::TestAdvisoryLockRollbackRecovery` y `scripts/verify_postgres_advisory_lock.py` | Cubierto |
