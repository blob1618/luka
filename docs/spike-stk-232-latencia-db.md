# Spike Técnico STK-232: Descomposición de Latencia de Base de Datos y Diagnóstico de Red vs Ejecución SQL

- **Ticket**: STK-232
- **Tipo**: Spike / Investigación Técnica
- **Repositorio**: `blob1618/luka` (Backend)
- **Topología Desplegada Confirmada**:
  - Proceso Backend en Render alojado en **Frankfurt**.
  - Servidor PostgreSQL en Supabase alojado en **São Paulo** (`sa-east-1`).
- **Estado de Mediciones**: Duración neta SQL, round-trips de red y RTT transatlántico WAN permanecen como **no medidos empíricamente** en entornos remotos.

---

## 1. Resumen Ejecutivo

El presente Spike investiga la composición de la latencia observada en la fase de persistencia de datos (`db_ms`) durante el procesamiento de mensajes de WhatsApp en Luka. El objetivo primordial es dilucidar si dicha latencia se origina predominantemente en el trabajo de procesamiento SQL en el servidor de base de datos, en la penalidad acumulada de los viajes de ida y vuelta de red (RTT WAN) entre continentes (Frankfurt ↔ São Paulo), o en componentes de la arquitectura de la aplicación (overhead de SQLAlchemy, pre-ping del connection pool, deserialización e hidratación ORM en memoria).

Como resultado de este Spike:
1. Se implementó y validó formalmente un prototipo de telemetría de eventos SQLAlchemy (`before_cursor_execute`, `after_cursor_execute`, `handle_error`) que captura `sql_count` y `sql_exec_ms` de forma anónima, concurrente y disociada de identificadores personales o de mensaje.
2. Se definió la metodología experimental comparativa de regiones, caracterizando cargas reales existentes (`query_movements` y `register_movement_with_category`).
3. Se documentó el diagnóstico de entornos de prueba, dictaminando el **bloqueo y diferimiento formal de las mediciones regionales** por falta de infraestructura de staging y runners regionales confirmados, en cumplimiento de la prohibición estricta de impactar producción o aprovisionar recursos pagos.
4. Se evaluó técnicamente el impacto y la viabilidad de **STK-229** (Transaction pooling Supavisor en puerto 6543), identificando una incompatibilidad arquitectónica con los advisory locks del scheduler en `app/scheduler.py` y recomendando explícitamente su **diferimiento**.

---

## 2. Descomposición Conceptual de Latencia: `db_ms` frente a `sql_exec_ms`

### 2.1 Hipótesis Técnicas a Contrastar Empíricamente

En el análisis de la latencia del backend desplegado convergen tres hipótesis técnicas fundamentales, catalogadas estrictamente como **hipótesis no medidas empíricamente**:
- **Hipótesis RTT Transatlántico (hipótesis externa / no validada empíricamente)**: Estimación teórica referencial en el rango de $\sim 180\text{--}220\text{ ms}$ por round-trip a nivel de socket TCP/TLS; se enfatiza explícitamente que el RTT real no fue medido empíricamente en este entorno.
- **Hipótesis $T_{sql}$ en PostgreSQL**: Las consultas individuales optimizadas sobre tablas con índices adecuados demandan tiempos de cómputo puro en el motor de base de datos en el orden de pocos milisegundos ($1\text{--}10\text{ ms}$).
- **Hipótesis de Penalidad Acumulada**: La mayor parte de la latencia percibida en las fases de persistencia se atribuye a la acumulación de viajes de red secuenciales necesarios para resolver una operación de negocio.
- **Criterio metodológico**: Queda prohibido asumir cantidades fijas de consultas por mensaje o porcentajes predeterminados de demora sin medición empírica en el entorno de ejecución.

### 2.2 Qué Mide y Qué NO Permite Inferir `db_ms`

La métrica `db_ms` existente en el sistema cronometra el tiempo de pared total (wall-clock time) transcurrido dentro de los bloques delimitados por `with track_phase("db"):` en el pipeline del webhook (`app/services/dispatcher.py` y módulos asociados).

