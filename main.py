from fastapi import FastAPI, Form
from slack_sdk.webhook import WebhookClient
from dotenv import load_dotenv
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from dateutil.parser import parse
import re
import calendar

load_dotenv()
app = FastAPI()

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

def detectar_mes(texto):
    texto = texto.lower()
    for i in range(1, 13):
        nombre_mes = calendar.month_name[i].lower()
        if nombre_mes in texto:
            return i
    return None

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()

    year = datetime.now().year
    mes_actual = datetime.now().month

    # Buscar si hay un año escrito
    match = re.search(r"(20\d{2})", text)
    if match:
        year = int(match.group(1))
        text = re.sub(r"\b" + match.group(1) + r"\b", "", text)

    # Detectar mes por nombre en el texto
    mes_detectado = detectar_mes(text)
    if mes_detectado:
        mes_actual = mes_detectado
        for i in range(1, 13):
            nombre = calendar.month_name[i].lower()
            sin_acentos = nombre.translate(str.maketrans("áéíóú", "aeiou"))
            text = text.replace(nombre, "")
            text = text.replace(sin_acentos, "")

    # Limpiar texto para filtro libre
    text = re.sub(r"\s+", " ", text).strip().lower()

    # Filtrar por mes y año
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

    # Filtrar si se indicó texto (por responsable o ciudad)
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
    responsables = [r.get("Sales", "").strip() for r in data if r.get("Sales")]
    ciudades = [r.get("Class", "").split(":")[1].strip() for r in data if ":" in r.get("Class", "")]

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
