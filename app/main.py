import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import redis.asyncio as redis

# Load environment variables from .env file BEFORE importing submodules
load_dotenv()

from app.scheduler import start_scheduler  # noqa: E402
from app.api.whatsapp import send_whatsapp_message  # noqa: E402
from app.services.llm import LLMService  # noqa: E402

# Global Redis client
redis_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    
    # Initialize Redis connection pool
    redis_client = redis.from_url(redis_url, decode_responses=True)
    
    # Basic test to check if the connection works on startup
    try:
        await redis_client.ping()
        print(f"✅ Redis connection successful to {redis_url}")
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")

    start_scheduler()
    yield
    # Shutdown logic
    if redis_client:
        await redis_client.close()

app = FastAPI(title="Luka WhatsApp FinBot", lifespan=lifespan)

# In production, securely load these from environment
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "fallback_token")

@app.get("/")
def read_root():
    return {"message": "Luka API is running"}

@app.get("/redis-test")
async def test_redis():
    """
    Basic test endpoint to verify Redis connectivity from Render.
    """
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis client not initialized")
    try:
        await redis_client.set("test_key", "works", ex=60)
        value = await redis_client.get("test_key")
        return {"status": "ok", "redis_value": value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Redis connection error: {str(e)}")

@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Required for Meta WhatsApp verification
    """
    query_params = request.query_params
    hub_mode = query_params.get("hub.mode") or query_params.get("hub_mode")
    hub_challenge = query_params.get("hub.challenge") or query_params.get("hub_challenge")
    hub_verify_token = query_params.get("hub.verify_token") or query_params.get("hub_verify_token")

    print(
        "[WEBHOOK VERIFY] ",
        f"path={request.url.path}",
        f"mode={hub_mode}",
        f"challenge={hub_challenge}",
        f"token={hub_verify_token}",
        f"expected={VERIFY_TOKEN}",
    )

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN and hub_challenge is not None:
        return PlainTextResponse(content=str(hub_challenge), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Handles incoming messages from WhatsApp Meta API
    """
    data = await request.json()
    print("Received webhook event")
    
    if data.get("object") == "whatsapp_business_account":
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                statuses = value.get("statuses", [])

                for status_event in statuses:
                    print(
                        "WhatsApp status update",
                        f"message_id={status_event.get('id')}",
                        f"status={status_event.get('status')}",
                    )
                
                if messages:
                    print(f"Incoming user message count: {len(messages)}")
                    for message in messages:
                        sender_phone = message.get("from")
                        message_type = message.get("type")
                        
                        if message_type == "text":
                            text_body = message["text"]["body"]
                            
                            # Use process_message to get full intent analysis
                            extracted_data = await LLMService.process_message(text_body)
                            
                            intent = extracted_data.get("intent", "out_of_scope")
                            reply_text = extracted_data.get("reply_text", "")
                            
                            if intent == "expense":
                                amount = extracted_data.get("amount")
                                expense = extracted_data.get("expense") or "gasto"
                                currency = extracted_data.get("currency") or "ARS"
                                
                                amount_text = f"{amount:.2f}" if isinstance(amount, (int, float)) else str(amount)
                                print(
                                    f"[EXPENSE] User {sender_phone}: "
                                    f"{expense} por {amount_text} {currency}"
                                )
                                # TODO: Persist expense to database
                                
                            elif intent == "budget_query":
                                print(f"[BUDGET_QUERY] User {sender_phone}: {text_body}")
                                # TODO: Query budget from database
                                
                            elif intent == "reminder":
                                reminder_title = extracted_data.get("reminder_title")
                                reminder_date = extracted_data.get("reminder_date")
                                print(
                                    f"[REMINDER] User {sender_phone}: "
                                    f"{reminder_title} - {reminder_date}"
                                )
                                # TODO: Create reminder in database
                                
                            elif intent == "expense_summary":
                                print(f"[EXPENSE_SUMMARY] User {sender_phone}: {text_body}")
                                # TODO: Query expense summary from database
                                
                            elif intent == "greeting":
                                print(f"[GREETING] User {sender_phone}: {text_body}")
                                
                            elif intent == "out_of_scope":
                                print(
                                    f"[OUT_OF_SCOPE] User {sender_phone}: "
                                    f"'{text_body}' → guardrail triggered"
                                )
                            
                            await send_whatsapp_message(sender_phone, reply_text)
                            
    return {"status": "ok"}