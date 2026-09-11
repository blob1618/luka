# Instrucciones para agentes de IA — Luka

Luka es un asistente financiero personal que opera por WhatsApp y ayuda a los usuarios a gestionar sus finanzas a través de lenguaje natural.

## Arquitectura y stack

- **Framework**: FastAPI (async). Python 3.11.
- **LLM**: fachada en `app/services/llm.py` -> `app/services/llm_providers/` (implementaciones `gemini` y `mistral`, selección por env `LLM_PROVIDER`, default `gemini`). La respuesta normalizada del proveedor se define en `prompt.md`, no en código.
- **Mensajería**: Meta WhatsApp Business API.
- **Base de datos**: SQLAlchemy (SQLite local, PostgreSQL/Supabase en producción).
- **Estado de conversación y caché**: Redis (`app/services/conversation.py`).
- **Tareas en segundo plano**: APScheduler (`app/scheduler.py`).
- **Deploy**: Docker + Render.

## Organización del código

- `app/main.py`: entrypoint y webhook. **Nota: hoy es un archivo grande (~900 líneas) que todavía contiene bastante lógica de reply y de dispatcher; el ideal del repo es mover esa lógica a los services, no agregar más inline.**
- `app/api/whatsapp.py`: único cliente saliente de WhatsApp. Normaliza teléfonos argentinos `549...` -> `54...`.
- `app/services/llm.py` + `app/services/llm_providers/`: fachada LLM y providers. `LLMService` es singleton por clase (usar `reset_provider()` y `set_prompt_path()` en tests).
- `app/services/finance.py`: validación y persistencia de movimientos y categorías.
- `app/services/onboarding.py`: alta/vinculación de usuarios e invitaciones.
- `app/services/reminder.py`: recordatorios (CRUD, título único, multi-turno).
- `app/services/conversation.py`: estado multi-turno en Redis (confirmación de categoría, recordatorio pendiente, rename).
- `app/models/database.py`: engine, sesión y todos los modelos SQLAlchemy (un solo archivo).
- `app/scheduler.py`: jobs en background (recordatorios cada 5 min).
- `testing/`: entorno de testing aislado. App Streamlit (`testing/streamlit_app.py`) que simula el flujo de WhatsApp de Luka contra el mismo backend, con sidebar de configuración (provider/modelo LLM, simular usuario registrado, phone, nombre), paneles de debug (JSON del LLM, latencia, estado Redis, logs del dispatcher), simulador de usuario y reset de DB. Usa una DB SQLite aislada (`sqlite:///./testing_luka.db`). Solo se levanta con Docker/Podman (`testing/docker-compose.yml` + `testing/Dockerfile`, servicios `streamlit` en `:8501` y `redis` en `:6380`); tiene sus propios tests en `testing/tests/` y sus dependencias en `testing/requirements.txt`. Logos en `testing/public/`.

## Guías de ingeniería

- Las rutas de FastAPI van en `app/main.py`; la lógica compleja va en los services (parseo en LLM, cambios de estado en finance/reminder). No persistir nuevas dependencias en `app/main.py`.
- Todo mensaje saliente de WhatsApp pasa por `app/api/whatsapp.py`.
- Toda lógica basada en tiempo/notificaciones se orquesta desde `app/scheduler.py`.
- Los modelos/estructura de BD se actualizan en `app/models/database.py` (un solo archivo) con sesiones async correctas.

### Contrato DB MVP / Release 1

- `public.movimientos_financieros` es la entidad central para ingresos y egresos; `public.usuario` es la tabla oficial de usuarios, mapeada por `public.usuario.whatsapp_id`.
- No ejecutar SQL ni tocar Supabase directamente; todo cambio de schema se versiona primero (ver `docs/database.md` y `supabase/migrations/`).
- `public.movimientos_financieros` tiene RLS habilitado; no asumir policies de acceso público (roles `anon`/`authenticated`). No hay frontend -> Supabase directo salvo nueva ADR.

### Invariantes del registro por texto

- Flujo: WhatsApp webhook -> `LLMService` -> `FinanceService` -> `public.movimientos_financieros` -> respuesta.
- `intent="expense"` se conserva por compatibilidad; `movement_type` define `ingreso`/`egreso`.
- Confirmar el registro solo tras una persistencia exitosa; nunca confiar en `reply_text` del LLM.
- Requiere usuario previamente registrado y vinculado por `whatsapp_id`; STK-35 no crea usuarios.
- `categoria_id` solo si existe una categoría activa del usuario; si no, queda `null`. No crear categorías automáticamente.
- No persistir como movimientos los intents `greeting`, `out_of_scope`, `reminder`, `budget_query`, `expense_summary`.
- No asumir que una migración versionada o el snapshot local prueban el estado aplicado en Supabase; comprobar el historial con `supabase migration list` y verificar los objetos afectados en remoto.

