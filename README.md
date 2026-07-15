[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/blob1618/luka)

# LUKA

LUKA es un asistente financiero por WhatsApp que ayuda a los usuarios a registrar ingresos y egresos desde mensajes en lenguaje natural. El punto de entrada del producto es WhatsApp a través de la Meta WhatsApp Business API.

STK-35 implementa el registro de movimientos financieros por texto. El bot recibe el mensaje, usa un proveedor LLM para interpretarlo, valida y persiste el movimiento mediante el backend y recién entonces confirma el resultado por WhatsApp. Las notas de voz e imágenes de comprobantes están preparadas en el código pero todavía no están implementadas.

## Registro de movimientos por texto (STK-35)

Flujo implementado:

```text
WhatsApp webhook -> LLMService -> FinanceService -> public.movimientos_financieros -> respuesta
```

- Los movimientos registrables pueden ser `ingreso` o `egreso`, según `movement_type`.
- `intent="expense"` se mantiene para ambos tipos por compatibilidad con el contrato existente del LLM.
- El backend confirma el registro solo después de que la escritura en base de datos termina correctamente; el LLM no es autoridad para confirmar persistencia.
- El remitente debe corresponder a un usuario previamente registrado y vinculado mediante `public.usuario.whatsapp_id`. STK-35 no implementa alta, registro, login ni vinculación inicial de usuarios.
- Si el usuario no existe, el movimiento no se registra.
- `FinanceService` asocia `categoria_id` solo cuando encuentra una categoría activa del usuario. No crea categorías automáticamente y, si no hay coincidencia, guarda `categoria_id=null`.
- Los intents `greeting`, `out_of_scope`, `reminder`, `budget_query` y `expense_summary` no se persisten como movimientos.

El flujo oficial de alta/vinculación de usuarios, las categorías default o personalizadas y la consulta de movimientos de STK-128 quedan fuera de STK-35. El acceso seguro al futuro micrositio/dashboard mediante Magic Link está relacionado con STK-54 y requiere coordinación entre backend y frontend; no está implementado por este ticket.

## Stack

- Backend: Python 3.11, FastAPI, Uvicorn/Gunicorn.
- Mensajería: Meta WhatsApp Business API.
- IA: Gemini o Mistral, seleccionado con `LLM_PROVIDER`.
- Base de datos: SQLAlchemy ORM. El desarrollo local usa SQLite por defecto. Producción debe usar PostgreSQL, normalmente Supabase.
- Jobs en background / caché: APScheduler y Redis.
- Deploy: Docker y Render.
- Tests / calidad: Pytest y Ruff.
- Frontend: no hay frontend web en este repositorio por ahora. La interfaz del producto es WhatsApp.

## Mapa del repositorio

- `app/main.py`: app FastAPI, endpoint de health, verificación del webhook de WhatsApp e ingesta de mensajes.
- `app/api/whatsapp.py`: cliente de salida hacia la API de WhatsApp.
- `app/services/llm.py`: fachada LLM que interpreta mensajes y normaliza el tipo de movimiento.
- `app/services/llm_providers/`: implementaciones de los providers Gemini y Mistral.
- `app/services/finance.py`: validación y persistencia de movimientos financieros y otras reglas de negocio.
- `app/models/database.py`: engine, sesión y modelos SQLAlchemy.
- `tests/`: tests del backend.
- `docs/database.md`: estado actual de la base de datos, esquema objetivo del MVP y decisiones pendientes.
- `docs/architecture.md`: flujo de información y resumen de arquitectura del MVP.
- `SUPABASE_SETUP.md`: guía de configuración de la base de datos.
- `RENDER_DEPLOYMENT.md`: notas de despliegue en Render.
- `AGENTS.md`: notas de ingeniería para agentes de IA.

## Configuración local del backend

Desde la raíz del repositorio:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --reload
```

La API levanta en `http://127.0.0.1:8000`.

Verificación rápida:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/
```

Respuesta esperada:

```json
{"message":"Luka API is running"}
```

Para macOS/Linux, usar `python3 -m venv .venv`, `source .venv/bin/activate`, y `cp .env.example .env`.

## Frontend

No hay frontend que levantar por ahora. La única interfaz del producto es WhatsApp, así que el trabajo local pasa por el backend FastAPI y el endpoint `/webhook`.

Para probar eventos reales de WhatsApp, Meta necesita una URL pública HTTPS. Usar Render o un túnel local, y luego configurar Meta para que llame a:

```text
https://<url-pública>/webhook
```

## Variables de entorno

Copiar `.env.example` a `.env` y completar solo lo que tu tarea necesite.

| Variable | Requerida | Notas |
| --- | --- | --- |
| `WHATSAPP_VERIFY_TOKEN` | Sí para verificación del webhook | Token configurado en Meta y verificado por `GET /webhook`. |
| `WHATSAPP_API_TOKEN` | Sí para enviar respuestas por WhatsApp | Token bearer de la API de Meta. |
| `WHATSAPP_PHONE_ID` | Sí para enviar respuestas por WhatsApp | ID del número de teléfono de WhatsApp en Meta. |
| `LLM_PROVIDER` | Opcional | `gemini` por defecto. También soporta `mistral`. |
| `GEMINI_API_KEY` | Requerido si `LLM_PROVIDER=gemini` | API key de Gemini. |
| `GEMINI_MODEL` | Opcional | Por defecto en `.env.example` es `gemini-2.0-flash`. |
| `MISTRAL_API_KEY` | Requerido si `LLM_PROVIDER=mistral` | API key de Mistral. |
| `MISTRAL_MODEL` | Opcional | Por defecto en `.env.example` es `mistral-small-latest`. |
| `DATABASE_URL` | Opcional en local, requerido en producción | Por defecto `sqlite:///./luka.db`. Usar Supabase/PostgreSQL para entornos compartidos. |
| `REDIS_URL` | Opcional en local, recomendado en producción | Por defecto `redis://localhost:6379`. La app loguea un error si Redis no está disponible pero igual arranca. |

