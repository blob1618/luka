# Instrucciones para agentes de IA — Luka

Bienvenido al proyecto Luka. Luka es un asistente financiero personal que opera por WhatsApp y ayuda a los usuarios a gestionar sus finanzas a través de lenguaje natural.

## Arquitectura y stack

- **Framework**: FastAPI (arquitectura async)
- **IA/LLM**: Google Gemini (transforma lenguaje natural en datos financieros estructurados)
- **Mensajería**: Meta WhatsApp Business API
- **Base de datos**: SQLAlchemy (SQLite para desarrollo local, PostgreSQL/Supabase en producción)
- **Tareas en segundo plano**: APScheduler (gestiona recordatorios y jobs en background)
- **Deploy**: Contenedor Docker desplegado en Render

## Organización del código

- `app/main.py`: Entrypoint de la aplicación, verificación del webhook y capa de ingesta de mensajes (`/webhook`).
- `app/api/whatsapp.py`: Cliente de salida para la API de WhatsApp.
- `app/services/llm.py`: Interacción con Gemini para extraer datos estructurados (gastos, presupuestos, etc.) desde texto en lenguaje natural.
- `app/services/finance.py`: Lógica de negocio central para gestionar usuarios, transacciones y presupuestos.
- `app/models/`: Modelos de base de datos (SQLAlchemy) y esquemas (`database.py`).
- `app/scheduler.py`: Procesos en segundo plano como recordatorios financieros periódicos.

## Guías de ingeniería

### 1. Cambios de archivos
- Las rutas de FastAPI van en `app/main.py`.
- La lógica compleja no va en los controllers: el parseo va en `app/services/llm.py` y los cambios de estado en `app/services/finance.py`.
- Actualizar las definiciones de SQLAlchemy en `app/models/` cuando se altere la estructura de la base de datos y asegurar sesiones async correctas.

### 2. Interacción con servicios
- Todos los mensajes salientes de WhatsApp deben pasar por `app/api/whatsapp.py`.
- Cualquier feature NLP/LLM externa debe integrarse a través del wrapper de `app/services/llm.py` para mantener modularidad.

### 3. Datos y persistencia
- Usar el ORM SQLAlchemy para interactuar con la base de datos.
- Mantener compatibilidad con SQLite local y PostgreSQL/Supabase en producción.

### 4. Tareas en segundo plano
- Toda lógica basada en tiempo, notificaciones o procesamiento batch debe encapsularse y orquestarse desde `app/scheduler.py`.

### 5. Deploy
- La app usa `Dockerfile` y `render.yaml` para hosting en contenedor. Manejar correctamente el `lifespan` de la app para iniciar y detener servicios en background limpiamente.

## Referencias DeepWiki
Para porciones complejas de arquitectura, consultar estas referencias:
- Overview: https://deepwiki.com/blob1618/luka/1-luka-overview
- Estructura del proyecto: https://deepwiki.com/blob1618/luka/1.2-project-structure
- Arquitectura central: https://deepwiki.com/blob1618/luka/2-core-architecture
- Servicio LLM: https://deepwiki.com/blob1618/luka/2.3-llm-service-(gemini-integration)
- Servicio de finanzas: https://deepwiki.com/blob1618/luka/3.1-finance-service
- Modelos de base de datos: https://deepwiki.com/blob1618/luka/4.1-database-models
- Deploy: https://deepwiki.com/blob1618/luka/5-deployment
