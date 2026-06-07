from fastapi.testclient import TestClient
import os
import pytest
from app.main import app

# Create a TestClient using the FastAPI app
client = TestClient(app)

def test_verify_webhook_success():
    # Set the token for the test
    os.environ["WHATSAPP_VERIFY_TOKEN"] = "test_verify_token"
    
    # Reload the token from environment in case main.py already loaded it
    # We will simulate the request to the webhook endpoint
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test_verify_token",
            "hub.challenge": "1158201444"
        }
    )
    
    # In main.py VERIFY_TOKEN is loaded once at the module level.
    # We might need to mock or ensure the token matches the one in app.main.VERIFY_TOKEN.
    # Because of how fastapi testclient works, it might use the default "fallback_token" if env wasn't set earlier.
    # Let's import the actual token being used to ensure test passes
    from app.main import VERIFY_TOKEN
    
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN
            "hub.challenge": "1158201444"
        }
    )
    
    assert response.status_code == 200
    assert response.text == "1158201444"

def test_verify_webhook_failure():
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "1158201444"
        }
    )
    
    assert response.status_code == 403
    assert response.json() == {"detail": "Verification failed"}

@pytest.mark.asyncio
async def test_handle_webhook_valid_payload():
    # Mock payload simulating an incoming message from WhatsApp
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "123456123456"
                            },
                            "contacts": [{"profile": {"name": "Test User"}, "wa_id": "12345"}],
                            "messages": [
                                {
                                    "from": "12345",
                                    "id": "wamid.HBgL",
                                    "timestamp": "1603059201",
                                    "text": {"body": "Gasté 500 en comida"},
                                    "type": "text"
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }
    
    # In a real scenario we'd mock LLMService.process_text_expense and send_whatsapp_message
    # But for a basic sanity test we just ensure the endpoint accepts the JSON and returns 200 OK
    response = client.post("/webhook", json=payload)
    
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
