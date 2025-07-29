from fastapi import FastAPI, Form
from slack_sdk.webhook import WebhookClient
from dotenv import load_dotenv
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from dateutil.parser import parse
import calendar
import re
import locale

# Establecer español para reconocer nombres de mes
try:
    locale.setlocale(locale.LC_TIME, 'es_CO.utf8')
except:
    locale.setlocale(locale.LC_TIME, 'es_ES.utf8')

load_dotenv()
app = FastAPI()

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

# Función para detectar mes por nombre
def detectar_mes(texto):
    texto = texto.lower()
    for i in range(1, 13):
        mes_es = calendar.month_name[i].lower()
        mes_en = calendar.month_name[i].lower().translate(str.maketrans("áéíóú", "aeiou"))
        if mes_es in texto or mes_en in texto:
            return i
    return None

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()

    # Año y mes actuales como default
    year = datetime.now().year
    mes_actual = datetime.now().month

    # Detectar año explícito
    match = re.search(r"(20\d{2})", text)
    if match:
        year = int(match.group(1))
        text = text.replace(match.group(1), "").strip().lower()

    # Detectar mes por nombre
    mes_detectado = detectar_mes(text)
    if mes_detectado:
        mes_actual = mes_detectado
        for nombre in calendar.month_name:
            text = text.replace(nombre.lower(), "").strip()

    # Filtrar datos por fecha
    data = []
    for r in rows:
        try:
            date_str = r.get("Date", "")
            if not date_str:
                continue
            date_obj = parse(date_str)
            if date_obj.year == year and date_obj.month == mes_actual:
                data.append(r)
        except:
            continue

    # Filtro por texto libre (responsable o ciudad)
    text = text.strip().lower()
    if text:
        data = [
            r for r in data if
            text in str(r.get("Sales", "")).lower() or
            text in str(r.get("Class", "")).lower()
        ]

    if not data:
        return f"No se encontraron resultados para *{text or 'el mes'}* en {year}."

    # Métricas
    deals = len(data)
    amount_total = sum(float(r.get("Amount", 0)) for r in data)
    responsables = [r["Sales"] for r in data if r.get("Sales")]
    ciudades = [r["Class"].split(":")[1] for r in data if "Class" in r and ":" in r["Class"]]

    def top(lista):
        return max(set(lista), key=lista.count) if lista else "N/A"

    resumen = f"""
📊 *Resumen de ventas - {calendar.month_name[mes_actual]} {year}*

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
