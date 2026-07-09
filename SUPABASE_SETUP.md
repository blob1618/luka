# Guía de configuración de Supabase

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

## Paso 4: Inicializar las tablas de la base de datos

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

## Paso 5: Verificar la conexión (opcional)

Probar que la conexión funciona:

```bash
python -c "from app.models.database import SessionLocal; db = SessionLocal(); print('✓ Conectado a Supabase!')"
```

## Paso 6: Desplegar en Render

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
- Para el tier gratuito de Supabase: 500MB de almacenamiento, suficiente para desarrollo
- El connection pooling está disponible con PgBouncer de Supabase (habilitarlo en Settings si se alcanzan los límites de conexiones)

## 🔗 Features útiles de Supabase

- **SQL Editor:** Ir a **SQL Editor** para ejecutar consultas raw o gestionar tablas
- **Auth:** Supabase tiene auth integrado (opcional, no requerido para la configuración actual)
- **Storage:** Para subida de archivos (comprobantes de gastos, etc.)
- **Real-time:** Para actualizaciones en vivo entre clientes

## Solución de problemas

**"Connection refused"** → Verificar que `DATABASE_URL` sea correcto, pegarlo exactamente desde Supabase

**"too many connections"** → El tier gratuito tiene límite de conexiones. Habilitar PgBouncer en Settings de Supabase

**"relation does not exist"** → Ejecutar el comando de inicialización del Paso 4 para crear las tablas