## Gotchas específicos

- **Redis es opcional en local pero obligatorio para multi-turno**: `ConversationService` crea su propio cliente (separado del `redis_client` global de `main.py`) y ante fallo logs y devuelve estado vacío (degradación silenciosa). Los flujos multi-turno (confirmación de categoría, creación de recordatorio en pasos, rename por título duplicado) dependen de Redis.
- **Scheduler debe correr una sola vez, no por worker**: el `Dockerfile` usa `gunicorn -w 1 -k uvicorn.workers.UvicornWorker` a propósito. No agregar workers extras.
- **`load_dotenv()` va ANTES de importar submódulos en `main.py`**; mantener ese orden si se tocan imports de `app.`.
- En `main.py` hay dos ramas de dispatcher (STK-39 v2 con hint de categoría y el flujo legacy) que pueden superponerse; al tocar el webhook revisar que el `intent` no se procese dos veces.
- **Streamlit NO está en el `requirements.txt` raíz**: solo vive en `testing/requirements.txt` (que incluye `-r ../requirements.txt` + streamlit). El entorno de testing se levanta únicamente con Docker/Podman; no correr `streamlit run` local con pip.
- **`testing/streamlit_app.py` hardcodea `DATABASE_URL=sqlite:///./testing_luka.db`** (sobreescribe el `.env`). Es una DB SQLite aislada del entorno productivo (el compose la setea de nuevo en `environment`).
- **El docker-compose del testing requiere `.env` en la raíz**: usa `env_file: ../.env`; sin ese archivo el compose falla.
- **Los logos viven solo en `testing/public/`**: `_public_asset()` en `testing/components/` (chat.py y sidebar.py) resuelve `Path(__file__).resolve().parent.parent / "public"`, es decir `testing/public/`. No existe carpeta `public/` en la raíz del repo.
- **Los tests del entorno de testing viven en `testing/tests/`** (no en `tests/`).

## Verificar cambios

- Lint + tests (same as GitHub Actions en pushes y PR a `main`; `WHATSAPP_VERIFY_TOKEN` se setea con valor de test por CI):

```powershell
python -m ruff check .
python -m pytest -v
```

- Tests NO requieren red real: usan SQLite en memoria y `monkeypatch.setattr(...SessionLocal...)`; LLM, WhatsApp y Redis se mockean con `AsyncMock`/`unittest.mock`. Los tests de `ConversationService` mockean el cliente Redis.
- Para levantar local: venv Python 3.11, `pip install -r requirements.txt`, copiar `.env.example` a `.env`, `python -m uvicorn app.main:app --reload`. En local, `DATABASE_URL` por defecto `sqlite:///./luka.db`.

## Principios de Diseño y Arquitectura

- No preserves la compatibilidad con versiones anteriores. Elimina las rutas obsoletas en lugar de añadir capas de compatibilidad, soluciones alternativas (fallbacks) o migraciones.
- Elige la implementación más simple que cumpla plenamente con los requisitos actuales. Evita abstracciones especulativas, configuraciones innecesarias e indirecciones.
- Haz crecer el sistema por capas. Comienza desde la versión más pequeña que funcione de extremo a extremo y añade cada nueva capacidad sobre un producto que ya funcione. Nunca cambies un producto funcional por complejidad inconclusa.
- Mantén los componentes modulares y las responsabilidades claramente separadas.
- Prefiere librerías consolidadas y bien mantenidas cuando reduzcan la complejidad general o mejoren la confiabilidad. No vuelvas a implementar funcionalidades comunes sin una razón clara.
- Apóyate en las dependencias existentes en el proyecto antes de escribir tu propia implementación o añadir nuevos paquetes. No asumas que a una librería le falta una capacidad sin consultar antes su documentación y tipos. No añadas nuevas dependencias a menos que se te indique o que sea estrictamente necesario.
- Toma decisiones de arquitectura a largo plazo. No aceptes soluciones parche que solo funcionen por el momento y estén destinadas a ser reemplazadas más adelante.

## Referencias DeepWiki

- Overview: https://deepwiki.com/blob1618/luka/1-luka-overview
- Estructura: https://deepwiki.com/blob1618/luka/1.2-project-structure
- Arquitectura central: https://deepwiki.com/blob1618/luka/2-core-architecture
- Servicio LLM: https://deepwiki.com/blob1618/luka/2.3-llm-service-(gemini-integration)
- Servicio de finanzas: https://deepwiki.com/blob1618/luka/3.1-finance-service
- Modelos de BD: https://deepwiki.com/blob1618/luka/4.1-database-models
- Deploy: https://deepwiki.com/blob1618/luka/5-deployment
