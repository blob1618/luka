# Instrucciones para agentes de IA — Luka

Luka es un asistente financiero personal que opera por WhatsApp y ayuda a los usuarios a gestionar sus finanzas a través de lenguaje natural.

## Arquitectura y stack

- **Framework**: FastAPI (async). Python 3.11.
- **LLM**: agente de herramientas nativas en `assistant_runtime.py` y catálogo en `assistant_catalog.py`; providers `gemini`, `mistral` y `deepseek` seleccionados por `LLM_PROVIDER`. El prompt breve es `prompts/core_prompt.md` (`prompt.md` es respaldo del loader). Contratos de argumentos/resultados en módulos de capacidad; redacción financiera protegida en `llm.py` con `prompts/financial_reply_prompt.md`. No hay clasificador de intents ni normalizador alternativo.
- **Mensajería**: Meta WhatsApp Business API.
- **Base de datos**: SQLAlchemy (SQLite local, PostgreSQL/Supabase en producción).
- **Estado de conversación y caché**: Redis (`app/services/conversation.py`).
- **Tareas en segundo plano**: APScheduler (`app/scheduler.py`).
- **Deploy**: Docker + Render.
- **Documentación**: `docs/architecture.md`, `docs/features.md`, `docs/conversation-flows.md`, `docs/database.md` y `docs/development.md`.

## Organización del código

- `app/main.py`: entrypoint y webhook. **Nota: hoy es un archivo grande (~900 líneas) que todavía contiene bastante lógica de reply y de dispatcher; el ideal del repo es mover esa lógica a los services, no agregar más inline.**
- `app/api/whatsapp.py`: único cliente saliente de WhatsApp. Normaliza teléfonos argentinos `549...` -> `54...`.
- `app/services/llm.py` + `app/services/llm_providers/`: fachada LLM y providers. `LLMService` es singleton por clase (usar `reset_provider()` y `set_prompt_path()` en tests). `LLMService.render_financial_reply(kind, facts, quote_types)` redacta respuestas de patrimonio y cotizaciones: el LLM solo ubica placeholders `{{KEY}}` y los valores salen del backend.
- `app/services/finance.py`: validación y persistencia de movimientos y categorías.
- `app/services/onboarding.py`: alta/vinculación de usuarios e invitaciones.
- `app/services/reminder.py`: recordatorios (CRUD, título único, multi-turno).
- `app/services/assistant_state.py`, `assistant_operations.py` y `assistant_context.py`: workspace de operaciones y referencias con dueño, conversación, revisión y expiración. `conversation.py` conserva cliente Redis, historial visible postenvío y navegación del editor.
- `app/models/database.py`: engine, sesión y todos los modelos SQLAlchemy (un solo archivo).
- `app/scheduler.py`: jobs en background cada 5 min: recordatorios de pago y recordatorio proactivo diario (a la hora `PROACTIVE_PROMPT_HOUR`, solo dentro de la ventana de 24h, a usuarios con `proactivo_habilitado` que no registraron movimientos hoy; máximo uno por día).
- `testing/`: entorno de testing aislado. App Streamlit (`testing/streamlit_app.py`) que usa el mismo dispatcher y wrappers de entrega que el webhook, con IDs únicos y entrega sintética explícita, con sidebar de configuración (provider/modelo LLM, simular usuario registrado, phone, nombre), paneles de debug (tools y resultados, latencia, estado Redis, logs del dispatcher), simulador de usuario y reset de DB. Usa una DB SQLite aislada (`sqlite:///./testing_luka.db`). Solo se levanta con Docker/Podman (`testing/docker-compose.yml` + `testing/Dockerfile`, servicios `streamlit` en `:8501` y `redis` en `:6380`); tiene sus propios tests en `testing/tests/` y sus dependencias en `testing/requirements.txt`. Logos en `testing/public/`.

## Guías de ingeniería

