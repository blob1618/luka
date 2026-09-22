# Desarrollo

Guía práctica para levantar, testear y desplegar el backend de LUKA.

## Setup local

Requisitos: Python 3.11 (la versión que usan CI y el `Dockerfile`), Git y, opcionalmente, Redis local para los flujos multi-turno.

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app.main:app --reload
```

Windows (PowerShell):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --reload
```

La API queda en `http://127.0.0.1:8000`. Health check:

```text
GET http://127.0.0.1:8000/
{"message":"Luka API is running"}
```

En local, `DATABASE_URL` por defecto es `sqlite:///./luka.db`. `REDIS_URL` por defecto es `redis://localhost:6379`: la app arranca aunque Redis no esté disponible, pero los flujos multi-turno (confirmación de categoría, recordatorios en pasos, rename) no funcionan sin él.

## Variables de entorno

Copiar `.env.example` a `.env` y completar solo lo necesario. Nunca subir `.env` ni secretos reales al repositorio. Toda variable nueva debe agregarse también a `.env.example`.

| Variable | Requerida | Notas |
| --- | --- | --- |
| `WHATSAPP_VERIFY_TOKEN` | Sí para verificar el webhook | Token configurado en Meta y validado por `GET /webhook`. |
| `WHATSAPP_API_TOKEN` | Sí para enviar por WhatsApp | Token bearer de la API de Meta. |
| `WHATSAPP_PHONE_ID` | Sí para enviar por WhatsApp | ID del número de teléfono en Meta. |
| `WHATSAPP_GRAPH_API_VERSION` | Opcional | Versión de Graph API. Default `v26.0`; un formato inválido se rechaza. |
| `FLOW_ADMIN_API_KEY` | Solo para la API admin de conversation flows | Se envía como bearer en `/admin/conversation-flows/*`. Sin valor, esa API responde 503. |
| `LLM_PROVIDER` | Opcional | `gemini` (default) o `mistral`. |
| `GEMINI_API_KEY` | Si `LLM_PROVIDER=gemini` | API key de Gemini. |
| `GEMINI_MODEL` | Opcional | Default `gemini-3.1-flash-lite`. |
| `MISTRAL_API_KEY` | Si `LLM_PROVIDER=mistral` | API key de Mistral. |
| `MISTRAL_MODEL` | Opcional | Default `mistral-small-latest`. |
| `DATABASE_URL` | Opcional en local, requerida en producción | Local: `sqlite:///./luka.db`. Compartido/producción: PostgreSQL/Supabase. |
| `REDIS_URL` | Opcional en local, recomendada en producción | Default `redis://localhost:6379`. La app loguea el fallo y sigue arrancando si no responde. |
| `CONVERSATION_MEMORY_TURNS` | Opcional | Turnos completos conservados en la memoria conversacional. Default `4`. |
| `CONVERSATION_MEMORY_TTL_HOURS` | Opcional | Horas de vigencia de la memoria desde la última actividad. Default `24`. |
| `CONVERSATION_MEMORY_MAX_CHARS` | Opcional | Máximo de caracteres por mensaje saneado antes de guardarlo en la memoria. Default `1200`. |
| `ONBOARDING_REGISTRATION_URL` | Para onboarding y dashboard | URL base de registro; también define el host del login del dashboard. |
| `ONBOARDING_INVITATION_TTL_MINUTES` | Opcional | Default del ejemplo: `30`. |
| `ONBOARDING_RESEND_COOLDOWN_SECONDS` | Opcional | Default del ejemplo: `60`. |
| `ONBOARDING_MAX_RESENDS` | Opcional | Default del ejemplo: `3`. |
| `WHATSAPP_REMINDER_TEMPLATE_NAME` | Para recordatorios de pago | Template de WhatsApp aprobado por Meta. El ejemplo usa `recordatorio_pago_vencimiento`. |
| `PROACTIVE_PROMPT_HOUR` | Opcional | Hora local (0-23, America/Argentina/Buenos_Aires) del recordatorio proactivo diario. Sin valor o inválida, la feature queda apagada. |
| `DASHBOARD_LOGIN_TTL_MINUTES` | Opcional | Default del ejemplo: `10`. |
| `DASHBOARD_LOGIN_RESEND_COOLDOWN_SECONDS` | Opcional | Default del ejemplo: `60`. |
| `DASHBOARD_LOGIN_MAX_RESENDS` | Opcional | Default del ejemplo: `3`. |

## Tests y verificación

GitHub Actions corre en pushes a cualquier rama y en Pull Requests a `main`: instala dependencias, ejecuta `pytest -v`, corre `ruff check .` y valida las migraciones de Supabase con el CLI.

Comandos locales (desde la raíz, con el venv activo):

```bash
# Ciclo rápido: tests marcados como unitarios, sin red ni base real
python -m pytest -q -m unit

# Regresión focalizada del webhook y la idempotencia
python -m pytest -q tests/test_webhook.py tests/test_webhook_idempotency.py --durations=40

# Suite completa
python -m pytest -v

# Lint
ruff check .
```

Aislamiento:

- `pytest.ini` limita `testpaths` a `tests/`. Los tests de `testing/tests/` se ejecutan dentro del entorno Docker de testing.
- `tests/conftest.py` provee un `FakeRedis` en memoria y un guardia a nivel de sockets que falla ante conexiones de red accidentales.
- Los tests no requieren Redis, Supabase, Meta ni LLM reales; los providers se reemplazan con los fakes de `tests/provider_fakes.py`.
- Las migraciones se validan en CI con `supabase db reset` y `supabase db lint --level warning`.

