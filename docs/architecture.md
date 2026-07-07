# Architecture

Resumen minimo de arquitectura y flujo de informacion del MVP.

## Fuentes usadas

- `Flujo de datos y Script DB.pdf`
- `flujo de mensajes, tecnologias y framework.pdf`

## Componentes

- Meta WhatsApp Cloud API: canal de entrada y salida de mensajes.
- FastAPI backend: recibe webhooks, valida reglas y orquesta la logica.
- LLM provider: Gemini o Mistral para interpretar mensajes.
- Supabase/PostgreSQL: base principal.
- Redis: opcional/recomendado para rate limiting, deduplicacion y cache efimera.
- Render: hosting y deploy automatico desde `main`.

## Flujo de informacion

1. Usuario envia mensaje por WhatsApp.
2. Meta envia evento al webhook del backend.
3. Backend identifica el numero de telefono.
4. Backend busca usuario en la base.
5. Si el usuario no existe, bloquea la operacion y registra evento de acceso denegado.
6. Si el usuario existe, valida consentimiento vigente.
7. Backend procesa la accion.
8. Backend genera evento.
9. Backend actualiza la proyeccion/estado actual.
10. Backend responde por WhatsApp.

## Regla critica

No hay interaccion valida sin usuario registrado y consentimiento vigente.

Esta regla pertenece al diseno objetivo del MVP. El codigo actual todavia no implementa validacion de usuario registrado ni consentimiento antes de procesar mensajes de WhatsApp.

## Frontend

El repo actual no tiene frontend implementado.

Los documentos de arquitectura mencionan una posible web/dashboard. Para ese caso se evaluo Jinja2 + HTMX como opcion liviana integrada a FastAPI, pero todavia no forma parte del codigo actual.

## Pruebas reales

Las pruebas reales de integracion dependen de:

- Numero de WhatsApp configurado en Meta.
- Webhook publico desplegado.
- Base de datos usada por el entorno.

Por eso el equipo esta usando `main` como rama de despliegue/prueba real por ahora. Un entorno separado de pruebas seria posible, pero requiere configurar otro numero, otro webhook y otra base de datos.

Deploy actual:

```text
https://luka-f2nb.onrender.com
```
