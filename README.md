[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/blob1618/luka)
# Luka - WhatsApp FinBot

Luka is a WhatsApp bot designed to help users track expenses, incomes, and budgets using natural language, voice notes, and receipt images.

## Prerequisites

- Python 3.9+
- A [Meta Developer Account](https://developers.facebook.com/) with a WhatsApp app configured.

## Local Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables:**
   Copy the example environment file and fill in your details:
   ```bash
   cp .env.example .env
   ```

3. **Run the FastAPI server locally:**
   ```bash
   uvicorn app.main:app --reload
   ```
   The server will start on `http://127.0.0.1:8000`.

## Testing with Meta WhatsApp API

To test the bot, Meta's WhatsApp API needs to send events (webhooks) to your application. **Meta requires a public `HTTPS` URL for this.** 

#### Deploying on Render (Free Tier Available)
1. Push your code to a GitHub repository.
2. Go to [Render](https://render.com/) and create a new **Web Service**.
3. Connect your GitHub repository.
4. Set the Start Command to: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Set the Build Command to: `pip install -r requirements.txt`
6. Under "Environment", add all the variables from your `.env` file.
7. Render will provide you with an `https://<your-app>.onrender.com` URL. Use this URL (with `/webhook` appended) in the Meta dashboard.
