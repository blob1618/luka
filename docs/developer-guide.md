# Guía para desarrolladores

Información técnica del backend de LUKA orientada al equipo de desarrollo.

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

## Presupuestos por categoría (HU-PRE-01 / STK-47)

- `LimitService` mantiene un único límite por usuario, categoría, inicio de período y moneda.
- `BudgetService` calcula el gasto consumido desde `movimientos_financieros`; no persiste saldos derivados.
- Solo consumen presupuesto los egresos con categoría, moneda coincidente y fecha dentro del período.
- `budget_query` consulta una categoría o todos los límites del período y la respuesta se construye en backend.
- Después de registrar un egreso o cambiar su categoría, el dispatcher evalúa el presupuesto y agrega una alerta si quedó excedido.
- Una categoría canónica inexistente requiere confirmación multi-turno antes de que el límite pueda crearla.
- La migración `005_presupuesto_control_gasto.sql` debe aplicarse y verificarse operativamente antes de desplegar código que dependa de ella en un entorno compartido.

## Mapa del repositorio

- `app/main.py`: app FastAPI, endpoint de health, verificación del webhook de WhatsApp e ingesta de mensajes.
- `app/api/whatsapp.py`: cliente de salida hacia la API de WhatsApp.
- `app/services/llm.py`: fachada LLM que interpreta mensajes y normaliza el tipo de movimiento.
- `app/services/llm_providers/`: implementaciones de los providers Gemini y Mistral.
- `app/services/finance.py`: validación y persistencia de movimientos financieros y otras reglas de negocio.
- `app/services/limit.py`: CRUD y validación de límites mensuales por categoría.
- `app/services/budget.py`: cálculo de consumo, disponibilidad, porcentaje y exceso.
- `app/services/telemetry.py`: medición por mensaje de latencia total y fases del pipeline.
- `app/models/database.py`: engine, sesión y modelos SQLAlchemy.
- `tests/`: tests del backend.
- `testing/`: entorno de testing de WhatsApp en Streamlit contra el mismo backend, se inicia solo con Docker o Podman (ver `testing/README.md`).
- `docs/database.md`: estado actual de la base de datos, esquema objetivo del MVP y decisiones pendientes.
- `docs/architecture.md`: flujo de información y resumen de arquitectura del MVP.
- `SUPABASE_SETUP.md`: guía de configuración de la base de datos.
- `RENDER_DEPLOYMENT.md`: notas de despliegue en Render.
- `AGENTS.md`: notas de ingeniería para agentes de IA.

## Variables de entorno

Copiar `.env.example` a `.env` y completar solo lo que tu tarea necesite.

| Variable                | Requerida                                    | Notas                                                                                                        |
| ----------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `WHATSAPP_VERIFY_TOKEN` | Sí para verificación del webhook             | Token configurado en Meta y verificado por `GET /webhook`.                                                   |
| `WHATSAPP_API_TOKEN`    | Sí para enviar respuestas por WhatsApp       | Token bearer de la API de Meta.                                                                              |
| `WHATSAPP_PHONE_ID`     | Sí para enviar respuestas por WhatsApp       | ID del número de teléfono de WhatsApp en Meta.                                                               |
| `LLM_PROVIDER`          | Opcional                                     | `gemini` por defecto. También soporta `mistral`.                                                             |
| `GEMINI_API_KEY`        | Requerido si `LLM_PROVIDER=gemini`           | API key de Gemini.                                                                                           |
| `GEMINI_MODEL`          | Opcional                                     | Por defecto en `.env.example` es `gemini-2.0-flash`.                                                         |
| `MISTRAL_API_KEY`       | Requerido si `LLM_PROVIDER=mistral`          | API key de Mistral.                                                                                          |
| `MISTRAL_MODEL`         | Opcional                                     | Por defecto en `.env.example` es `mistral-small-latest`.                                                     |
| `DATABASE_URL`          | Opcional en local, requerido en producción   | Por defecto `sqlite:///./luka.db`. Usar Supabase/PostgreSQL para entornos compartidos.                       |
| `REDIS_URL`             | Opcional en local, recomendado en producción | Por defecto `redis://localhost:6379`. La app loguea un error si Redis no está disponible pero igual arranca. |
| `PROACTIVE_PROMPT_HOUR` | Opcional                                     | Hora local (0-23, America/Argentina/Buenos_Aires) del recordatorio proactivo diario. Sin valor o inválida, el aviso queda apagado. |

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

