from fastapi import FastAPI, Form, BackgroundTasks
from slack_sdk.webhook import WebhookClient
from dotenv import load_dotenv
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from dateutil.parser import parse
import re
import unicodedata
from collections import Counter

load_dotenv()
app = FastAPI()

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto or "") if unicodedata.category(c) != 'Mn').lower().strip()

def resumen_individual(data, rep):
    data_rep = [r for r in data if normalizar(r.get("Sales", "")) == normalizar(rep)]
    deals = len(data_rep)
    total = sum(float(r.get("Amount", 0)) for r in data_rep)
    return f"*{rep.title()}*: {deals} deals, ${total:,.0f}"

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()
    
    text_original = text
    text = normalizar(text.strip()) if text else ""
    
    year = datetime.now().year
    year_match = re.search(r"(20\d{2})", text)
    if year_match:
        year = int(year_match.group(1))
        text = text.replace(year_match.group(1), "").strip()
    
    data = []
    for r in rows:
        try:
            date_str = r.get("Date", "")
            if not date_str:
                continue
            date_obj = parse(date_str)
            if date_obj.year == year:
                r["__date"] = date_obj  # opcional para usar luego
                data.append(r)
        except:
            continue

    if text == "todos":
        reps = sorted(set(r.get("Sales", "N/A") for r in data if r.get("Sales") and r.get("Sales").strip()))
        resumenes = [resumen_individual(data, rep) for rep in reps]
        resultado = "*📊 Ventas por responsable - {}*\n\n{}".format(year, "\n".join(resumenes))
        return resultado

    if text:
        data = [r for r in data if text in normalizar(r.get("Sales", ""))]

    if not data:
        return f"No se encontraron resultados para *{text_original}* en {year}."

    deals = len(data)
    amount_total = sum(float(r.get("Amount", 0)) for r in data)

    reps = [r["Sales"] for r in data if r.get("Sales")]
    ciudades = [r["Class"].split(":")[-1].strip() for r in data if r.get("Class")]
    canales = [r["Sales"] for r in data if r.get("Sales")]

    def top(lista): 
        return Counter(lista).most_common(1)[0][0] if lista else "N/A"

    resumen = f"""📊 *Resumen de ventas - {year}*
• Deals: *{deals}*
• Monto total estimado: *${amount_total:,.0f}*
• Responsable top: *{top(reps)}*
• Ciudad top: *{top(ciudades)}*
• Canal top: *{top(canales)}*"""

    return resumen

def procesar_y_responder(response_url, text):
    resumen = filtrar_y_resumir(text)
    webhook = WebhookClient(response_url)
    webhook.send(text=resumen)

@app.post("/slack/ventas")
async def ventas(background_tasks: BackgroundTasks, response_url: str = Form(...), text: str = Form("")):
    background_tasks.add_task(procesar_y_responder, response_url, text)
    return {"response_type": "ephemeral", "text": "⏳ Procesando ventas..."}
