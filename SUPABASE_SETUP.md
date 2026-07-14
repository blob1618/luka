# Guía de configuración de base de datos

LUKA usa SQLite por defecto para desarrollo local y PostgreSQL/Supabase en entornos compartidos. Son escenarios distintos:

- En local se pueden crear tablas temporales o de desarrollo desde los modelos SQLAlchemy.
- En Supabase compartido, todo cambio de esquema debe estar versionado en `database/migrations/` y coordinarse antes de aplicarlo.
- `database/schema_supabase_actual.sql` es un snapshot de referencia, no una migración ejecutable ni una garantía del estado remoto actual.

El contrato vigente de Release 1 está documentado en `docs/decisions/0001-mvp-db-contract.md`: `public.usuario` es la tabla oficial de usuarios y `public.movimientos_financieros` es la tabla oficial para ingresos y egresos. Las nuevas features no deben usar tablas legacy como `public.gastos`.

## Paso 1: Crear el proyecto en Supabase

1. Ir a [supabase.com](https://supabase.com)
2. Registrarse o iniciar sesión
3. Hacer clic en "New Project"
4. Completar:
   - **Nombre del proyecto:** `luka` (o cualquier nombre)
   - **Contraseña de la base de datos:** crear una contraseña segura (¡guardarla!)
   - **Región:** elegir la más cercana
5. Hacer clic en "Create new project" y esperar ~2 minutos

## Paso 2: Obtener el connection string

1. Una vez creado el proyecto, ir a **Settings → Database**
2. Buscar la sección **Connection string**
3. Hacer clic en la pestaña **"URI"** (no Pool)
4. Copiar el connection string (tiene esta forma):
   ```
   postgresql://postgres:[PASSWORD]@[HOST]:[PORT]/postgres
   ```

## Paso 3: Actualizar el entorno

Reemplazar `[PASSWORD]` con la contraseña creada y agregar al archivo `.env`:

```bash
DATABASE_URL=postgresql://postgres:TU_CONTRASEÑA@TU_HOST:5432/postgres
```

**Ejemplo:**
```bash
DATABASE_URL=postgresql://postgres:MiContraseñaSegura123@db.supabase.co:5432/postgres
```

## Paso 4: Preparar una base local

El siguiente mecanismo está destinado a SQLite o a una base local descartable de desarrollo. No debe usarse para actualizar el esquema del proyecto compartido en Supabase:

Ejecutar esto para crear todas las tablas:

```bash
python
>>> from app.models.database import engine, Base
>>> Base.metadata.create_all(bind=engine)
```

O desde la línea de comandos:
```bash
python -c "from app.models.database import engine, Base; Base.metadata.create_all(bind=engine)"
```

## Paso 5: Gestionar el esquema compartido

El repositorio ya contiene migraciones SQL versionadas, comenzando por `database/migrations/001_mvp_movimientos_financieros.sql`. Aunque todavía no hay una herramienta formal de migraciones configurada, no debe afirmarse que el proyecto carece de migraciones.

La existencia de una migración en GitHub describe el contrato esperado, pero no confirma que haya sido aplicada en Supabase. Antes de depender de una columna, tabla o índice en producción, el equipo debe verificar su aplicación mediante el proceso operativo autorizado y luego reexportar el schema de referencia.

En particular, deben verificarse en el entorno remoto:

- `public.usuario.whatsapp_id` y su índice productivo.
- `public.movimientos_financieros` y sus índices de consulta.
- El índice único parcial sobre `public.movimientos_financieros.whatsapp_message_id`, necesario para reforzar la deduplicación ante concurrencia.
- RLS habilitado en `public.movimientos_financieros`, sin asumir acceso directo para roles públicos.

Esta guía no define ni ejecuta el procedimiento de aplicación de migraciones en Supabase.

## Paso 6: Verificar la conexión (opcional)

Probar que la conexión funciona:

```bash
python -c "from app.models.database import SessionLocal; db = SessionLocal(); print('✓ Conectado a Supabase!')"
```

## Paso 7: Desplegar en Render

1. Crear una cuenta en Render y agregar un nuevo **Web Service**.
2. Conectar el repositorio de Git y seleccionar la rama a desplegar.
3. Elegir el entorno: usar **Docker** (recomendado) o **Python**.
   - Si se usa Docker: incluir el `Dockerfile` provisto en el repo — Render construirá la imagen automáticamente.
   - Si se usa Python (sin Docker): configurar el build command como `pip install -r requirements.txt` y el start command como:
     ```bash
     gunicorn -w 4 -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:$PORT
     ```
4. En el dashboard de Render, agregar las variables de entorno requeridas: `WHATSAPP_VERIFY_TOKEN`, `DATABASE_URL` y las API keys necesarias.
5. Desplegar. Render provee un servidor persistente, por lo que el scheduler interno de la app correrá con normalidad.

## ⚠️ Notas importantes

- **¡Mantener la contraseña en secreto!** No subirla al repo
- El archivo `.env` ya está en `.gitignore`
- STK-35 requiere que el remitente ya exista en `public.usuario` con su `whatsapp_id`; no implementa alta, register, login ni vinculación inicial.
- Las categorías no se crean automáticamente. Solo se asocia una categoría activa existente del usuario; de lo contrario, `categoria_id` queda en `null`.
- El dashboard y el acceso mediante Magic Link corresponden a trabajo relacionado con STK-54 y no forman parte de STK-35.
- Para el tier gratuito de Supabase: 500MB de almacenamiento, suficiente para desarrollo
- El connection pooling está disponible con PgBouncer de Supabase (habilitarlo en Settings si se alcanzan los límites de conexiones)

## 🔗 Features útiles de Supabase

- **SQL Editor:** Disponible para operaciones administradas por el equipo; no usarlo para introducir cambios de esquema sin versionarlos primero en el repositorio
- **Auth:** Supabase tiene auth integrado (opcional, no requerido para la configuración actual)
- **Storage:** Para una futura subida de archivos financieros (comprobantes, etc.); no forma parte de STK-35
- **Real-time:** Para actualizaciones en vivo entre clientes

## Solución de problemas

**"Connection refused"** → Verificar que `DATABASE_URL` sea correcto, pegarlo exactamente desde Supabase

**"too many connections"** → El tier gratuito tiene límite de conexiones. Habilitar PgBouncer en Settings de Supabase

**"relation does not exist"** → En local, revisar la inicialización indicada en el Paso 4. En Supabase compartido, verificar que la migración versionada correspondiente haya sido aplicada; no intentar reparar el entorno remoto con `Base.metadata.create_all()`.

**El webhook no encuentra al usuario** → Confirmar que el número recibido coincida con `public.usuario.whatsapp_id`. STK-35 no crea ni vincula usuarios automáticamente.

**El movimiento queda sin categoría** → Verificar que exista una categoría activa para ese usuario con el nombre interpretado. Si no existe, el comportamiento esperado es guardar `categoria_id=null`.
