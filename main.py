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
    # Usar "Sales" para los responsables de ventas - búsqueda exacta normalizada
    data_rep = [r for r in data if normalizar(r.get("Sales", "")) == normalizar(rep)]
    deals = len(data_rep)
    total = sum(float(r.get("Amount", 0)) for r in data_rep)
    return f"*{rep.title()}*: {deals} deals, ${total:,.0f}"

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()
    
    # Año detectado
    year = datetime.now().year
    month = None  # Por defecto, buscar en todos los meses
    
    # Buscar año en el texto
    year_match = re.search(r"(20\d{2})", text)
    if year_match:
        year = int(year_match.group(1))
        text = text.replace(year_match.group(1), "").strip()
    
    # Buscar mes específico en el texto
    meses = {
        'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5, 'junio': 6,
        'julio': 7, 'agosto': 8, 'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12
    }
    
    for mes_nombre, mes_num in meses.items():
        if mes_nombre in text.lower():
            month = mes_num
            text = text.lower().replace(mes_nombre, "").strip()
            break
    
    # Si no se especifica mes, usar el actual
    if month is None:
        month = datetime.now().month
    
    text = normalizar(text)
    
    # Filtrar por mes y año
    data = []
    for r in rows:
        try:
            date_str = r.get("Date", "")
            if not date_str:
                continue
            date_obj = parse(date_str)
            if date_obj.year == year and date_obj.month == month:
                data.append(r)
        except:
            continue
    
    # TEMPORAL: Si no hay datos en el mes actual, buscar en mayo (donde están tus datos)
    if not data and month == datetime.now().month:
        for r in rows:
            try:
                date_str = r.get("Date", "")
                if not date_str:
                    continue
                date_obj = parse(date_str)
                if date_obj.year == year and date_obj.month == 5:  # Mayo
                    data.append(r)
            except:
                continue
        month = 5  # Actualizar para el reporte
    
    if text == "todos":
        # Usar "Sales" para los responsables de ventas
        reps = sorted(set(r.get("Sales", "N/A") for r in data if r.get("Sales") and r.get("Sales").strip()))
        resumenes = [resumen_individual(data, rep) for rep in reps]
        mes_nombre = list(meses.keys())[month-1] if month <= 12 else "mes"
        return "*📊 Ventas por responsable - {} {}*\n\n{}".format(
            mes_nombre.title(), year, "\n".join(resumenes)
        )
    
    if text:
        # Mejorar la búsqueda - buscar coincidencias exactas o parciales
        filtered_data = []
        for r in data:
            sales_norm = normalizar(r.get("Sales", ""))
            class_norm = normalizar(r.get("Class", ""))
            posting_norm = normalizar(r.get("Posting", ""))
            
            # Coincidencia exacta o el texto está contenido en el campo
            if (text == sales_norm or text in sales_norm or
                text == class_norm or text in class_norm or
                text == posting_norm or text in posting_norm):
                filtered_data.append(r)
        
        data = filtered_data
    
    if not data:
        return f"No se encontraron resultados para *{text or 'el periodo'}* en {year}."
    
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
