# Guía de deploy en Render

Deploy actual:

```text
https://luka-f2nb.onrender.com
```

Health check:

```text
GET https://luka-f2nb.onrender.com/
```

Respuesta esperada:

```json
{"message":"Luka API is running"}
```

URL del webhook de Meta:

```text
https://luka-f2nb.onrender.com/webhook
```

## Configuración en Render

El repo está preparado para Render usando Docker:

- `Dockerfile`
- `render.yaml`

Render construye la imagen Docker y ejecuta:

```bash
gunicorn -w 4 -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${PORT:-8000}
```

## Variables de entorno requeridas

Configurar estas en el dashboard de Render. No subir valores reales al repo.

- `WHATSAPP_VERIFY_TOKEN`
- `WHATSAPP_API_TOKEN`
- `WHATSAPP_PHONE_ID`
- `LLM_PROVIDER`
- `GEMINI_API_KEY` si `LLM_PROVIDER=gemini`
- `GEMINI_MODEL`
- `MISTRAL_API_KEY` si `LLM_PROVIDER=mistral`
- `MISTRAL_MODEL`
- `DATABASE_URL`
- `REDIS_URL`

`render.yaml` es una plantilla del servicio. Los valores secretos reales deben configurarse en Render.

## Notas

- `main` es la rama de despliegue y prueba para el flujo actual del equipo.
- Render despliega cuando los cambios llegan a `main`, según la configuración del servicio.
- Las pruebas reales de WhatsApp dependen del número de teléfono de Meta, la URL del webhook y la base de datos compartida.
- Si el comportamiento de WhatsApp falla pero el health check funciona, revisar los logs de Render primero.
