from fastapi import FastAPI, Request
import os, requests

app = FastAPI()

VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")
WA_TOKEN     = os.getenv("WA_ACCESS_TOKEN")
WA_PHONE_ID  = os.getenv("WA_PHONE_NUMBER_ID")

@app.get("/wa-webhook")
async def verify(req: Request):
    mode = req.query_params.get("hub.mode")
    token = req.query_params.get("hub.verify_token")
    challenge = req.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return str(challenge)   # devolver el challenge como texto
    return {"error": "Verification failed"}

@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", data)
    # responder solo si hay mensajes
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []):
                user = m.get("from")
                if user:
                    url = f"https://graph.facebook.com/v22.0/{WA_PHONE_ID}/messages"
                    headers = {"Authorization": f"Bearer {WA_TOKEN}",
                               "Content-Type": "application/json"}
                    payload = {
                        "messaging_product": "whatsapp",
                        "to": user,
                        "type": "text",
                        "text": {"body":
                            "¡Hola! Soy tu concierge virtual de TWOTRAVEL 🛎️✨\n"
                            "Hi! I’m your TWOTRAVEL virtual concierge 🛎️✨"}
                    }
                    r = requests.post(url, headers=headers, json=payload, timeout=15)
                    print("WA send:", r.status_code, r.text)
    return {"status": "ok"}

