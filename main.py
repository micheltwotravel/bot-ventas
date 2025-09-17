import os, requests
from fastapi import FastAPI, Request

app = FastAPI()

WA_PHONE_ID = os.getenv("WA_PHONE_NUMBER_ID")
WA_TOKEN    = os.getenv("WA_ACCESS_TOKEN")

print("BOOT> WA_PHONE_ID:", WA_PHONE_ID)           # <- para verificar en logs
print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))  # <- solo para sanity check

def wa_send_text(to_e164: str, body: str):
    if not WA_PHONE_ID:
        print("ERROR> WA_PHONE_NUMBER_ID no está configurado")
        return
    url = f"https://graph.facebook.com/v22.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if r.status_code >= 300:
        print("WA send error:", r.status_code, r.text)