#### Componentes incluidos dentro de `db_ms`:
1. **Checkout del Pool de Conexiones**: Adquisición de una conexión libre desde el pool de SQLAlchemy (`QueuePool`).
2. **Verificación Pre-Ping de Conexión (`pool_pre_ping=True`)**: Emisión de una verificación de salud (`SELECT 1`) sobre el socket antes de entregar la conexión, lo cual implica un round-trip WAN adicional si la conexión estuvo ociosa.
3. **Negociación TCP y Handshake TLS**: En conexiones frías (*cold start*), el establecimiento de la sesión de transporte seguro con Supabase.
4. **Compilación de Expresiones ORM**: Traducción y compilación de árboles sintácticos de expresiones (AST) de SQLAlchemy a sentencias SQL textuales parametrizadas.
5. **Despacho y Ejecución en Cursor**: El envío y retorno inmediato de cada sentencia en el driver de base de datos (`sql_exec_ms`).
6. **Recepción, Deserialización e Hidratación ORM**: La lectura de los bytes de respuesta desde el socket, conversión de tipos por el dialecto (`psycopg3`) y la instanciación de objetos de dominio SQLAlchemy en memoria Python.
7. **Lógica Intermedia de Negocio**: Cualquier cálculo, bifurcación o manipulación de datos en Python que resida dentro del bloque delimitado por el context manager `track_phase("db")`.

#### Qué NO permite inferir `db_ms`:
- **No permite inferir el tiempo puro de cómputo en el motor de base de datos**: No discrimina entre el tiempo de CPU/disco de PostgreSQL y el tiempo que los paquetes pasan en tránsito por la red pública WAN.
- **No permite inferir la cantidad de round-trips físicos de red**: No cuantifica cuántos intercambios a nivel de paquete ocurrieron entre el host de la aplicación y el host de base de datos.
- **No permite inferir la latencia de consultas individuales**: Agrupa todas las operaciones de persistencia del bloque de servicio en un único valor escalar acumulado.
- **No permite inferir la fuente de contención**: Si el pool está saturado esperando conexiones libres, ese tiempo de espera se suma a `db_ms`, sin indicar si la demora fue local o remota.

### 2.3 Qué Mide y Cuáles son los Límites de `sql_exec_ms` y `sql_count`

#### `sql_count`
- **Definición**: Cantidad de eventos `before_cursor_execute` observados por SQLAlchemy durante el procesamiento de una petición.
- **Regla de incremento**: Se incrementa **exactamente una vez** por cada evento `before_cursor_execute`.
- **Límites y salvedades**:
  - No equivale necesariamente a sentencias SQL textuales únicas ni a paquetes físicos de red garantizados.
  - En operaciones por lotes (`executemany`), SQLAlchemy dispara un único evento `before_cursor_execute` con una secuencia de tuplas de parámetros. Dependiendo del driver (`psycopg3`), la versión del servidor y el pipelining, esto puede resolverse en uno o más intercambios de red. Por lo tanto, `sql_count` registra despachos a nivel cursor.

#### `sql_exec_ms`
- **Definición Estricta**: Intervalo de tiempo observado cronometrado estrictamente alrededor de la invocación de `cursor.execute()` o `cursor.executemany()` (delimitado entre los eventos `before_cursor_execute` y `after_cursor_execute` o `handle_error` de SQLAlchemy).
- **Límites y Salvedades Críticas**:
  - **Dependencia del driver y buffering**: La proporción de recepción de datos capturada dentro de este intervalo depende del driver subyacente (`psycopg3`), de la modalidad de cursor (cursor estándar del lado cliente que bufferea resultados vs. cursor streaming del lado servidor) y del volumen del resultado.
  - **Exclusión de fetch completo e hidratación ORM**: No mide necesariamente iteraciones o llamadas subsecuentes a `fetchmany()` / `fetchall()` fuera de la llamada al cursor, y **no mide la hidratación ORM** (la transformación de tuplas crudas en modelos de entidad de dominio ocurre posteriormente en el runtime de Python).

### 2.4 Naturaleza de $\text{db\_ms} - \text{sql\_exec\_ms}$

La diferencia aritmética entre `db_ms` y `sql_exec_ms` debe entenderse y describirse **únicamente como una diferencia diagnóstica no aditiva, con alcances distintos**:
- No constituye una cota matemática rigurosa ni una descomposición aditiva cerrada.
- Revela el orden de magnitud del tiempo consumido por el ciclo de vida de la conexión, compilación de queries, hidratación de objetos ORM y procesamiento Python frente al tiempo de despacho a nivel cursor.

---

## 3. Arquitectura del Prototipo de Telemetría

La instrumentación se integró en la arquitectura existente de Luka respetando el principio de modularidad y sin alterar el cálculo ni el significado de las fases actuales.

### 3.1 Integración de Eventos SQLAlchemy

En `app/models/database.py`, se registraron tres listeners a nivel de la clase `Engine` de SQLAlchemy:

