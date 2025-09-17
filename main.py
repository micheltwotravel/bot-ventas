# main.py
import os, requests
from fastapi import FastAPI, Request

app = FastAPI()

VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")
WA_TOKEN     = os.getenv("WA_ACCESS_TOKEN")
WA_PHONE_ID  = os.getenv("WA_PHONE_NUMBER_ID")

# Loguea rutas al iniciar para verificar que /wa-webhook existe
@app.on_event("startup")
async def show_routes():
    print("BOOT> Routes:", [r.path for r in app.router.routes])
    print("BOOT> WA_PHONE_ID:", WA_PHONE_ID)
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

@app.get("/")
def root():
    return {"ok": True, "routes": [r.path for r in app.router.routes]}

# GET para verificación de webhook (Meta espera el challenge como texto)
@app.get("/wa-webhook")
async def verify(req: Request):
    mode = req.query_params.get("hub.mode")
    token = req.query_params.get("hub.verify_token")
    challenge = req.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return str(challenge or "")
    return {"error": "verification failed"}

# POST para recibir mensajes y responder un opener simple
@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", data)
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []):
                user = m.get("from")
                if not user: 
                    continue
                url = f"https://graph.facebook.com/v22.0/{WA_PHONE_ID}/messages"
                headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
                payload = {
                    "messaging_product": "whatsapp",
                    "to": user,  # viene sin '+', WhatsApp lo acepta así
                    "type": "text",
                    "text": {"body": "¡Hola! Soy tu concierge virtual de TWOTRAVEL 🛎️✨\nHi! I’m your TWOTRAVEL virtual concierge 🛎️✨"}
                }
                r = requests.post(url, headers=headers, json=payload, timeout=15)
                print("WA send:", r.status_code, r.text)
    return {"status": "ok"}
