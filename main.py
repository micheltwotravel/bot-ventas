from fastapi import FastAPI, Form, BackgroundTasks
from fastapi.responses import Response
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

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

# Alias para unificar nombres
alias = {
    "sofia millan wedding": "sofia milan",
    "sofia millan": "sofia milan",
    "sofía milan": "sofia milan"
}

def normalizar(texto):
    return ''.join(
        c for c in unicodedata.normalize('NFD', texto or "") 
        if unicodedata.category(c) != 'Mn'
    ).lower().strip()

def normalizar_nombre(nombre):
    n = normalizar(nombre)
    return alias.get(n, n)

def meses_inv(mes_num):
    meses = ["", "enero","febrero","marzo","abril","mayo","junio",
             "julio","agosto","septiembre","octubre","noviembre","diciembre"]
    return meses[mes_num].capitalize() if mes_num else ""

def resumen_individual(data, rep):
    items = [r for r in data if normalizar_nombre(r.get("Sales","")) == normalizar_nombre(rep)]
    deals = len(items)
    total = sum(float(r.get("Amount",0)) for r in items)
    return f"*{rep.title()}*: {deals} deals, ${total:,.0f}"

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()
    
    text_orig = text or ""
    t = normalizar(text_orig.strip()) if text_orig else ""
    
    # Detectar año
    year = datetime.now().year
    m = re.search(r"(20\d{2})", t)
    if m:
        year = int(m.group(1))
        t = t.replace(m.group(1), "").strip()
    
    # Detectar mes
    meses_map = {
        "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
        "julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12
    }
    mes = None
    for name, num in meses_map.items():
        if name in t:
            mes = num
            t = t.replace(name, "").strip()
            break
    
    period_label = f"{meses_inv(mes)} {year}" if mes else str(year)
    
    # Comando: por mes
    if text_orig.lower().startswith("por mes"):
        stats = {i:{"deals":0,"amount":0} for i in range(1,13)}
        for r in rows:
            try:
                d = parse(r.get("Date",""))
                if d.year == year:
                    stats[d.month]["deals"] += 1
                    stats[d.month]["amount"] += float(r.get("Amount",0))
            except:
                continue
        lines = [
            f"• {meses_inv(m)}: {v['deals']} deals, ${v['amount']:,.0f}"
            for m,v in stats.items() if v["deals"]>0
        ]
        return f"📈 Ventas por mes – {period_label}\n" + "\n".join(lines)
    
    # Comando: top ciudades
    if "top ciudades" in text_orig.lower():
        data = []
        for r in rows:
            try:
                d = parse(r.get("Date",""))
                if d.year == year and (mes is None or d.month == mes):
                    data.append(r)
            except:
                continue
        ciudades = {}
        for r in data:
            c = r.get("Class","").split(":")[-1].strip()
            if not c: continue
            info = ciudades.setdefault(c, {"deals":0,"amount":0})
            info["deals"] += 1
            info["amount"] += float(r.get("Amount",0))
        if not ciudades:
            return f"No se encontraron ciudades con ventas en {period_label}."
        orden = sorted(ciudades.items(), key=lambda x: x[1]["amount"], reverse=True)
        lines = [
            f"{i+1}. {c} — {info['deals']} deals, ${info['amount']:,.0f}"
            for i,(c,info) in enumerate(orden)
        ]
        return f"🏙️ *Top ciudades por ventas – {period_label}*\n" + "\n".join(lines)
    
    # Flujo normal: filtrar filas por año/mes
    data = []
    for r in rows:
        try:
            d = parse(r.get("Date",""))
            if d.year == year and (mes is None or d.month == mes):
                data.append(r)
        except:
            continue
    
    # Comando: todos
    if t.strip() == "todos":
        reps = sorted(set(normalizar_nombre(r.get("Sales","")) for r in data if r.get("Sales")))
        lines = [resumen_individual(data, rep) for rep in reps]
        return f"*📊 Ventas por responsable – {period_label}*\n\n" + "\n".join(lines)
    
    # Comando: filtro por responsable
    if t:
        data = [
            r for r in data if t in normalizar(normalizar_nombre(r.get("Sales","")))
        ]
    
    if not data:
        return f"No se encontraron resultados para *{text_orig}* en {period_label}."
    
    deals = len(data)
    total = sum(float(r.get("Amount",0)) for r in data)
    reps = [normalizar_nombre(r.get("Sales","")) for r in data if r.get("Sales")]
    ciudades = [r.get("Class","").split(":")[-1].strip() for r in data if r.get("Class")]
    top_rep = Counter(reps).most_common(1)[0][0].title() if reps else "N/A"
    top_ciudad = Counter(ciudades).most_common(1)[0][0].title() if ciudades else "N/A"
    
    return (
        f"📊 *Resumen de ventas – {period_label}*\n"
        f"• Deals: *{deals}*\n"
        f"• Monto total estimado: *${total:,.0f}*\n"
        f"• Responsable top: *{top_rep}*\n"
        f"• Ciudad top: *{top_ciudad}*"
    )

def procesar_y_responder(response_url, text):
    resp = filtrar_y_resumir(text)
    WebhookClient(response_url).send(text=resp)

@app.post("/slack/ventas")
async def ventas(background_tasks: BackgroundTasks, response_url: str = Form(...), text: str = Form("")):
    background_tasks.add_task(procesar_y_responder, response_url, text)
    return Response(status_code=200)