1. **`before_cursor_execute(conn, cursor, statement, parameters, context, executemany)`**:
   - Consulta `get_current_telemetry()`. Si no hay telemetría activa (`None`), retorna de inmediato (no-op sin sobrecarga).
   - Incrementa el contador: `telemetry.increment_sql_count()` (único punto de incremento).
   - Valida la presencia de `context`:
     - Si `context is not None`: almacena el timestamp de inicio mediante `context._luka_sql_start = time.perf_counter()`.
     - Si `context is None`: omite de forma segura el cronometraje de ese despacho específico, evitando accesos ilegales a atributos (`AttributeError`) y previniendo fugas de memoria o mezclas de contexto en ejecuciones crudas.
   - **Privacidad**: Prohibido acceder, formatear, concatenar o registrar `statement` o `parameters`.

2. **`after_cursor_execute(conn, cursor, statement, parameters, context, executemany)`**:
   - Consulta `get_current_telemetry()`. Si es `None`, retorna.
   - Si `context is not None and hasattr(context, "_luka_sql_start")`:
     - Calcula la duración transcurrida: `duration_ms = (time.perf_counter() - start) * 1000`.
     - Acumula el tiempo en `telemetry.accumulate_sql_time(duration_ms)`.
     - Elimina el atributo de inicio para garantizar que no haya acumulación doble.

3. **`handle_error(exception_context)`**:
   - Captura excepciones DBAPI (`IntegrityError`, caídas de socket, timeouts).
   - Consulta `get_current_telemetry()`. Si es `None`, retorna.
   - Extrae de forma segura el contexto de ejecución: `ctx = getattr(exception_context, "execution_context", None)`.
   - Si `ctx is not None and hasattr(ctx, "_luka_sql_start")`:
     - Calcula la duración transcurrida y acumula en `telemetry.accumulate_sql_time(duration_ms)`.
     - Elimina el atributo de inicio.
     - **No incrementa `sql_count`**, garantizando que el intento fallido contabilice exactamente 1 en `sql_count`.
   - **Seguridad**: No se inspecciona ni se loguea `original_exception` ni la sentencia SQL que falló.

### 3.2 Sincronización y Concurrencia

En `app/services/telemetry.py`:
- La clase `MessageTelemetry` encapsula `self._lock = threading.RLock()`.
- Los métodos `increment_sql_count()`, `accumulate_sql_time()`, `record_phase()` y la lectura de snapshots en `finish()` están sincronizados mediante este lock reentrante, protegiendo las mutaciones ante accesos concurrentes dentro del mismo contexto.
- El aislamiento entre peticiones concurrentes está garantizado por `contextvars.ContextVar("current_telemetry")`, asegurando que cada tarea asíncrona (`asyncio`) opere sobre su propia instancia sin contaminación cruzada.

### 3.3 Privacidad Estricta y Canal Anónimo Dedicado

- **Alineación con TASK.md**: La métrica agregada por mensaje debe quedar estrictamente disociada de identificadores de usuario o de mensaje.
- **Canal de Emisión Separado**:
  - Al concluir el procesamiento del mensaje en `finish()`, se emite una línea de log independiente en un canal dedicado (`luka.telemetry.sql`):
    `[SQL_METRICS_ANONYMOUS] sql_count=<int> sql_exec_ms=<float>`
  - Esta línea contiene **exclusivamente agregados escalares numéricos**. No incluye `message_id`, WAMID, número telefónico, `user_id`, texto SQL ni parámetros.
- **Invariante de Estructuras con Identificadores**:
  - El diccionario retornado por `MessageTelemetry.finish()` (que contiene `message_id`) **no incluye** `sql_count` ni `sql_exec_ms`.
  - La línea de log de `luka.metrics` (`[METRICS] message_id=...`) **no incluye** métricas de SQL.
- **Configuración Global de Logs**: Se verificó que `logging.basicConfig` en `app/main.py` utiliza el formato estándar `%(asctime)s [%(levelname)s] %(name)s: %(message)s` sin filtros o formatters contextuales que inyecten identificadores globales al canal de SQL.

---

## 4. Metodología Experimental para Comparación Regional

Para contrastar empíricamente la hipótesis de latencia regional cuando se disponga de infraestructura autorizada, se diseñó la siguiente metodología experimental basada estrictamente en los servicios y modelos vigentes en `origin/main` (`commit 1e39b471ee925d8c1da21c9f42a133dcf15efb71`).

### 4.1 Caracterización de Cargas Representativas