- Las rutas de FastAPI van en `app/main.py`; la lógica compleja va en los services (parseo en LLM, cambios de estado en finance/reminder). No persistir nuevas dependencias en `app/main.py`.
- Todo mensaje saliente de WhatsApp pasa por `app/api/whatsapp.py`.
- Toda lógica basada en tiempo/notificaciones se orquesta desde `app/scheduler.py`.
- Los modelos/estructura de BD se actualizan en `app/models/database.py` (un solo archivo) con sesiones async correctas.

### Contrato DB MVP / Release 1

- `public.movimientos_financieros` es la entidad central para ingresos y egresos; `public.usuario` es la tabla oficial de usuarios, mapeada por `public.usuario.whatsapp_id`.
- No ejecutar SQL ni tocar Supabase directamente; todo cambio de schema se versiona primero (ver `docs/database.md` y `supabase/migrations/`).
- `public.movimientos_financieros` tiene RLS habilitado; no asumir policies de acceso público (roles `anon`/`authenticated`). ADR 0002: el dashboard se sirve por BFF (Astro/Workers). No hay frontend -> Data API de Supabase directo.

### Invariantes del registro por texto

- Flujo único: webhook → wrapper de idempotencia/entrega → dispatcher → runtime de tools → operación de dominio. Onboarding precede al agente; no hay flags por teléfono ni fallback a clasificación.
- El backend fija identidad/reloj y calcula datos; el modelo sólo aporta argumentos tipados y referencias opacas. No aceptar UUIDs canónicos ni una herramienta de commit del modelo.
- Toda mutación requiere propuesta completa entregada y confirmación posterior ligada a operación/revisión/hash. Una lectura suspende el borrador e invalida el sí/botón anterior; retomar vuelve a validar y presentar.
- `economic_effect` es explícito: `cash_flow` permite ingreso/gasto real; principal, colocación e inversión existente se rechazan. Indeterminación pide aclarar, sin default silencioso ni regex de intención.
- Usuario vinculado por `whatsapp_id`; registrar no crea usuarios. Categoría activa propia o null; crearla es operación hija confirmada y el padre requiere confirmación separada.
- Mutación financiera y receipt se guardan atómicamente; lote tiene IDs estables por item. Replay no duplica feedback. Los otros dominios conservan las garantías concretas de sus servicios.
- El único historial visible se escribe después del envío exitoso. Texto, botones y listas del simulador usan esa misma entrada con entrega sintética identificada.
- Migraciones/modelos locales no prueban el estado aplicado en producción; verificarlo sólo dentro de una tarea/autorización específica.

### Redacción financiera protegida

- El agente selecciona herramientas nativas con `prompts/core_prompt.md`. Patrimonio/cotizaciones calculan facts de backend y pueden usar `prompts/financial_reply_prompt.md` para su redacción; no se presupone un número fijo de llamadas.
- `render_financial_reply` recibe `kind` (`net_worth` o `usd_quotes`), una lista de facts `{key, label, value}` y los `quote_types` pedidos.
- Reglas del prompt (`prompts/financial_reply_prompt.md`): usar cada `key` una sola vez como `{{KEY}}`, no escribir cifras, fechas ni valores literales fuera de los tokens, no inventar datos, no mencionar tipos ausentes de `quote_types`, máximo 12 líneas (se pueden agrupar varios facts en una línea) y sin tablas. Para `net_worth` el prompt pide no mostrar compra, venta, fuente, hora de consulta ni hora de actualización.
- Validación en runtime (`_violates_financial_reply_contract` en `app/services/llm.py`): cada `{{KEY}}` debe existir en los facts y aparecer una sola vez, cada línea con placeholder debe empezar con `• `, no se aceptan llaves sueltas ni dígitos fuera de los placeholders, y no se aceptan nombres de tipos no pedidos. Para `net_worth` rechaza el texto que mencione `compra`, `venta`, `DolarAPI`, `consultad[oa]s?` o `actualizad[oa]s?`.
- Si la salida viola el contrato se reintenta una vez; ante cualquier fallo devuelve una lista determinística con los mismos facts. Los valores nunca se toman del texto generado.
- El runtime presenta facts obligatorios incluso si el modelo no copia sus refs. Debug expone tools/outcomes y métricas, sin depender de un JSON de clasificación.

