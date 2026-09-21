# Entorno de testing (Streamlit)

Luka es un asistente financiero por WhatsApp. El entorno de testing es una app web (Streamlit) que simula el flujo de WhatsApp de Luka contra el mismo backend del repositorio, sin necesidad de la API de Meta ni de un número real de WhatsApp. Cada mensaje que se escribe en el chat se procesa con el flujo completo del dispatcher de producción (`app/services/dispatcher.py`), tal como lo haría el webhook, y la respuesta se muestra como si llegara por WhatsApp.

El entorno vive dentro de `testing/` y se levanta únicamente con Docker o Podman. Streamlit no está en el `requirements.txt` de la raíz, solo en `testing/requirements.txt`, así que `streamlit run` local con pip ya no es una opción soportada.

## Funcionalidades

- Galería de gráficos: página lateral con muestras reproducibles, vista de 360 px
  y descarga PNG; funciona sin llamadas a LLM ni consultas a la base.

- Chat que simula WhatsApp: entrada de mensajes, historial de conversación, avatar de Luka y respuesta procesada por el flujo completo del dispatcher (modo webhook). No se envía ningún mensaje real por WhatsApp.
- Configuración desde la sidebar:
  - Provider LLM (Gemini o Mistral, según los providers registrados en el factory).
  - Archivo de prompt (`prompts/core_prompt.md` por defecto; el selector lista los `.md` del repo en `prompts/` y los de `testing/prompts/`).
  - Modelo (lista por provider, el primer item es el default).
  - Sesiones (ver abajo).
- Sesiones: cada sesión simula un número distinto y tiene su propio teléfono y su propia conversación visible, sin mezclar mensajes entre números. Desde la sidebar se puede:
  - Crear una nueva sesión con etiqueta, teléfono, nombre y "Ya registrado" (el teléfono no puede repetirse).
  - Cambiar la sesión activa con el selector, sin tocar los mensajes de las demás.
  - Vincular o desvincular la sesión activa (checkbox "Vinculado"): al vincular se crea el usuario de test en la base y al desvincular se borran sus datos (via `UserSimulator`).
  - Eliminar la sesión activa (solo si hay más de una); no borra el usuario de la base.
- Modos de debug, activables por checkboxes, visibles en un panel desplegable por mensaje:
  - JSON crudo de la respuesta del LLM.
  - Latencia de procesamiento en milisegundos.
  - Estado de Redis (estado multi-turno de la conversación).
  - Memoria conversacional: últimos turnos guardados en Redis y su TTL.
  - Servicio invocado / logs del dispatcher.
- Reset de base de datos: borra movimientos, categorías y recordatorios del usuario de la sesión activa (el usuario se conserva).
- Exportar la conversación de la sesión activa como JSON o texto plano, y copiarla al portapapeles con o sin datos de debug. También se pueden exportar todas las sesiones (JSON y texto, más copiar con debug) para comparar el aislamiento de la memoria Redis entre números.
- Base de datos SQLite aislada en un volumen del entorno (`testing_data`): no toca la base local (`luka.db`) ni Supabase.

> Las sesiones viven en el estado de la app de Streamlit: un refresh las pierde (la app vuelve a crear "Sesión 1"), aunque los usuarios y movimientos quedan en SQLite y la memoria conversacional en Redis. El flujo de usuario no registrado se prueba hasta el link de registro: la respuesta arma la URL con `ONBOARDING_REGISTRATION_URL`, pero `/registro` no existe en este repositorio, así que no hay pantalla de alta detrás del link.

## Requisitos iniciales

1. Docker o Podman instalado y corriendo. Es la única forma soportada de levantar este entorno.
2. Copiar `.env.example` a `.env` en la raíz del repositorio:

   PowerShell (Windows):

   ```powershell
   Copy-Item .env.example .env
   ```

   Bash (Linux/macOS):

   ```bash
   cp .env.example .env
   ```

3. Completar en `.env` la API key del LLM elegido según `LLM_PROVIDER`:
   - `LLM_PROVIDER=gemini` (default): setear `GEMINI_API_KEY`.
   - `LLM_PROVIDER=mistral`: setear `MISTRAL_API_KEY` y cambiar `LLM_PROVIDER=mistral`.