#### 1. Carga de Lectura (`Carga_Read`)
- **Operación del Servicio**: Invocación de `FinanceService.query_movements(user_id=test_user_id, limit=5)` (`app/services/finance.py:940`).
- **Secuencia SQL Real Verificada en el Código**:
  1. *Validación de Usuario*: `session.query(Usuario).filter(Usuario.id == parsed_user_id).first()` → **1 query `SELECT`** sobre `public.usuario`.
  2. *Resolución de Categoría*: Invocación general sin filtro de categoría → **0 queries adicionales** de categoría.
  3. *Conteo Total de Registros*: `query.count()` → **1 query `SELECT count(*) AS count_1 FROM (...)`**.
  4. *Obtención de Resultados Acotados*: `query.limit(5).all()` → **1 query `SELECT ... FROM movimientos_financieros ... LIMIT 5`**.
- **Total de Consultas**: Exactamente **3 queries `SELECT`** secuenciales a la base de datos.
- Operación estrictamente de solo lectura (sin mutaciones).

#### 2. Carga de Escritura (`Carga_Write`)
- **Operación del Servicio**: Invocación de `FinanceService.register_movement_with_category(...)` (`app/services/finance.py:773`).
- **Parámetros**: Teléfono de prueba, WAMID sintético unívoco (`synthetic_wamid = f"bench-{uuid4()}"`), monto decimal, categoría y descripción.
- **Secuencia SQL Real Verificada en el Código**:
  1. *Validación de Usuario*: `SELECT` en `Usuario` por teléfono (`_get_user_by_phone`).
  2. *Deduplicación*: `SELECT` en `MovimientoFinanciero` por `whatsapp_message_id` (`_find_duplicate`).
  3. *Resolución de Categoría*: `SELECT` en `Categoria` para validar categoría activa.
  4. *Inserción y Commit*: `INSERT` en `public.movimientos_financieros` + `session.commit()` final.
- **Total de Consultas**: Exactamente **3 queries `SELECT` + 1 query `INSERT` + `COMMIT`**.
- **Procedimiento de Limpieza Fuera del Intervalo Cronometrado**:
  - Para evitar que la tabla de movimientos crezca con cada iteración alterando los planes de ejecución de PostgreSQL:
  - Tras verificar `assert result.status == "registered"`, se detiene el cronómetro.
  - En una sesión de base de datos independiente, se ejecuta `DELETE FROM public.movimientos_financieros WHERE id = :created_id` utilizando el ID devuelto en el resultado.
  - La tabla retorna exactamente a su tamaño base inicial (50 filas semilla) antes de la siguiente muestra.

### 4.2 Tratamiento de Muestras e Incertidumbre

- **Tamaño Muestral**: $N = 100$ peticiones por tipo de carga y escenario, catalogada formalmente como **muestra exploratoria** (no asume por sí sola significancia estadística definitiva).
- **Métricas y Percentiles Reportados**:
  - Mediana (P50), Percentil 90 (P90) y Percentil 95 (P95).
  - Intervalos de confianza al 95% calculados mediante técnica de bootstrap no paramétrico (10.000 remuestreos).
  - Medidas de dispersión empírica: Rango intercuartílico (IQR = P75 - P25) y rango total $[P_{min}, P_{max}]$.
  - Exclusión explícita de P99 dado que una muestra de $N=100$ no provee soporte estadístico para estimar un percentil de orden 99 con significancia.
- **Condiciones de Equivalencia Experimental**:
  - Mismo commit SHA de Git, Python 3.11, dependencias bloqueadas y configuración idéntica de pool SQLAlchemy (`pool_pre_ping=True`).
  - Separación rigurosa entre ejecuciones frías (*cold start*, primer handshake TLS y conexión en pool vacío) y calientes (*warm start*, conexiones recicladas).
  - Peticiones interleaved / alternadas cronológicamente para mitigar el impacto de fluctuaciones horarias en la red WAN pública.

### 4.3 Inviabilidad de Mocks Remotos y Restricciones de Acceso a Shell

- **Inviabilidad de Mocks Remotos**: Queda terminantemente descartada cualquier estrategia de pruebas basada en enviar peticiones HTTP a `POST /webhook` en un entorno desplegado inyectando dobles de prueba (`AsyncMock`) o interceptores en memoria desde un cliente externo. En un proceso remoto en ejecución en Render, no es técnicamente posible inyectar mocks sin modificar el código fuente desplegado o exponer puertos y endpoints de depuración inseguros.
- **Restricciones de Shell en Render**: El servicio de backend configurado en Render en este proyecto no cuenta con acceso a shell interactiva ni facilidades para subir y ejecutar scripts ad-hoc de benchmarking directamente en el entorno de producción. Por ende, no se asume viabilidad de scripts ad-hoc en Render.

