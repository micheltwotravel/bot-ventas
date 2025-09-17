from fastapi import FastAPI, Request
import os

app = FastAPI()

VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")

@app.get("/wa-webhook")
async def verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)
    else:
        return {"error": "Verification failed"}

