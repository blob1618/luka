[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/blob1618/luka)

# LUKA

LUKA es un asistente financiero por WhatsApp que ayuda a los usuarios a registrar gastos desde mensajes en lenguaje natural. El punto de entrada del producto es WhatsApp a través de la Meta WhatsApp Business API.

Por ahora el bot puede recibir mensajes de texto, pedirle a un proveedor LLM que extraiga los datos del gasto, y responder de vuelta por WhatsApp. Las notas de voz e imágenes de comprobantes están preparadas en el código pero todavía no están implementadas.

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
- `app/services/llm.py`: fachada LLM usada por el flujo del webhook.
- `app/services/llm_providers/`: implementaciones de los providers Gemini y Mistral.
- `app/services/finance.py`: helpers de lógica financiera y de negocio.
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

Para crear las tablas actuales desde los modelos SQLAlchemy:

```powershell
python -c "from app.models.database import engine, Base; Base.metadata.create_all(bind=engine)"
```

No hay herramienta de migraciones configurada todavía. Si un cambio toca los modelos, coordinar la actualización del esquema con el equipo y actualizar `docs/database.md`.

## Verificar cambios

GitHub Actions corre verificaciones automáticas. El workflow actual corre en pushes a `main` y en Pull Requests a `main`.

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