---

## 5. Diagnóstico de Entornos de Prueba y Declaración de Bloqueo

### 5.1 Estado de Infraestructura de Staging y Runners Regionales

Para llevar a cabo una comparación empírica rigurosa entre regiones, se requiere indispensablemente:
1. Un proceso ejecutor del backend en **Frankfurt** (misma región de Render).
2. Un proceso ejecutor del backend en **São Paulo** (misma región de Supabase `sa-east-1`).
3. Una base de datos PostgreSQL de **staging aislada y accesible** alojada en São Paulo.

**Resultado de la Verificación en el Repositorio y Entorno**:
- Las bases de datos PostgreSQL de staging y los runners de backend regionales en Frankfurt y São Paulo **no están confirmados como disponibles en este contexto de proyecto** (sin inferir su inexistencia formal meramente por la ausencia de referencias en el checkout local).
- Rige la prohibición estricta de utilizar la base de datos de producción para pruebas de carga o benchmarking, así como la prohibición de aprovisionar o crear recursos pagos.

### 5.2 Dictamen Formal de Bloqueo y Diferimiento

- **Dictamen**: La ejecución empírica de las pruebas de comparación regional de latencia queda **FORMALMENTE BLOQUEADA / DIFERIDA**.
- **Prerrequisitos Concretos Indispensables para Desbloquear**:
  1. *Base de Datos de Staging Confirmada*: Instancia PostgreSQL en São Paulo (`sa-east-1`) aprovisionada y accesible para testing, con migraciones aplicadas y dataset semilla no productivo (1 usuario de prueba, 8 categorías activas, 50 movimientos iniciales).
  2. *Runner Regional en Frankfurt*: Entorno de ejecución en Frankfurt con Python 3.11, dependencias del proyecto instaladas y conectividad autorizada a la base de staging.
  3. *Runner Regional en São Paulo*: Entorno de ejecución en São Paulo (o con latencia de red despreciable respecto a la base de staging).
  4. *Variables de Entorno Aisladas*: Configuración de credenciales de staging estrictamente separadas de cualquier clave o secreto de producción.
- En ausencia de estos prerrequisitos, el Spike cumple plenamente su alcance mediante la implementación, validación unitaria local y documentación exhaustiva de las herramientas de diagnóstico.

---

## 6. Evaluación Técnica y Recomendación Formal sobre STK-229 (Transaction Pooling 6543)

### 6.1 Alcance Geográfico y Supavisor

- **Topología del Pooler**: En la arquitectura de Supabase, Supavisor (pooler en puerto 6543 en modo transacción) se ejecuta en la misma infraestructura adyacente a PostgreSQL en **São Paulo** (`sa-east-1`).
- **Impacto sobre la Latencia Transatlántica**: Cambiar el puerto de conexión de 5432 (directo de sesión) a 6543 (transaction pooling) **no altera la distancia física entre el backend en Frankfurt y la base de datos en São Paulo**.
- Cada consulta enviada a través de Supavisor sigue atravesando el enlace WAN transatlántico entre continentes (cuyo RTT real no fue medido empíricamente en este Spike).
- El transaction pooling resuelve problemas de escalabilidad y contención de memoria en el servidor ante cientos de clientes concurrentes (multiplexando conexiones de servidor), pero por sí solo no elimina ni reduce la penalidad de red de múltiples viajes secuenciales por mensaje.

### 6.2 Incompatibilidad Arquitectónica con Advisory Locks del Scheduler

Se realizó una inspección minuciosa del código vigente en `app/scheduler.py` (líneas 615 a 646, commit `1e39b471ee925d8c1da21c9f42a133dcf15efb71` en `origin/main`):

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
        ...
    finally:
        session.close()
        try:
            lock_conn.rollback()
            unlocked = lock_conn.execute(text("SELECT pg_advisory_unlock(5354418701)")).scalar()
            ...
