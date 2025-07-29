from fastapi import FastAPI, Form
from slack_sdk.webhook import WebhookClient
from dotenv import load_dotenv
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from dateutil.parser import parse
import unicodedata
import re
from collections import Counter

load_dotenv()
app = FastAPI()

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

def normalizar(txt):
    txt = str(txt).lower()
    txt = unicodedata.normalize('NFD', txt).encode('ascii', 'ignore').decode("utf-8")
    return txt.strip()

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()

    # Año por defecto actual
    year = datetime.now().year
    match = re.search(r"(20\d{2})", text)
    if match:
        year = int(match.group(1))
        text = text.replace(match.group(1), "").strip()

    texto_normalizado = normalizar(text)

    # Filtrar por mes actual y año
    data = []
    for r in rows:
        try:
            date_str = r.get("Date", "")
            if not date_str:
                continue
            date_obj = parse(date_str)
            if date_obj.year == year and date_obj.month == datetime.now().month:
                data.append(r)
        except:
            continue

    # Filtro por texto libre (responsable o ciudad)
    if texto_normalizado:
        data = [
            r for r in data if
            texto_normalizado in normalizar(r.get("Sales", "")) or
            texto_normalizado in normalizar(r.get("Class", ""))
        ]

    if not data:
        return f"No se encontraron resultados para *{text or 'el mes'}* en {year}."

    # Métricas
    deals = len(data)
    amount_total = sum(float(r.get("Amount", 0)) for r in data)

    # Agrupar normalizados para contar, pero devolver nombre original más frecuente
    def top_original(campo):
        lista = [r[campo] for r in data if r.get(campo)]
        normalizados = [normalizar(x) for x in lista]
        if not normalizados:
            return "N/A"
        top_norm = Counter(normalizados).most_common(1)[0][0]
        for x in lista:
            if normalizar(x) == top_norm:
                return x
        return "N/A"

    responsable_top = top_original("Sales")
    ciudad_top = top_original("Class").split(":")[1] if ":" in top_original("Class") else top_original("Class")

    resumen = f"""
📊 *Resumen de ventas - {datetime.now().strftime('%B %Y')}*

• Deals: *{deals}*
• Monto total estimado: *${amount_total:,.0f}*
• Responsable top: *{responsable_top}*
• Ciudad top: *{ciudad_top}*
""".strip()

    return resumen

@app.post("/slack/ventas")
async def ventas(response_url: str = Form(...), text: str = Form("")):
    resumen = filtrar_y_resumir(text)
    webhook = WebhookClient(response_url)
    webhook.send(text=resumen)
    return {"status": "ok"}
