# añade arriba si no lo tienes
import os, requests
from fastapi import FastAPI, Request

app = FastAPI()
VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")
WA_PHONE_ID  = os.getenv("WA_PHONE_NUMBER_ID")
WA_TOKEN     = os.getenv("WA_ACCESS_TOKEN")

def wa_send_text(to_e164: str, body: str):
    url = f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    # si hay error, imprime en logs (Render → Logs)
    if r.status_code >= 300:
        print("WA send error:", r.status_code, r.text)

@app.get("/wa-webhook")
async def verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return str(challenge)
    return {"error": "Verification failed"}

@app.post("/wa-webhook")
async def incoming(request: Request):
    data = await request.json()
    # WhatsApp envía: entry -> changes -> value -> messages
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []):
                # número del usuario (viene sin '+')
                user = m.get("from")
                if not user:
                    continue
                to = f"+{user}"
                opener = (
                    "¡Hola! Soy tu concierge virtual de TWOTRAVEL 🛎️✨.\n"
                    "Puedo ayudarte con villas, botes, islas, bodas/eventos y concierge.\n"
                    "¿En qué idioma prefieres continuar? (ES / EN)\n\n"
                    "Hi! I’m your TWOTRAVEL virtual concierge 🛎️✨.\n"
                    "I can help with villas, boats, islands, weddings/events and concierge.\n"
                    "Which language would you prefer? (ES / EN)"
                )
                wa_send_text(to, opener)
    # responde 200 rápido para que Meta quede OK
    return {"status": "ok"}
