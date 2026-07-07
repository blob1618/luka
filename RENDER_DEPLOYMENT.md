# Render Deployment Guide

Current deploy:

```text
https://luka-f2nb.onrender.com
```

Health check:

```text
GET https://luka-f2nb.onrender.com/
```

Expected response:

```json
{"message":"Luka API is running"}
```

Meta webhook URL:

```text
https://luka-f2nb.onrender.com/webhook
```

## Render setup

The repo is prepared for Render using Docker:

- `Dockerfile`
- `render.yaml`

Render should build the Docker image and run:

```bash
gunicorn -w 4 -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${PORT:-8000}
```

## Required environment variables

Set these in the Render dashboard. Do not commit real values.

- `WHATSAPP_VERIFY_TOKEN`
- `WHATSAPP_API_TOKEN`
- `WHATSAPP_PHONE_ID`
- `LLM_PROVIDER`
- `GEMINI_API_KEY` if `LLM_PROVIDER=gemini`
- `GEMINI_MODEL`
- `MISTRAL_API_KEY` if `LLM_PROVIDER=mistral`
- `MISTRAL_MODEL`
- `DATABASE_URL`
- `REDIS_URL`

`render.yaml` is a service template. The actual secret values must be configured in Render.

## Notes

- `main` is the deploy/test branch for the current team workflow.
- Render deploys after changes reach `main`, according to the service configuration.
- Real WhatsApp testing depends on the Meta phone number, webhook URL, and shared database.
- If WhatsApp behavior fails but the health check works, inspect Render logs first.
