# Grumium - WhatsApp FinBot

Grumium is a WhatsApp bot designed to help users track expenses, incomes, and budgets using natural language, voice notes, and receipt images.

## Prerequisites

- Python 3.9+
- A [Meta Developer Account](https://developers.facebook.com/) with a WhatsApp app configured.
- (Optional but recommended) [ngrok](https://ngrok.com/) for local testing.

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

You have two options:

### Option A: Local Testing with Ngrok (Recommended for Development)
You do **not** need to deploy your app to the cloud just to test it. You can expose your local server to the internet using a tool like `ngrok`.

1. Install [ngrok](https://ngrok.com/download).
2. With your FastAPI server running on port 8000, start ngrok:
   ```bash
   ngrok http 8000
   ```
3. Ngrok will give you an `https://<random-id>.ngrok-free.app` URL.
4. Go to your Meta Developer Dashboard -> WhatsApp -> Configuration.
5. Edit the Webhook URL and paste your ngrok URL with the webhook path:
   `https://<random-id>.ngrok-free.app/webhook`
6. Enter the `WHATSAPP_VERIFY_TOKEN` you set in your `.env` file and verify.

*Note: The free version of ngrok changes its URL every time you restart it, so you will need to update the Meta Developer dashboard if you restart ngrok.*

### Option B: Cloud Deployment (For Production/Always-on)
If you want to deploy the bot so it is always running without your computer being on, you can use PaaS (Platform as a Service) providers.

#### Deploying on Render (Free Tier Available)
1. Push your code to a GitHub repository.
2. Go to [Render](https://render.com/) and create a new **Web Service**.
3. Connect your GitHub repository.
4. Set the Start Command to: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Set the Build Command to: `pip install -r requirements.txt`
6. Under "Environment", add all the variables from your `.env` file.
7. Render will provide you with an `https://<your-app>.onrender.com` URL. Use this URL (with `/webhook` appended) in the Meta dashboard.

#### Other alternatives
- **Railway.app**: Similar to Render, very fast GitHub deployment.
- **Fly.io**: Great container-based deployment using a Dockerfile.
