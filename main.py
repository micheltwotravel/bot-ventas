from fastapi import FastAPI, Request
import os
import requests

app = FastAPI()

VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID")

# ✅ Ruta GET para verificación del webhook
@app.get("/wa-webhook")
async def verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)
    return {"error": "Verification failed"}

# ✅ Ruta POST para recibir mensajes
@app.post("/wa-webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Incoming webhook:", data)

    # Si hay un mensaje entrante, responde
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" in entry:
            msg = entry["messages"][0]
            from_number = msg["from"]  # número del usuario
            text = msg.get("text", {}).get("body", "")

            url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
            headers = {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {
                "messaging_product": "whatsapp",
                "to": from_number,
                "type": "text",
                "text": {
                    "body": "¡Hola! Soy tu concierge virtual de TwoTravel 🛎️✨"
                }
            }
            requests.post(url, headers=headers, json=payload)
    except Exception as e:
        print("Error procesando mensaje:", e)

    return {"status": "ok"}