El `docker-compose.yml` usa `env_file: ../.env`, así que sin el `.env` en la raíz el compose falla. `TESTING_DATABASE_URL` y `REDIS_URL` se definen en el propio compose, por lo que no hace falta tocarlas.

## Configuración y uso

Todos los comandos se ejecutan desde la raíz del repositorio. Los comandos de Compose son idénticos en Windows (PowerShell) y Linux/macOS (bash); la única diferencia entre plataformas es la copia inicial de `.env` (ver Requisitos iniciales).

### 1. Construir y Levantar

```bash
# Construir
docker compose -f testing/docker-compose.yml build

# Levantar
docker compose -f testing/docker-compose.yml up -d
```

El primer `build` puede tardar: descarga la imagen `python:3.11-slim` e instala las dependencias del proyecto + Streamlit.

Abrir la app en:

```text
http://localhost:8501
```

### 2. Ver Logs

```bash
# Todo
docker compose -f testing/docker-compose.yml logs -f

# Solo Streamlit
docker compose -f testing/docker-compose.yml logs -f streamlit
```

### 3. Detener

```bash
docker compose -f testing/docker-compose.yml down
```

> Para usar con "Podman" simplemente cambia `docker` por `podman` en los comandos anteriores.

### 4. Correr los Tests

Los tests del entorno viven en `testing/tests/`. Dentro del contenedor se corren con:

```bash
docker compose -f testing/docker-compose.yml exec streamlit python -m pytest -v testing/tests
```

El test de integración de la memoria conversacional (`testing/tests/test_conversation_memory_redis.py`) necesita un Redis real y también se puede correr desde el host contra el Redis del compose, con el venv del repo en la raíz:

```bash
docker compose -f testing/docker-compose.yml up -d redis
REDIS_URL=redis://localhost:6380 .venv/bin/python -m pytest -v testing/tests/test_conversation_memory_redis.py
docker compose -f testing/docker-compose.yml stop redis
```

En Windows, reemplazá `.venv/bin/python` por `python`, o corré el comando dentro del contenedor con `docker compose -f testing/docker-compose.yml run --rm streamlit python -m pytest -v testing/tests`.

Si Redis no responde, el test se saltea con `pytest.skip` en lugar de fallar.

## Características

- Base de datos aislada: la app configura `DATABASE_URL` desde `TESTING_DATABASE_URL`; fuera del contenedor su valor por defecto es `sqlite:///./testing_luka.db`. El compose lo dirige a `/data/testing_luka.db`, dentro del volumen administrado `testing_data`, para evitar conflictos de permisos con el montaje del repositorio. Es independiente de `luka.db` y de Supabase.
- Volumen en vivo: el compose monta todo el repositorio en `/app`, por lo que los cambios de código se reflejan sin reconstruir la imagen. El `COPY . /app/` del Dockerfile queda cubierto por el volumen en runtime.
- Redis: el compose levanta `redis:7-alpine` y lo expone en el host como `localhost:6380` (mapea al puerto interno 6379). El estado multi-turno de la conversación (confirmación de categoría, recordatorios en pasos, etc.) vive ahí y se puede inspeccionar desde el panel de debug.
- Mismo código de backend: la app importa `app/*` directamente. `WebhookModeService` ejecuta `process_incoming_message` del dispatcher con el teléfono simulado, sin HTTP y sin enviar mensajes a la API de WhatsApp; la confirmación de un registro ocurre recién después de la persistencia en base, igual que en producción.
- Provider y prompt en caliente: cambiar provider o archivo de prompt en la sidebar resetea el `LLMService` y aplica el nuevo modelo/prompt en el siguiente mensaje.
- Previsualización de gráficos: cuando el dispatcher devuelve un `WhatsAppImage`, el chat
  muestra el PNG en memoria debajo del mensaje. Es una simulación visual: no carga medios
  ni envía mensajes a Meta. Los bytes no se incluyen en las exportaciones de chat.
- El entorno de testing tiene sus propios tests en `testing/tests/`. Están fuera de la suite por defecto (el `pytest.ini` de la raíz solo incluye `tests/`) y se corren dentro del entorno Docker, donde está instalado Streamlit: `python -m pytest -v testing/tests`.
