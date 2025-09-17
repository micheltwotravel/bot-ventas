import os
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
load_dotenv()

WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")

app = FastAPI()

# 1) Endpoint para que Meta VERIFIQUE el webhook (GET)
@app.get("/wa-webhook")
async def verify(mode: str = None, challenge: str = None, token: str = None,
                 **kw):
    # Meta envía como query: hub.mode, hub.verify_token, hub.challenge
    hub_mode = kw.get("hub.mode") or mode
    hub_token = kw.get("hub.verify_token") or token
    hub_challenge = kw.get("hub.challenge") or challenge
    if hub_mode == "subscribe" and hub_token == WA_VERIFY_TOKEN:
        # Debe devolver el challenge tal cual
        return int(hub_challenge or 0)
    raise HTTPException(status_code=403, detail="Invalid verify token")

# 2) Endpoint para recibir mensajes (POST) — por ahora solo responde 200
@app.post("/wa-webhook")
async def incoming(_: Request):
    return {"status": "ok"}

