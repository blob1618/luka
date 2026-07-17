import os
import httpx

WHATSAPP_API_TOKEN = os.getenv("WHATSAPP_API_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")

async def send_whatsapp_message(
    to_number: str,
    message_text: str | None = None,
    *,
    template_name: str | None = None,
    template_parameters: list[str] | None = None,
):
    """
    Envía un mensaje de texto o template a través de la API de WhatsApp de Meta.
    """
    # Corrección para números argentinos: la API de Meta requiere el número sin el '9'
    if to_number.startswith("549") and len(to_number) == 13:
        to_number = "54" + to_number[3:]

    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_ID:
        print("Falta WHATSAPP_API_TOKEN o WHATSAPP_PHONE_ID. No se puede enviar el mensaje.")
        return False

    url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
        "Content-Type": "application/json"
    }

    if template_name:
        parameters = template_parameters or []
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": "es_AR",
                },
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(parameter)}
                            for parameter in parameters
                        ],
                    }
                ],
            },
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {
                "body": message_text or "",
            },
        }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            print(f"Error al enviar el mensaje: {response.text}")
            return False

        print(f"Mensaje enviado a {to_number}")
        return True
