# Vercel Deployment Guide

## ✅ What's Been Set Up

The following files have been created to prepare your FastAPI project for Vercel:

- **vercel.json** - Vercel configuration specifying Python runtime and routing
- **.vercelignore** - Files to exclude from deployment
- **runtime.txt** - Python version specification (3.11)

## 📋 Pre-Deployment Checklist

### 1. Environment Variables
Before deploying, you'll need to set environment variables in Vercel:

Go to Vercel Project Settings → Environment Variables and add:
- `WHATSAPP_VERIFY_TOKEN` - Your WhatsApp verification token
- `DATABASE_URL` - Your database connection string (see below)
- Any other API keys or configuration values

### 2. Database Configuration

**Important:** SQLite (default) won't work on Vercel because serverless functions have ephemeral storage.

You have two options:

#### Option A: PostgreSQL (Recommended)
1. Set up a PostgreSQL database (e.g., on Railway, Supabase, or Amazon RDS)
2. Update your database connection in `app/models/database.py`:
   ```python
   DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@host/dbname")
   engine = create_engine(DATABASE_URL)
   ```
3. Add the DATABASE_URL environment variable in Vercel

#### Option B: MongoDB
1. Set up MongoDB (e.g., MongoDB Atlas)
2. Update models to use a MongoDB driver like `mongoengine` or `motor`

### 3. Background Jobs / Scheduler

**⚠️ Important:** APScheduler won't work reliably on Vercel serverless functions because:
- Instances are ephemeral and scale independently
- Jobs need external coordination

**Solutions:**
- **Option A:** Use a separate service for scheduled tasks (e.g., AWS Lambda, Google Cloud Functions)
- **Option B:** Use a job queue service (e.g., Celery with Redis, RQ)
- **Option C:** Use Vercel Cron Jobs (limited to certain routes)
- **Option D:** For now, disable the scheduler in production and rely on webhook events

To disable the scheduler in serverless:
```python
import os
if not os.getenv("VERCEL"):
    start_scheduler()
```

## 🚀 Deployment Steps

### Step 1: Install Vercel CLI
```bash
npm i -g vercel
```

### Step 2: Connect Your GitHub Repository
1. Go to [vercel.com](https://vercel.com)
2. Click "New Project"
3. Import your Git repository
4. Vercel will auto-detect it's a Python project

### Step 3: Set Environment Variables in Vercel
- Click "Environment Variables"
- Add all required variables (WHATSAPP_VERIFY_TOKEN, DATABASE_URL, etc.)

### Step 4: Deploy via CLI (Alternative)
```bash
vercel
```

### Step 5: Configure Custom Domain (Optional)
In Vercel Project Settings → Domains, add your custom domain.

## 🔗 Testing Your Deployment

Once deployed, test the endpoints:

```bash
# Test root endpoint
curl https://your-vercel-url.vercel.app/

# Test webhook verification (replace with your token)
curl "https://your-vercel-url.vercel.app/webhook?hub.mode=subscribe&hub.challenge=test&hub.verify_token=YOUR_TOKEN"
```

## 📝 Final Notes

- Your FastAPI app is now serverless on Vercel's platform
- All traffic will be routed through the `/webhook` endpoint for WhatsApp
- The free tier should handle development workloads
- For production, consider upgrading to a Pro plan for better performance

## 🐛 Troubleshooting

**Build fails:** Check that all dependencies in `requirements.txt` are compatible with Python 3.11

**Environmental variables not working:** Ensure they're added in Vercel settings, not just locally in `.env`

**Database connection errors:** Verify your DATABASE_URL is correct and the database accepts connections from Vercel's IP ranges

**WhatsApp webhooks failing:** After deployment, update your Meta Developer Dashboard webhook URL to point to: `https://your-vercel-url.vercel.app/webhook`
