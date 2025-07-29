from fastapi import FastAPI, Form
from slack_sdk.webhook import WebhookClient
from dotenv import load_dotenv
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from dateutil.parser import parse
import re

load_dotenv()
app = FastAPI()

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()

    # Año por defecto actual
    year = datetime.now().year

    # Detectar año en el texto (ej: "2024", "sofia 2025", etc.)
    match = re.search(r"(20\d{2})", text)
    if match:
        year = int(match.group(1))
        text = text.replace(match.group(1), "").strip().lower()
    else:
        text = text.strip().lower()

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

    # Filtro por texto libre (en Sales o en Class)
    if text:
        data = [
            r for r in data if
            text in str(r.get("Sales", "")).lower().strip() or
            text in str(r.get("Class", "")).lower().strip()
        ]

    if not data:
        responsables_unicos = sorted(set(r.get("Sales", "").strip() for r in rows if r.get("Sales")))
        ciudades_unicas = sorted(set(r.get("Class", "").split(":")[1].strip() for r in rows if "Class" in r and ":" in r["Class"]))
        sugerencia = f"*Responsables válidos:* {', '.join(responsables_unicos)}\n*Ciudades válidas:* {', '.join(ciudades_unicas)}"
        return f"No se encontraron resultados para *{text or 'el mes actual'}* en {year}.\n\n{sugerencia}"

    # Métricas del resumen
    deals = len(data)
    amount_total = sum(float(r.get("Amount", 0)) for r in data)
    responsables = [r["Sales"] for r in data if r.get("Sales")]
    ciudades = [r["Class"].split(":")[1] for r in data if "Class" in r and ":" in r["Class"]]

    def top(lista):
        return max(set(lista), key=lista.count) if lista else "N/A"

    resumen = f"""
📊 *Resumen de ventas - {datetime.now().strftime('%B %Y')}*

• Deals: *{deals}*
• Monto total estimado: *${amount_total:,.0f}*
• Responsable top: *{top(responsables)}*
• Ciudad top: *{top(ciudades)}*
""".strip()

    return resumen

@app.post("/slack/ventas")
async def ventas(response_url: str = Form(...), text: str = Form("")):
    resumen = filtrar_y_resumir(text)
    webhook = WebhookClient(response_url)
    webhook.send(text=resumen)
    return {"status": "ok"}