Nunca subir `.env` ni secretos reales al repo.

## Base de datos

Base de datos local por defecto:

```text
sqlite:///./luka.db
```

Base de datos de producción/compartida:

```text
postgresql://...
```

Usar Supabase salvo que el equipo decida otra cosa. Los detalles de configuración están en `SUPABASE_SETUP.md`.

Para crear las tablas actuales desde los modelos SQLAlchemy en una base local de desarrollo:

```powershell
python -c "from app.models.database import engine, Base; Base.metadata.create_all(bind=engine)"
```

El repositorio contiene migraciones SQL versionadas en `database/migrations/`, aunque todavía no hay una herramienta formal de migraciones configurada. No usar `Base.metadata.create_all()` para actualizar una base compartida de Supabase. Si un cambio toca el esquema, debe versionarse y coordinarse con el equipo antes de aplicarlo; ver `docs/database.md`.

`database/reference/schema_supabase_inicial_legacy.sql` es un snapshot histórico no ejecutable: no representa el estado actual y no debe usarse para reconstruir ni reparar Supabase. Las migraciones versionadas describen el contrato esperado, pero solo Supabase remoto demuestra qué cambios están realmente aplicados. Después de aplicar y verificar una migración deberá generarse un snapshot nuevo mediante un procedimiento controlado.

## Limitaciones conocidas y trabajo relacionado

- La deduplicación por `whatsapp_message_id` evita insertar dos filas, pero un reintento de Meta puede generar una segunda respuesta visible indicando que el movimiento estaba duplicado.
- Los índices productivos de usuarios y movimientos, incluido el índice único parcial de `whatsapp_message_id`, deben verificarse en Supabase. El contrato versionado puede contenerlos sin que eso pruebe su aplicación remota.
- El flujo completo puede tardar aproximadamente entre 5 y 10 segundos por la suma de LLM, base de datos, API de WhatsApp y hosting.
- Faltan métricas de latencia por etapa e investigación sobre typing indicator o mark as read en WhatsApp Business API.
- Quedan pendientes rate limiting, protección frente al abuso de tokens y optimizaciones para evitar llamadas innecesarias al LLM: validar usuarios y duplicados antes del LLM y usar un pre-router para saludos o mensajes fuera de alcance.

## Verificar cambios

GitHub Actions corre verificaciones automáticas. El workflow actual corre en pushes a cualquier rama y en Pull Requests a `main`.

Si el equipo no está usando Pull Requests, la verificación automática ocurre cuando el cambio llega a `main`. En lo posible, correr las mismas verificaciones en local antes de integrar cambios:

```powershell
python -m ruff check .
python -m pytest -v
```

Las pruebas reales de WhatsApp ocurren después de que `main` esté desplegado en Render, porque el proyecto depende del número de teléfono de Meta, la URL del webhook y la base de datos compartida.

## Flujo Jira y ramas

La rama de despliegue compartida es `main`.

Flujo mínimo:

1. Tomar un ticket de Jira.
2. Usar la extensión de Jira en VS Code para crear o tomar la rama del ticket.
3. Desarrollar el cambio.
4. Pushear a GitHub.
5. Dejar que GitHub Actions corra cuando aplique.
6. Integrar a `main` según el acuerdo actual del equipo.
7. Render despliega `main` automáticamente.
8. Probar el flujo real en WhatsApp.
9. Si algo falla, revisar los logs de Render y corregirlo.
10. Confirmar que Jira movió el estado del ticket. Si no lo hizo, actualizarlo manualmente.

Los Pull Requests no son obligatorios en el flujo actual.

Más detalle en `CONTRIBUTING.md`.

## Links del proyecto

- GitHub: https://github.com/blob1618/luka
- Deploy en Render: https://luka-f2nb.onrender.com
- DeepWiki overview: https://deepwiki.com/blob1618/luka/1-luka-overview
- DeepWiki arquitectura: https://deepwiki.com/blob1618/luka/2-core-architecture
- Arquitectura MVP: `docs/architecture.md`
- Configuración de base de datos: `SUPABASE_SETUP.md`
- Notas de base de datos: `docs/database.md`
- Deploy en Render: `RENDER_DEPLOYMENT.md`
- Jira: URL del equipo pendiente.
- Confluence: URL del equipo pendiente.

## Deploy / pruebas en WhatsApp

Render puede desplegar este repo con el `Dockerfile` y `render.yaml` incluidos.

URL actual en Render:

```text
https://luka-f2nb.onrender.com
```

Configuración rápida en Render:

1. Crear un Web Service en Render.
2. Conectar este repositorio de GitHub.
3. Usar Docker.
4. Agregar las variables de entorno desde `.env`.
5. Desplegar.
6. Configurar la URL de callback del webhook de Meta como `https://luka-f2nb.onrender.com/webhook` para el deploy actual, o `https://<render-app>.onrender.com/webhook` para un servicio nuevo.

Ver `RENDER_DEPLOYMENT.md` para más detalle.
