import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Load environment variables from .env file BEFORE importing submodules
load_dotenv()

from app.scheduler import start_scheduler
from app.api.whatsapp import send_whatsapp_message
from app.services.llm import LLMService
from app.services.finance import FinanceService

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    # Shutdown logic if needed

app = FastAPI(title="Grumium WhatsApp FinBot", lifespan=lifespan)

# In production, securely load these from environment
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "fallback_token")

@app.get("/")
def read_root():
    return {"message": "Grumium API is running"}

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
    print(f"Received webhook: {data}")
    
    if data.get("object") == "whatsapp_business_account":
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                
                if messages:
                    for message in messages:
                        sender_phone = message.get("from")
                        message_type = message.get("type")
                        
                        if message_type == "text":
                            text_body = message["text"]["body"]
                            
                            # Use our LLM stub
                            extracted_data = await LLMService.process_text_expense(text_body)
                            
                            # Use our Finance stub to get a positive response
                            reallocation_msg = FinanceService.check_dynamic_budget(
                                user_id=1, 
                                new_expense=extracted_data["amount"], 
                                category=extracted_data["category"]
                            )
                            
                            reply_text = (
                                f"Registré tu gasto: ${extracted_data['amount']} en '{extracted_data['category']}'.\n\n"
                                f"{reallocation_msg}"
                            )
                            
                            await send_whatsapp_message(sender_phone, reply_text)
                            
    return {"status": "ok"}
