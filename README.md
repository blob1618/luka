[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/blob1618/luka)

# LUKA

LUKA is a WhatsApp financial assistant that helps users register expenses from natural language messages. The product entry point is WhatsApp through the Meta WhatsApp Business API.

Today the bot can receive text messages, ask an LLM provider to extract expense data, and answer back through WhatsApp. Voice notes and receipt images are planned in code but are not implemented yet.

## Stack

- Backend: Python 3.11, FastAPI, Uvicorn/Gunicorn.
- Messaging: Meta WhatsApp Business API.
- AI: Gemini or Mistral, selected with `LLM_PROVIDER`.
- Database: SQLAlchemy ORM. Local dev uses SQLite by default. Production should use PostgreSQL, normally Supabase.
- Background jobs/cache: APScheduler and Redis.
- Deploy: Docker and Render.
- Tests/quality: Pytest and Ruff.
- Frontend: there is no web frontend in this repository yet. The user interface is WhatsApp.

## Repository map

- `app/main.py`: FastAPI app, health endpoint, WhatsApp webhook verification and message ingestion.
- `app/api/whatsapp.py`: outgoing WhatsApp API client.
- `app/services/llm.py`: LLM facade used by the webhook flow.
- `app/services/llm_providers/`: Gemini and Mistral provider implementations.
- `app/services/finance.py`: finance/business logic helpers.
- `app/models/database.py`: SQLAlchemy engine, session and models.
- `tests/`: backend tests.
- `docs/database.md`: current database state, target MVP schema, and pending decisions.
- `docs/architecture.md`: MVP information flow and architecture summary.
- `SUPABASE_SETUP.md`: database setup guide.
- `RENDER_DEPLOYMENT.md`: deployment notes for Render.
- `AGENTS.md`: engineering notes for AI/code agents.

## Local backend setup

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --reload
```

The API starts at `http://127.0.0.1:8000`.

Basic check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/
```

Expected response:

```json
{"message":"Luka API is running"}
```

For macOS/Linux, use `python3 -m venv .venv`, `source .venv/bin/activate`, and `cp .env.example .env`.

## Frontend setup

There is no frontend app to run right now. The only product interface is WhatsApp, so local work happens through the FastAPI backend and the `/webhook` endpoint.

To test real WhatsApp events, Meta needs a public HTTPS URL. Use Render or a local tunnel, then configure Meta to call:

```text
https://<public-url>/webhook
```

## Environment variables

Copy `.env.example` to `.env` and fill only what your task needs.

| Variable | Required | Notes |
| --- | --- | --- |
| `WHATSAPP_VERIFY_TOKEN` | Yes for webhook verification | Token configured in Meta and checked by `GET /webhook`. |
| `WHATSAPP_API_TOKEN` | Yes to send WhatsApp replies | Meta API bearer token. |
| `WHATSAPP_PHONE_ID` | Yes to send WhatsApp replies | WhatsApp phone number ID from Meta. |
| `LLM_PROVIDER` | Optional | `gemini` by default. Also supports `mistral`. |
| `GEMINI_API_KEY` | Required if `LLM_PROVIDER=gemini` | Gemini API key. |
| `GEMINI_MODEL` | Optional | Defaults in `.env.example` to `gemini-2.0-flash`. |
| `MISTRAL_API_KEY` | Required if `LLM_PROVIDER=mistral` | Mistral API key. |
| `MISTRAL_MODEL` | Optional | Defaults in `.env.example` to `mistral-small-latest`. |
| `DATABASE_URL` | Optional locally, required in production | Defaults to `sqlite:///./luka.db`. Use Supabase/PostgreSQL for shared environments. |
| `REDIS_URL` | Optional locally, recommended in production | Defaults to `redis://localhost:6379`. The app logs an error if Redis is unavailable but continues starting. |

Never commit `.env` or real secrets.

## Database

Local default database:

```text
sqlite:///./luka.db
```

Production/shared database:

```text
postgresql://...
```

Use Supabase unless the team decides otherwise. Setup details are in `SUPABASE_SETUP.md`.

To create the current tables from the SQLAlchemy models:

```powershell
python -c "from app.models.database import engine, Base; Base.metadata.create_all(bind=engine)"
```

There is no migration tool configured yet. If a change touches models, coordinate the schema update with the team and update `docs/database.md`.

## Verify changes

GitHub Actions runs automated checks. The current workflow runs on pushes to `main` and Pull Requests to `main`.

If the team is not using Pull Requests, the automatic check happens when the change reaches `main`. When possible, run the same checks locally before integrating changes:

```powershell
python -m ruff check .
python -m pytest -v
```

Real WhatsApp testing happens after `main` is deployed by Render, because the project depends on the Meta phone number, webhook URL, and shared database.

## Jira and branch flow

The shared deploy branch is `main`.

Minimum workflow:

1. Take a Jira ticket.
2. Use the Jira extension in VS Code to create or take the ticket branch.
3. Develop the change.
4. Push to GitHub.
5. Let GitHub Actions run when applicable.
6. Integrate to `main` according to the team's current agreement.
7. Render deploys `main` automatically.
8. Test the real flow in WhatsApp.
9. If something fails, check Render logs and fix it.
10. Confirm Jira moved the ticket status. If it did not, update it manually.

Pull Requests are not mandatory in the current workflow.

More detail is in `CONTRIBUTING.md`.

## Project links

- GitHub: https://github.com/blob1618/luka
- Render deploy: https://luka-f2nb.onrender.com
- DeepWiki overview: https://deepwiki.com/blob1618/luka/1-luka-overview
- DeepWiki architecture: https://deepwiki.com/blob1618/luka/2-core-architecture
- MVP architecture: `docs/architecture.md`
- Database setup: `SUPABASE_SETUP.md`
- Database notes: `docs/database.md`
- Render deployment: `RENDER_DEPLOYMENT.md`
- Jira: pending team URL.
- Confluence: pending team URL.

## Deploy / WhatsApp testing

Render can deploy this repo with the included `Dockerfile` and `render.yaml`.

Current Render URL:

```text
https://luka-f2nb.onrender.com
```

Quick Render setup:

1. Create a Render Web Service.
2. Connect this GitHub repository.
3. Use Docker.
4. Add environment variables from `.env`.
5. Deploy.
6. Configure Meta's webhook callback URL as `https://luka-f2nb.onrender.com/webhook` for the current deploy, or `https://<render-app>.onrender.com/webhook` for a new service.

See `RENDER_DEPLOYMENT.md` for more detail.
