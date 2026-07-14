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
- `app/services/llm.py`: Interacción con el proveedor LLM para interpretar mensajes y normalizar movimientos financieros desde texto en lenguaje natural.
- `app/services/finance.py`: Lógica de negocio central para validar y persistir ingresos y egresos, además de otros helpers financieros.
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
- Para Release 1, el acceso a datos financieros debe ser mediado por backend. No implementar frontend -> Supabase directo para `movimientos_financieros` salvo nueva ADR.
- `public.movimientos_financieros` tiene RLS habilitado; no asumir que existen policies para acceso público desde roles `anon` o `authenticated`.

### 3.1 Contrato DB MVP Release 1
- Usar `public.movimientos_financieros` como entidad central del MVP para ingresos y egresos.
- Usar `public.usuario` como tabla oficial de usuarios; el contrato requiere `whatsapp_id` para mapear WhatsApp con `usuario.id`.
- No usar tablas legacy para nuevas features: `public.usuarios`, `public.presupuestos`, `public.limites_gasto`, `public.versiones_consentimiento`, `public.consentimientos_usuario`, `public.gastos`. La tabla oficial de recordatorios es `public.recordatorio`.
- No ejecutar SQL ni tocar Supabase directamente desde tareas de agentes; todo cambio de schema debe versionarse primero en GitHub.
- Mantener la lógica de negocio fuera de `app/main.py`; el parseo va en servicios LLM y los cambios de estado en servicios de finanzas.

### 3.2 Invariantes del registro por texto (STK-35)
- El flujo implementado es WhatsApp webhook -> `LLMService` -> `FinanceService` -> `public.movimientos_financieros` -> respuesta.
- Para movimientos registrables, `intent="expense"` se conserva por compatibilidad y `movement_type` define si es `ingreso` o `egreso`.
- Confirmar el registro al usuario únicamente después de una persistencia exitosa. Nunca confiar en `reply_text` del LLM para confirmar una escritura.
- Exigir un usuario previamente registrado y vinculado por `public.usuario.whatsapp_id`; STK-35 no crea usuarios ni implementa register, login o vinculación inicial.
- Asociar `categoria_id` solo si existe una categoría activa del usuario. No crear categorías automáticamente; sin coincidencia debe quedar `null`.
- No persistir como movimientos los intents `greeting`, `out_of_scope`, `reminder`, `budget_query` o `expense_summary`.
- Tratar alta/vinculación de usuarios, categorías default, Magic Link/STK-54 y consulta de movimientos/STK-128 como trabajo separado, no como comportamiento implementado por STK-35.
- No asumir que una migración versionada o el snapshot local demuestran el estado aplicado en Supabase; los índices productivos deben verificarse por el proceso operativo correspondiente.

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