## Gotchas específicos

- **Redis es obligatorio para preparar/confirmar cambios**: el workspace usa claim por dueño y falla sin autorizar escrituras si no hay estado fiable. Lecturas explícitas pueden funcionar sin memoria; historial/navigation tienen su propia degradación. No fingir entrega para habilitar una confirmación.
- **Scheduler debe correr una sola vez, no por worker**: el `Dockerfile` usa `gunicorn -w 1 -k uvicorn.workers.UvicornWorker` a propósito. No agregar workers extras.
- **`load_dotenv()` va ANTES de importar submódulos en `main.py`**; mantener ese orden si se tocan imports de `app.`.
- Texto y respuestas interactivas atraviesan wrappers comunes y las dos APIs del dispatcher. El callback acredita la propuesta completa después del envío exitoso; un followup distinto no acredita una propuesta recurrente. No añadir rutas paralelas de comandos al simulador.
- **Streamlit NO está en el `requirements.txt` raíz**: solo vive en `testing/requirements.txt` (que incluye `-r ../requirements.txt` + streamlit). El entorno de testing se levanta únicamente con Docker/Podman; no correr `streamlit run` local con pip.
- **Hot reload en testing**: `testing/docker-compose.yml` setea `PYTHONPATH=/app` y `.streamlit/config.toml` de la raíz setea `fileWatcherType="poll"` + `runOnSave=true`; guardar un módulo importado en `app/` o `testing/` rerunea el simulador solo (~1-3 s, sin reiniciar el contenedor, conserva la sesión); el rebuild queda solo para cambios de dependencias o del `testing/Dockerfile`, y `.env` o las opciones `[server]` del config requieren recrear el contenedor. Motivo del poll: inotify no cruza el bind mount Windows→VM.
- **`testing/streamlit_app.py` hardcodea `DATABASE_URL=sqlite:///./testing_luka.db`** (sobreescribe el `.env`). Es una DB SQLite aislada del entorno productivo (el compose la setea de nuevo en `environment`).
- **El docker-compose del testing requiere `.env` en la raíz**: usa `env_file: ../.env`; sin ese archivo el compose falla.
- **Los logos del entorno de testing viven en `testing/public/`**: `_public_asset()` en `testing/components/` (chat.py y sidebar.py) resuelve `Path(__file__).resolve().parent.parent / "public"`, es decir `testing/public/`. La carpeta `public/` de la raíz es la del README (`logo-luka-texto.png`, `hero-readme.jpg`) y no la usa el entorno de testing.
- **Los tests del entorno de testing viven en `testing/tests/`** (no en `tests/`).

## Verificar cambios

### Pull Requests de historias de usuario

- Trabajar cada historia de usuario en una rama asociada a su tarea de Jira y abrir un PR antes de integrarla a `main`.
- Antes de crear el PR, leer `.github/pull_request_template.md` y completar sus secciones con hechos verificables. Dejar como «Pendiente» cualquier hito o criterio que aún no se comprobó.
- El autor del PR mantiene su descripción actualizada cuando aparecen nuevas evidencias, incluso después del merge. Solo registrar un criterio como aceptado cuando la persona designada para validarlo deje su conclusión explícita, con fecha y enlace, en el PR o en Jira.
- Quien integra a `main` verifica el despliegue de Render y aporta la fecha y evidencia al autor del PR. No inferir una prueba real en WhatsApp de los resultados del simulador.

- Durante el desarrollo, ejecutar primero el ciclo rápido:

```bash
python -m pytest -q -m unit
```

- Para cambios en webhook o idempotencia, ejecutar la regresión focalizada:

```bash
python -m pytest -q tests/test_webhook.py tests/test_webhook_idempotency.py --durations=40
```

- Antes de integrar, ejecutar el gate completo:

```bash
ruff check .
python -m pytest -v
```

- Los tests no requieren red real. `tests/conftest.py` provee Redis en memoria y
  bloquea conexiones accidentales; Supabase, Meta y los proveedores LLM también deben
  permanecer aislados.
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
