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

    # Detectar año si se menciona
    year = datetime.now().year
    match = re.search(r"(20\d{2})", text)
    if match:
        year = int(match.group(1))
        text = text.replace(match.group(1), "").strip().lower()
    else:
        text = text.strip().lower()

    # Obtener año y mes desde la columna "Date"
    def get_year_month(record):
        try:
            date_str = record.get("Date")
            if not date_str:
                return None, None
            dt = parse(date_str)
            return dt.year, dt.month
        except:
            return None, None

    # Filtrar por año y mes actual
    data = []
    for r in rows:
        r_year, r_month = get_year_month(r)
        if r_year == year and r_month == datetime.now().month:
            data.append(r)

    # Filtro opcional por texto
    if text:
        data = [
            r for r in data if
            text in str(r.get("Rep", "")).lower() or
            text in str(r.get("Sales", "")).lower() or
            text in str(r.get("Class", "")).lower()
        ]

    if not data:
        return f"No se encontraron resultados para *{text or 'el mes'}* en {year}."

    # Métricas
    deals = len(data)
    amount_total = sum(float(r.get("Amount", 0)) for r in data if str(r.get("Amount", "")).replace(".", "").replace(",", "").isdigit())
    reps = [r.get("Rep", "") for r in data if r.get("Rep")]
    ciudades = [r["Class"].split(":")[1] for r in data if "Class" in r and ":" in r["Class"]]
    canales = [r.get("Sales", "") for r in data if r.get("Sales")]

    def top(lista):
        return max(set(lista), key=lista.count) if lista else "N/A"

    resumen = f"""
📊 *Resumen de ventas - {datetime.now().strftime('%B %Y')}*

• Deals: *{deals}*
• Monto total estimado: *${amount_total:,.0f}*
• Responsable top: *{top(reps)}*
• Ciudad top: *{top(ciudades)}*
• Canal top: *{top(canales)}*
    """.strip()

    return resumen

@app.post("/slack/ventas")
async def ventas(response_url: str = Form(...), text: str = Form("")):
    try:
        resumen = filtrar_y_resumir(text)
        webhook = WebhookClient(response_url)
        webhook.send(text=resumen)
        return {"ok": True}
    except Exception as e:
        print(f"Error: {e}")
        webhook = WebhookClient(response_url)
        webhook.send(text="⚠️ Ocurrió un error procesando la solicitud.")
        return {"ok": False, "error": str(e)}
