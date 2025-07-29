from fastapi import FastAPI, Form
from slack_sdk.webhook import WebhookClient
from dotenv import load_dotenv
import os, gspread, re
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from dateutil.parser import parse

load_dotenv()
app = FastAPI()

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

def cargar_datos():
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    return sheet.get_all_records()

def filtrar_mes_actual(rows):
    ahora = datetime.now()
    data = []
    for r in rows:
        try:
            fecha = r.get("Date", "")
            if not fecha: continue
            fecha_obj = parse(fecha)
            if fecha_obj.year == ahora.year and fecha_obj.month == ahora.month:
                r["parsed_date"] = fecha_obj
                data.append(r)
        except: continue
    return data

def top(lista):
    return max(set(lista), key=lista.count) if lista else "N/A"

def resumen_general(data):
    deals = len(data)
    total = sum(float(r.get("Amount", 0)) for r in data)
    promedio = total / deals if deals else 0
    mayor = max([float(r.get("Amount", 0)) for r in data], default=0)

    reps = [r["Rep"] for r in data if r.get("Rep")]
    ciudades = [r["Class"].split(":")[1] for r in data if "Class" in r and ":" in r["Class"]]

    resumen = f"""
📊 *Resumen de ventas - {datetime.now().strftime('%B %Y')}*

• Deals: *{deals}*
• Monto total estimado: *${total:,.0f}*
• Ticket promedio: *${promedio:,.0f}*
• Mayor ticket: *${mayor:,.0f}*

👥 Top reps:
{formato_top(reps)}

📍 Top ciudades:
{formato_top(ciudades)}
""".strip()
    return resumen

def resumen_por_rep(data, rep):
    data = [r for r in data if rep in str(r.get("Rep", "")).lower()]
    if not data:
        return f"No se encontraron resultados para *{rep}* este mes."

    deals = len(data)
    total = sum(float(r.get("Amount", 0)) for r in data)
    promedio = total / deals if deals else 0
    mayor = max([float(r.get("Amount", 0)) for r in data], default=0)
    fechas = [r["parsed_date"] for r in data]
    ultima = max(fechas).strftime("%-d de %B") if fechas else "N/A"
    ciudades = [r["Class"].split(":")[1] for r in data if "Class" in r and ":" in r["Class"]]
    canales = [r["Sales"] for r in data if r.get("Sales")]

    resumen = f"""
📌 *Resumen para {rep.title()} - {datetime.now().strftime('%B %Y')}*

• Deals cerrados: *{deals}*
• Total estimado: *${total:,.0f}*
• Ticket promedio: *${promedio:,.0f}*
• Última venta: *{ultima}*
• Ciudades vendidas: *{", ".join(set(ciudades)) or "N/A"}*
• Canal más usado: *{top(canales)}*
""".strip()
    return resumen

def formato_top(lista):
    conteo = {}
    for item in lista:
        if not item: continue
        conteo[item] = conteo.get(item, 0) + 1
    ordenado = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:3]
    return "\n".join([f"{i+1}. {k} ({v})" for i, (k, v) in enumerate(ordenado)]) if ordenado else "N/A"

@app.post("/slack/ventas")
async def ventas(response_url: str = Form(...), text: str = Form("")):
    rows = cargar_datos()
    data = filtrar_mes_actual(rows)
    text = text.strip().lower()

    resumen = resumen_general(data) if not text else resumen_por_rep(data, text)

    webhook = WebhookClient(response_url)
    webhook.send(text=resumen)
    return {"status": "ok"}
