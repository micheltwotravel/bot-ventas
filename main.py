from fastapi import FastAPI, Form
from slack_sdk.webhook import WebhookClient
from dotenv import load_dotenv
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from dateutil.parser import parse
import re
import unicodedata

load_dotenv()
app = FastAPI()

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

# Normalizador para quitar tildes y bajar a minúscula
def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto or "") if unicodedata.category(c) != 'Mn').lower().strip()

def resumen_individual(data, rep):
    # Usar "Sales" para los responsables de ventas
    data_rep = [r for r in data if normalizar(rep) in normalizar(r.get("Sales", ""))]
    deals = len(data_rep)
    total = sum(float(r.get("Amount", 0)) for r in data_rep)
    return f"*{rep.title()}*: {deals} deals, ${total:,.0f}"

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()
    
    # Año detectado
    year = datetime.now().year
    match = re.search(r"(20\d{2})", text)
    if match:
        year = int(match.group(1))
        text = text.replace(match.group(1), "").strip()
    
    text = normalizar(text)
    
    # Filtrar por mes y año actual
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
    
    if text == "todos":
        # Usar "Sales" para los responsables de ventas
        reps = sorted(set(r.get("Sales", "N/A") for r in data if r.get("Sales") and r.get("Sales").strip()))
        resumenes = [resumen_individual(data, rep) for rep in reps]
        return "*📊 Ventas por responsable - {} {}*\n\n{}".format(
            datetime.now().strftime("%B"), year, "\n".join(resumenes)
        )
    
    if text:
        data = [
            r for r in data if
            text in normalizar(r.get("Sales", "")) or  # Sales para responsables
            text in normalizar(r.get("Class", "")) or
            text in normalizar(r.get("Posting", ""))
        ]
    
    if not data:
        return f"No se encontraron resultados para *{text or 'el mes'}* en {year}."
    
    # Métricas generales
    deals = len(data)
    amount_total = sum(float(r.get("Amount", 0)) for r in data)
    
    # Usar "Sales" para obtener los responsables, filtrar valores vacíos
    reps = [r["Sales"] for r in data if r.get("Sales") and r.get("Sales").strip()]
    ciudades = [r["Class"].split()[-1] for r in data if r.get("Class")]
    canales = [r["Sales"] for r in data if r.get("Sales") and r.get("Sales").strip()]
    
    def top(lista): 
        if not lista:
            return "N/A"
        # Contar frecuencias y obtener el más común
        from collections import Counter
        counter = Counter(lista)
        return counter.most_common(1)[0][0] if counter else "N/A"
    
    resumen = f"""📊 *Resumen de ventas - {datetime.now().strftime('%B %Y')}*
• Deals: *{deals}*
• Monto total estimado: *${amount_total:,.0f}*
• Responsable top: *{top(reps)}*
• Ciudad top: *{top(ciudades)}*
• Canal top: *{top(canales)}*"""

    return resumen

@app.post("/slack/ventas")
async def ventas(response_url: str = Form(...), text: str = Form("")):
    resumen = filtrar_y_resumir(text)
    webhook = WebhookClient(response_url)
    webhook.send(text=resumen)
    return {"
