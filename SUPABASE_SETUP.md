# Supabase Integration Guide

## Step 1: Create Supabase Project

1. Go to [supabase.com](https://supabase.com)
2. Sign up or log in
3. Click "New Project"
4. Fill in:
   - **Project name:** `luka` (or any name)
   - **Database Password:** Create a strong password (save this!)
   - **Region:** Choose closest to you
5. Click "Create new project" and wait ~2 minutes

## Step 2: Get Your Connection String

1. Once your project is created, go to **Settings → Database**
2. Look for **Connection string** section
3. Click the tab for **"URI"** (not Pool)
4. Copy the connection string (it looks like):
   ```
   postgresql://postgres:[PASSWORD]@[HOST]:[PORT]/postgres
   ```

## Step 3: Update Your Environment

Replace `[PASSWORD]` with the password you created, then add to your `.env`:

```bash
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@YOUR_HOST:5432/postgres
```

**Example:**
```bash
DATABASE_URL=postgresql://postgres:MySecurePass123@db.supabase.co:5432/postgres
```

## Step 4: Initialize Database Tables

Run this to create all tables:

```bash
python
>>> from app.models.database import engine, Base
>>> Base.metadata.create_all(bind=engine)
```

Or from command line:
```bash
python -c "from app.models.database import engine, Base; Base.metadata.create_all(bind=engine)"
```

## Step 5: Verify Connection (Optional)

Test the connection works:

```bash
python -c "from app.models.database import SessionLocal; db = SessionLocal(); print('✓ Connected to Supabase!')"
```

## Step 6: Deploy to Render

1. Create a Render account and add a new **Web Service**.
2. Connect your Git repository and select the branch to deploy.
3. Choose the environment: use **Docker** (recommended) or **Python**.
   - If using Docker: include the provided `Dockerfile` in the repo — Render will build the image automatically.
   - If using Python (no Docker): set the build command to `pip install -r requirements.txt` and the start command to:
     ```bash
     gunicorn -w 4 -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:$PORT
     ```
4. In Render's dashboard, add required environment variables: `WHATSAPP_VERIFY_TOKEN`, `DATABASE_URL`, and any API keys.
5. Deploy. Render provides a persistent server, so the app's internal scheduler will run as expected.

## ⚠️ Important Notes

- **Keep your password secret!** Don't commit it to git
- Your `.env` file is already in `.gitignore`
- For Supabase Free tier: 500MB storage, plenty for development
- Connection pooling available with Supabase's PgBouncer (enable in Settings if you hit connection limits)

## 🔗 Useful Supabase Features

- **SQL Editor:** Go to **SQL Editor** to run raw queries or manage tables
- **Auth:** Supabase has built-in auth (optional, not needed for your current setup)
- **Storage:** For file uploads (expenses receipts, etc.)
- **Real-time:** For live updates across clients

## Troubleshooting

**"Connection refused"** → Check DATABASE_URL is correct, paste it exactly from Supabase

**"too many connections"** → Free tier has a connection limit. Enable PgBouncer in Supabase Settings

**"relation does not exist"** → Run the initialization command from Step 4 to create tables
