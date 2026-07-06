# Luka AI Agent Instructions

Welcome to the Luka project! Luka is a WhatsApp-based financial assistant designed to help users manage their personal finances through natural language interaction.

## High-Level Architecture and Stack

- **Framework**: FastAPI (async architecture)
- **AI/LLM**: Google Gemini (transforms natural language to structured financial data)
- **Messaging**: Meta WhatsApp Business API
- **Database**: SQLAlchemy (SQLite for local dev, PostgreSQL/Supabase for production)
- **Background Tasks**: APScheduler (manages reminders and background jobs)
- **Deployment**: Containerized via Docker, deployed to Render

## Codebase Organization

- `app/main.py`: Application entrypoint, webhook verification, and message ingestion layer (`/webhook`).
- `app/api/whatsapp.py`: WhatsApp API client for sending messages.
- `app/services/llm.py`: Interaction with Gemini to extract structured data (e.g., expenses, budgets) from natural language text.
- `app/services/finance.py`: Core business logic for managing users, transactions, and budgets.
- `app/models/`: Contains database models (SQLAlchemy) and schemas (`database.py`).
- `app/scheduler.py`: Handles background processes like recurring financial reminders.

## General Engineering Guidelines

### 1. File Changes
- Place FastAPI route definitions inside `app/main.py`.
- Keep complex logic out of controllers; handle parsing in `app/services/llm.py` and state changes in `app/services/finance.py`.
- Update SQLAlchemy definitions in `app/models/` when altering database structure and ensure proper async sessions.

### 2. Interaction with Services
- All outgoing WhatsApp messages must be routed through `app/api/whatsapp.py`.
- Any external NLP/LLM features must be integrated through `app/services/llm.py` logic wrapper to maintain modularity.

### 3. Data & Persistence
- Use SQLAlchemy ORM for database interaction.
- Maintain compatibility for both local SQLite development and production Supabase PostgreSQL deployment.

### 4. Background Tasks
- Any time-based logic, notifications, or batch processing should be encapsulated and orchestrated through `app/scheduler.py`.

### 5. Deployment
- The app uses `Dockerfile` and `render.yaml` for containerized hosting. Treat the app's `lifespan` effectively for cleanly starting and killing background services.

## DeepWiki References
When working on complex architectural portions, refer to the deepwiki references here:
- Overview: https://deepwiki.com/blob1618/luka/1-luka-overview
- Project Structure: https://deepwiki.com/blob1618/luka/1.2-project-structure
- Core Architecture: https://deepwiki.com/blob1618/luka/2-core-architecture
- LLM Service: https://deepwiki.com/blob1618/luka/2.3-llm-service-(gemini-integration)
- Finance Service: https://deepwiki.com/blob1618/luka/3.1-finance-service
- Database Models: https://deepwiki.com/blob1618/luka/4.1-database-models
- Deployment: https://deepwiki.com/blob1618/luka/5-deployment