```

#### Análisis de Incompatibilidad Estructural:
1. **Semántica de Sesión de `pg_try_advisory_lock`**: En PostgreSQL, los advisory locks de nivel de sesión (`pg_try_advisory_lock` / `pg_advisory_unlock`) están atados unívocamente a la **conexión física del servidor** (backend PID). Para ser liberado correctamente, el unlock debe ejecutarse en el mismo proceso de servidor que adquirió el lock.
2. **Comportamiento de Transaction Pooling (Puerto 6543)**: En modo transacción, Supavisor asigna una conexión física del servidor únicamente mientras una transacción está activa. Al ejecutarse `session.commit()` (línea 629), la transacción finaliza y Supavisor devuelve inmediatamente la conexión física del servidor al pool general, pudiendo reasignarla a cualquier otra petición.
3. **Fallo Crítico al Liberar el Lock**: Cuando se ejecuta el bloque `finally` e invoca `pg_advisory_unlock(5354418701)` (línea 641), dicha sentencia se ejecutará sobre una transacción nueva que puede estar respaldada por una conexión física de servidor totalmente diferente:
   - Si la conexión nueva no poseía el lock, `pg_advisory_unlock` retornará `false` o fallará.
   - La conexión física original que realmente retiene el lock permanecerá bloqueada en el servidor PostgreSQL hasta que muera o se desconecte, impidiendo ejecuciones posteriores del scheduler y generando un bloqueo permanente silencioso.
4. **Insuficiencia de `pg_try_advisory_xact_lock`**: El uso de locks de nivel de transacción tampoco es una solución directa, ya que se liberarían prematuramente al primer `session.commit()` intermedio del proceso de detección diaria, dejando desprotegida la ejecución restante contra workers concurrentes.

### 6.3 Recomendación Formal sobre STK-229

Con base en la evidencia técnica recopilada:
- Queda expresamente prohibido cancelar STK-229 o dictaminar causas raíz sin mediciones empíricas de descomposición.
- Sin embargo, la incompatibilidad arquitectónica de los advisory locks del scheduler demuestra que adoptar STK-229 mediante un cambio global de `DATABASE_URL` al puerto 6543 rompería el mecanismo de exclusión mutua de jobs en background.
- Por lo tanto, la recomendación formal y explícita es:

> **RECOMENDACIÓN FORMAL: DIFERIR STK-229**
>
> Se recomienda **DIFERIR la decisión y la implementación de STK-229** hasta que se cumplan dos condiciones técnicas indispensables:
> 1. Disponer de mediciones empíricas mediante el prototipo de telemetría de STK-232 (`sql_count`, `sql_exec_ms`, RTT) que cuantifiquen fehacientemente qué porcentaje de la latencia corresponde a contención de pool vs. tránsito de red WAN.
> 2. Diseñar e implementar una solución arquitectónica que aísle las conexiones del scheduler (por ejemplo, una topología dual donde el webhook transaccional utilice Supavisor en puerto 6543 y el scheduler conserve una conexión dedicada de sesión en puerto 5432) antes de alterar la configuración global del pool.

---

## 7. Verificación Local del Prototipo de Telemetría

La implementación de telemetría de eventos SQL se verificó rigurosamente mediante el runner oficial aislado `.codex\dev.py`:

```powershell
python -I .codex\dev.py test -q tests/test_telemetry.py tests/test_sql_telemetry.py
python -I .codex\dev.py lint
git diff --check
```

### Casos de Prueba Validados en `tests/test_sql_telemetry.py`:
1. `test_sql_telemetry_single_query_count_and_timing`: Verifica que una consulta incremente `sql_count` en 1 y acumule tiempo en `sql_exec_ms`.
2. `test_sql_telemetry_executemany_single_event`: Verifica que una inserción masiva con `executemany` compute exactamente 1 evento en `sql_count`.
3. `test_sql_telemetry_context_none_graceful_omission`: Simula eventos con `context=None`, confirmando que se incremente `sql_count` y se omita el tiempo de forma segura sin lanzar `AttributeError`.
4. `test_sql_telemetry_handle_error_no_double_count`: Provoca un error forzado de base de datos (`IntegrityError`) y verifica que se acumule la duración en `sql_exec_ms` sin incrementar `sql_count` por segunda vez.
5. `test_sql_telemetry_noop_outside_pipeline`: Comprueba que consultas ejecutadas sin telemetría activa (`None`) no generen llamadas ni sobrecarga.
6. `test_sql_telemetry_concurrency_isolation`: Ejecuta dos contextos concurrentes mediante `asyncio.gather` validando que las métricas de SQL permanezcan estrictamente aisladas por contexto.
7. `test_sql_telemetry_strict_privacy_no_identifiers`: Inspecciona todas las emisiones y estructuras verificando la ausencia total de `message_id`, WAMID, texto SQL, parámetros y datos de usuario en el canal anónimo de SQL.