## Base de datos local

Para una SQLite local descartable, se pueden crear las tablas desde los modelos:

```bash
python -c "from app.models.database import engine, Base; Base.metadata.create_all(bind=engine)"
```

No usar este comando para actualizar una base compartida. Todo cambio de esquema se versiona como migración en `supabase/migrations/` (crear con `supabase migration new <nombre>`, validar con `supabase db reset`) y se aplica por el flujo del equipo. Ver [database.md](database.md).

## Entorno de testing

`testing/` contiene una app Streamlit que simula el flujo de WhatsApp contra el mismo backend, sin la API de Meta. Se levanta únicamente con Docker o Podman (Streamlit no está en el `requirements.txt` raíz) y usa una base SQLite aislada. Ver la guía completa en [../testing/README.md](../testing/README.md).

## Deploy en Render

Deploy actual:

```text
https://luka-f2nb.onrender.com
```

- Health check: `GET https://luka-f2nb.onrender.com/` → `{"message":"Luka API is running"}`.
- Webhook de Meta: `https://luka-f2nb.onrender.com/webhook`.
- Build: Render usa el `Dockerfile` del repo; `render.yaml` es una plantilla del servicio (los secretos se configuran en el dashboard, no en el archivo).
- Comando de arranque del `Dockerfile`:

```bash
gunicorn -w 1 -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${PORT:-8000}
```

El worker es uno solo a propósito: el scheduler debe correr una sola vez, no una vez por worker.

Variables a configurar en Render: `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_API_TOKEN`, `WHATSAPP_PHONE_ID`, `LLM_PROVIDER`, `GEMINI_API_KEY`/`GEMINI_MODEL` o `MISTRAL_API_KEY`/`MISTRAL_MODEL` según el provider, `DATABASE_URL` y `REDIS_URL`. Agregar `ONBOARDING_REGISTRATION_URL` si se usan onboarding o el link de dashboard, `WHATSAPP_REMINDER_TEMPLATE_NAME` para recordatorios de pago y `PROACTIVE_PROMPT_HOUR` para el recordatorio proactivo.

Al arrancar, la app prueba Redis con un timeout de 3 segundos. Si falla, la API sigue disponible y el error queda en los logs; validar la conectividad con `GET /redis-test`. `main` es la rama de despliegue: Render la despliega automáticamente y las pruebas reales de WhatsApp dependen del número de Meta, la URL del webhook y la base compartida. Si el health check responde pero WhatsApp falla, revisar primero los logs de Render.

## Flujo de trabajo del equipo

La rama compartida y de despliegue es `main`. El flujo de ramas, PRs y verificación de tareas está en [../CONTRIBUTING.md](../CONTRIBUTING.md). GitHub Actions corre en pushes y en PRs a `main`; Render despliega `main` automáticamente.

## Mapa del repositorio

- `app/main.py`: app FastAPI, webhook de WhatsApp (verificación, ingesta y respuesta), health y endpoints admin de conversation flows.
- `app/api/whatsapp.py`: único cliente saliente hacia la API de WhatsApp.
- `app/services/`:
  - `dispatcher.py`: orquesta el procesamiento de cada mensaje entrante.
  - `llm.py` y `llm_providers/`: fachada LLM y providers Gemini/Mistral.
  - `llm_contract.py`, `conversation_flow_contract.py`: contrato de la respuesta del LLM y de los flujos configurables.
  - `finance.py`: validación y persistencia de movimientos.
  - `limit.py`: CRUD de límites mensuales por categoría.
  - `budget.py`: cálculo de consumo, disponibilidad y exceso.
  - `reminder.py`: CRUD de recordatorios.
  - `onboarding.py`: alta y vinculación de usuarios e invitaciones.
  - `conversation.py`, `conversation_flow.py`, `conversation_flow_runtime.py`: estado multi-turno en Redis y flujos configurables.
  - `dashboard_link.py`, `telemetry.py`, `reference_resolution.py`, `intent_routing.py`, `categories_taxonomy.py`, `webhook_idempotency.py`.
- `app/models/database.py`: engine, sesión y modelos SQLAlchemy.
- `app/scheduler.py`: jobs en background (recordatorios de pago y recordatorio proactivo diario).
- `tests/`: suite del backend. `testing/`: entorno Streamlit con tests propios en `testing/tests/`.
- `supabase/migrations/`: historial de migraciones de Supabase.
- `alembic/`: configuración y versiones de migración de Alembic (baseline inicial STK-210).
- `scripts/`: herramientas operativas y de mantenimiento (`adopt_existing_database.py`, `audit_schema_adoption.py`).
- `docs/`: [architecture.md](architecture.md), [features.md](features.md), [conversation-flows.md](conversation-flows.md), [database.md](database.md), [development.md](development.md), [audits/README.md](audits/README.md) y [decisions/0001-mvp-db-contract.md](decisions/0001-mvp-db-contract.md).
- Raíz: `README.md`, `AGENTS.md`, `CONTRIBUTING.md`, `alembic.ini` y `prompt.md` (prompt de runtime del LLM).

## Deuda técnica observada

- `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` en `fastapi.testclient`.
- `DeprecationWarning: Call to deprecated setex. (Use 'set' instead.)` en `app/services/conversation.py`: migrar los `setex(...)` a `set(..., ex=...)`.
