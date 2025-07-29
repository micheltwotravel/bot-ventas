from fastapi import FastAPI, Form
from slack_sdk.webhook import WebhookClient
from dotenv import load_dotenv
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from dateutil.parser import parse
import re
from collections import defaultdict

load_dotenv()
app = FastAPI()

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

def resumen_individual(data, rep):
    data_rep = [r for r in data if r.get("Rep", "").lower() == rep.lower()]
    deals = len(data_rep)
    total = sum(float(r.get("Amount", 0)) for r in data_rep)
    return f"*{rep.title()}*: {deals} deals, ${total:,.0f}"

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()

    # Año detectado opcional
    year = datetime.now().year
    match = re.search(r"(20\d{2})", text)
    if match:
        year = int(match.group(1))
        text = text.replace(match.group(1), "").strip().lower()
    else:
        text = text.strip().lower()

    # Filtrar por fecha
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

    # Si es "todos", devolver por cada rep
    if text == "todos":
        reps = sorted(set(r.get("Rep", "N/A") for r in data if r.get("Rep")))
        resumenes = [resumen_individual(data, rep) for rep in reps]
        return "*📊 Ventas por responsable - {} {}*\n\n{}".format(
            datetime.now().strftime("%B"), year,
            "\n".join(resumenes)
        )

    # Filtro general
    if text:
        data = [
            r for r in data if
            text in str(r.get("Rep", "")).lower() or
            text in str(r.get("Sales", "")).lower() or
            text in str(r.get("Class", "")).lower()
        ]

    if not data:
        return f"No se encontraron resultados para *{text or 'el mes'}* en {year}."

    # Métricas generales
    deals = len(data)
    amount_total = sum(float(r.get("Amount", 0)) for r in data)
    reps = [r["Rep"] for r in data if r.get("Rep")]
    ciudades = [r["Class"].split(":")[1] for r in data if "Class" in r and ":" in r["Class"]]
    canales = [r["Sales"] for r in data if r.get("Sales")]

    def top(lista): return max(set(lista), key=lista.count) if lista else "N/A"

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
    resumen = filtrar_y_resumir(text)
    webhook = WebhookClient(response_url)
    webhook.send(text=resumen)
    return {"status": "ok"}
