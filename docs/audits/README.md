# Auditoría de Esquema y Snapshot de Solo Lectura (STK-210)

Este directorio documenta el procedimiento de auditoría de esquema de solo lectura para validar la base de datos de Supabase frente a la baseline canónica de Alembic sin exponer credenciales, archivos `.env` ni acceder a datos de usuarios (cero PII).

## 1. Procedimiento de Extracción de Snapshot

Para auditar el esquema real de Supabase sin necesidad de inyectar cadenas de conexión al entorno local:

1. Ejecutar el script SQL de solo lectura:
   [`export_supabase_catalog_snapshot.sql`](export_supabase_catalog_snapshot.sql)
   en el **SQL Editor de Supabase** o mediante un cliente `psql` conectado con un rol de solo lectura.
2. Copiar el resultado JSON producido en la columna `schema_snapshot_json` y **guardarlo en una ruta fuera del repositorio** (por ejemplo, en un directorio temporal externo como `/tmp/supabase_schema_snapshot.json` o `%TEMP%\luka-audits\supabase_schema_snapshot.json`).

> **RECOMENDACIÓN DE SEGURIDAD OPERATIVA:**
> **No guarde ni versione el archivo de snapshot ni los reportes generados dentro del árbol de Git.**
> Consérvelos como evidencia operativa en una ubicación externa al repositorio para evitar agregar artefactos pesados o datos de entorno al historial.

### Garantías de Seguridad
- El script consulta **exclusivamente catálogos del sistema** (`pg_catalog`, `information_schema`).
- **Cero acceso a datos de tablas**: No se ejecuta ningún `SELECT` sobre tablas de usuario.
- **Cero DDL**: No altera tablas, secuencias, esquemas ni políticas.

## 2. Ejecución de la Auditoría Offline

Una vez obtenido el archivo de snapshot JSON fuera del repositorio, ejecutar el auditor indicando su ruta externa y dirigiendo el reporte de salida también a una ubicación externa:

```bash
python scripts/audit_schema_adoption.py --snapshot /tmp/supabase_schema_snapshot.json --output /tmp/stk_210_supabase_audit.json
```

El script validará contra el Contrato de Comparación Canónico:
- Presencia exacta de las 15 tablas oficiales (excluyendo objetos de infraestructura de migraciones como `alembic_version`).
- Columnas, tipos normalizados, nullability y defaults.
- Primary keys, Unique constraints y Check constraints (con verificación semántica de operadores).
- Foreign keys y acciones referenciales (`ON DELETE CASCADE`, `ON DELETE SET NULL`, `NO ACTION`).
- Índices simples, compuestos y predicados `WHERE` de índices parciales.
- Estado de Row Level Security (RLS) habilitado en las 12 tablas protegidas.
- Ausencia de políticas públicas para `anon` o `authenticated`.
- Privilegios no otorgados a `anon`/`authenticated` en tablas sensibles (`candidato_gasto_recurrente`, `aviso_recordatorio`, `cron_job_claim`).
- Presencia de clave foránea a `auth.users(id)` en `usuario.auth_user_id`.
- Extensiones instaladas (`uuid-ossp`).

## 3. Comparación de Reportes

Para contrastar el snapshot contra el reporte generado por la baseline de Alembic:

```bash
python scripts/audit_schema_adoption.py --compare /tmp/alembic_audit.json /tmp/stk_210_supabase_audit.json
```

Si ambos esquemas presentan paridad estricta y huellas SHA-256 idénticas, el comando saldrá con código `0` confirmando paridad canónica.

## 4. Prueba Opcional de Regresión en Suite Automatizada

El archivo de tests versionado `tests/test_migrations.py` incluye una prueba opcional de regresión: `test_real_supabase_catalog_snapshot_parity_regression`.

- **Comportamiento por defecto**: Si no se proporciona la opción `--supabase-snapshot` ni la variable `LUKA_SUPABASE_SNAPSHOT_PATH`, la prueba se **omite limpiamente** (`pytest.skip`) explicando la razón, asegurando que CI y desarrolladores sin el snapshot local ejecuten la suite sin fallos (reportando `skipped`).
- **Ejecución con evidencia operativa**: Para ejecutar la prueba contra un snapshot real extraído fuera del repositorio:

```bash
# Mediante argumento CLI (recomendado con dev.py debido al aislamiento de variables de entorno):
python -I .codex/dev.py test -q tests/test_migrations.py --supabase-snapshot /tmp/supabase_schema_snapshot.json
```

```powershell
# En PowerShell (Windows):
python -I .codex\dev.py test -q tests/test_migrations.py --supabase-snapshot "C:\ruta\externa\supabase_schema_snapshot.json"
```

O si se invoca `pytest` directamente en entornos donde no se aíslan variables de entorno:
```bash
export LUKA_SUPABASE_SNAPSHOT_PATH="/tmp/supabase_schema_snapshot.json"
pytest -q tests/test_migrations.py -k test_real_supabase_catalog_snapshot_parity_regression
```

