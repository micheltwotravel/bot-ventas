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

# Aliases para nombres equivalentes
alias = {
    "sofia millan wedding": "sofia milan",
    "sofia millan": "sofia milan",
    "sofía milan": "sofia milan",
    "sofia milan": "sofia milan",
}

# Utilidades
def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto or "") if unicodedata.category(c) != 'Mn').lower().strip()

def normalizar_nombre(nombre):
    n = normalizar(nombre)
    return alias.get(n, n)

def meses_inv(mes_num):
    if not mes_num:
        return ""
    meses = [
        "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
    ]
    return meses[mes_num].capitalize()

def resumen_individual(data, rep):
    data_rep = [r for r in data if normalizar_nombre(r.get("Sales", "")) == normalizar_nombre(rep)]
    deals = len(data_rep)
    total = sum(float(r.get("Amount", 0)) for r in data_rep)
    return f"*{rep.title()}*: {deals} deals, ${total:,.0f}"

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()

    text_original = text
    text = normalizar(text.strip()) if text else ""

    # Año
    year = datetime.now().year
    year_match = re.search(r"(20\d{2})", text)
    if year_match:
        year = int(year_match.group(1))
        text = text.replace(year_match.group(1), "").strip()

    # Mes
    meses = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12
    }
    mes = None
    for m in meses:
        if m in text:
            mes = meses[m]
            text = text.replace(m, "").strip()
            break

    # Filtrar por fecha
    data = []
    for r in rows:
        try:
            date_str = r.get("Date", "")
            if not date_str:
                continue
            date_obj = parse(date_str)
            if date_obj.year == year and (mes is None or date_obj.month == mes):
                r["__date"] = date_obj
                data.append(r)
        except:
            continue

    # Agrupación por todos
    if text == "todos":
        reps_originales = [r.get("Sales", "N/A") for r in data if r.get("Sales")]
        reps_norm = sorted(set(normalizar_nombre(rep) for rep in reps_originales))
        resumenes = [resumen_individual(data, rep) for rep in reps_norm]
        periodo = f"{meses_inv(mes)} {year}" if mes else str(year)
        resultado = f"*📊 Ventas por responsable - {periodo}*\n\n" + "\n".join(resumenes)
        return resultado

    # Filtro por responsable (si hay texto restante)
    if text:
        data = [r for r in data if text in normalizar(normalizar_nombre(r.get("Sales", "")))]

    if not data:
        periodo = f"{meses_inv(mes)} {year}" if mes else str(year)
        return f"No se encontraron resultados para *{text_original}* en {periodo}."

    deals = len(data)
    amount_total = sum(float(r.get("Amount", 0)) for r in data)
    reps = [normalizar_nombre(r["Sales"]) for r in data if r.get("Sales")]
    ciudades = [r["Class"].split(":")[-1].strip() for r in data if r.get("Class")]
    canales = reps

    def top(lista): 
        return Counter(lista).most_common(1)[0][0].title() if lista else "N/A"

    periodo = f"{meses_inv(mes)} {year}" if mes else str(year)
    resumen = f"""📊 *Resumen de ventas - {periodo}*
• Deals: *{deals}*
• Monto total estimado: *${amount_total:,.0f}*
• Responsable top: *{top(reps)}*
• Ciudad top: *{top(ciudades)}*

    return resumen

# Asíncrono para evitar timeout
def procesar_y_responder(response_url, text):
    resumen = filtrar_y_resumir(text)
    webhook = WebhookClient(response_url)
    webhook.send(text=resumen)

@app.post("/slack/ventas")
async def ventas(background_tasks: BackgroundTasks, response_url: str = Form(...), text: str = Form("")):
    background_tasks.add_task(procesar_y_responder, response_url, text)
    return {"response_type": "ephemeral", "text": "⏳ Procesando ventas..."}