El repositorio usa Supabase CLI y migraciones timestamped en `supabase/migrations/`. No usar `Base.metadata.create_all()` para actualizar una base compartida. Crear cada cambio con `supabase migration new <nombre>`, validarlo con `supabase db reset` y publicarlo mediante el flujo de GitHub; ver `docs/database.md`.

`database/reference/schema_supabase_inicial_legacy.sql` es un snapshot histórico no ejecutable: no representa el estado actual y no debe usarse para reconstruir ni reparar Supabase. El historial efectivo se comprueba con `supabase migration list`; una migración local por sí sola no demuestra que haya sido desplegada.

## Limitaciones conocidas y trabajo relacionado

- La deduplicación por `whatsapp_message_id` evita insertar dos filas. Cuando Meta reenvía un mensaje ya persistido, el reintento se procesa en silencio: la respuesta visible se suprime porque la confirmación ya se envió en la primera entrega.
- Los índices productivos de usuarios y movimientos, incluido el índice único parcial de `whatsapp_message_id`, deben verificarse en Supabase. El contrato versionado puede contenerlos sin que eso pruebe su aplicación remota.
- `POST /webhook` confirma la recepción inmediatamente y procesa mensajes de texto e interactivos con `BackgroundTasks`. Este mecanismo corre dentro del mismo proceso y no es durable: si el worker cae después del HTTP 200, el trabajo pendiente no se recupera. Una garantía de entrega requerirá una cola durable u outbox en una tarea posterior.
- Al comenzar el procesamiento en segundo plano se intenta enviar la reacción `⏳` al mensaje original. Es una señal de mejor esfuerzo, con timeout HTTP de 3 segundos; su falla queda registrada y no interrumpe el procesamiento financiero.
- Cada mensaje genera logs `[BACKGROUND_MESSAGE]` y `[METRICS]` con `message_id`, estado, latencia total y las fases presentes: `reaction_ms`, `redis_ms`, `llm_ms`, `db_ms` y `reply_ms`. No registrar cuerpo del mensaje, teléfono completo, montos ni otras credenciales o datos personales.
- Las métricas actuales viven en logs. Todavía no existe almacenamiento histórico, dashboard ni alertas automáticas para percentiles de latencia o tasas de error.
- Quedan pendientes rate limiting, protección frente al abuso de tokens y optimizaciones para evitar llamadas innecesarias al LLM: validar usuarios y duplicados antes del LLM y usar un pre-router para saludos o mensajes fuera de alcance.

## Verificar cambios

GitHub Actions corre verificaciones automáticas en pushes y en Pull Requests a `main`.

En local, el entorno aislado utiliza `.codex\dev.py`. Existen tres niveles de ejecución según el ciclo de trabajo:

### 1. Ciclo rápido (unitario)

Ejecuta únicamente tests unitarios con dependencias externas aisladas (sin tocar red ni base de datos):

```powershell
python -I .codex\dev.py test -q -m unit
```

### 2. Regresión focalizada (webhook e idempotencia)

Verifica el contrato del webhook, idempotencia y procesamiento desacoplado en segundo plano con dobles en memoria:

```powershell
python -I .codex\dev.py test -q tests/test_webhook.py tests/test_webhook_idempotency.py --durations=40
```

### 3. Suite completa y verificación estricta

Corre linter (Ruff) y la suite completa de tests (unitarios + integración con SQLite aislado):

```powershell
python -I .codex\dev.py lint
python -I .codex\dev.py verify
```

### Aislamiento de infraestructura

- Las pruebas locales no requieren Redis, Supabase, Meta ni Gemini reales.
- `tests/conftest.py` proporciona un `FakeRedis` en memoria determinista y un guardia de sockets que intercepta conexiones accidentales de red/Redis fallando de inmediato con un mensaje diagnóstico.
- `ConversationService._client` y `_loop_id` se limpian automáticamente entre tests para evitar contaminación entre event loops.

### Deuda técnica y deprecaciones observadas (seguimiento)

- `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` en `fastapi.testclient`.
- `DeprecationWarning: Call to deprecated setex. (Use 'set' instead.)` en `app/services/conversation.py` (migrar llamadas `setex(...)` a `set(..., ex=...)` en una tarea posterior).

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
