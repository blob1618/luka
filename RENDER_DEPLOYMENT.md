# Render Deployment Guide

This project can be deployed to Render using the included `Dockerfile` or by configuring a Python web service.

Quick steps (Docker):

1. Create a Render account and a new **Web Service**.
2. Connect your Git repository and pick the branch to deploy.
3. Choose **Docker** as the Environment — Render will build using the repo `Dockerfile`.
4. Add required environment variables in Render: `WHATSAPP_VERIFY_TOKEN`, `DATABASE_URL`, and other API keys.
5. Deploy. Render exposes a `$PORT` environment variable; the `Dockerfile` starts the app with Gunicorn bound to that port.

Quick steps (no Docker):

1. Create a Render Web Service and choose the **Python** environment.
2. Set the build command to:

```
pip install -r requirements.txt
```

3. Set the start command to:

```
gunicorn -w 4 -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:$PORT
```

Notes:
- Render provides a persistent server, so background schedulers (like APScheduler used here) will run as expected.
- Ensure `DATABASE_URL` points to a production-ready Postgres instance (Supabase, Heroku Postgres, etc.).
- If you prefer Render's `render.yaml` infrastructure config, see `render.yaml` in the repo for a template.
