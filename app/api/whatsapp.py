import os
import httpx

WHATSAPP_API_TOKEN = os.getenv("WHATSAPP_API_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")

async def send_whatsapp_message(to_number: str, message_text: str):
    """
    Sends a text message via Meta's WhatsApp API.
    """
    # Fix for Argentina numbers: Meta API requires the number without the '9'
    if to_number.startswith("549") and len(to_number) == 13:
        to_number = "54" + to_number[3:]

    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_ID:
        print("Missing WHATSAPP_API_TOKEN or WHATSAPP_PHONE_ID. Cannot send message.")
        return

    url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {
            "body": message_text
        }
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            print(f"Failed to send message: {response.text}")
        else:
            print(f"Message sent to {to_number}")
