from fastapi import FastAPI, Request
import os, requests

app = FastAPI()

VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")
WA_PHONE_ID  = os.getenv("WA_PHONE_NUMBER_ID")
WA_TOKEN     = os.getenv("WA_ACCESS_TOKEN")

@app.get("/wa-webhook")
async def verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)
    return {"error": "Verification failed"}

@app.post("/wa-webhook")
async def incoming(request: Request):
    data = await request.json()
    print("Incoming webhook:", data)  # Verás esto en logs
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                user = msg.get("from")
                if user:
                    wa_send_text(user, "Hola 👋, soy tu concierge virtual de TwoTravel")
    return {"status": "ok"}

def wa_send_text(to: str, body: str):
    url = f"https://graph.facebook.com/v22.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(url, headers=headers, json=payload)
    print("WA send response:", r.status_code, r.text)
