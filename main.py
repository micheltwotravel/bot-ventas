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

# Configuración de Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = os.getenv("SHEET_NAME", "D6 Tracking")
TAB_NAME = os.getenv("TAB_NAME", "Quickbooks")

# Aliases para unificar nombres
alias = {
    "sofia millan wedding": "sofia milan",
    "sofia millan": "sofia milan",
    "sofía milan": "sofia milan",
}

def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto or "") if unicodedata.category(c) != 'Mn').lower().strip()

def normalizar_nombre(nombre):
    return alias.get(normalizar(nombre), normalizar(nombre))

def meses_inv(mes_num):
    meses = ["", "enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
    return meses[mes_num].capitalize() if mes_num else ""

def resumen_individual(data, rep):
    data_rep = [r for r in data if normalizar_nombre(r.get("Sales","")) == normalizar_nombre(rep)]
    deals = len(data_rep)
    total = sum(float(r.get("Amount",0)) for r in data_rep)
    return f"*{rep.title()}*: {deals} deals, ${total:,.0f}"

def filtrar_y_resumir(text):
    sheet = client.open(SHEET_NAME).worksheet(TAB_NAME)
    rows = sheet.get_all_records()

    text_original = text or ""
    text = normalizar(text_original.strip()) if text else ""

    # Año
    year = datetime.now().year
    ym = re.search(r"(20\d{2})", text)
    if ym:
        year = int(ym.group(1))
        text = text.replace(ym.group(1),"").strip()

    # Mes
    meses = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
    mes = None
    for m,n in meses.items():
        if m in text:
            mes = n
            text = text.replace(m,"").strip()
            break

    periodo = f"{meses_inv(mes)} {year}" if mes else str(year)

    # Comando: por mes
    if text_original.lower().startswith("por mes"):
        stats = {m:{'deals':0,'amount':0} for m in range(1,13)}
        for r in rows:
            try:
                d = parse(r.get("Date",""))
                if d.year == year:
                    stats[d.month]['deals'] +=1
                    stats[d.month]['amount'] += float(r.get("Amount",0))
            except:
                continue
        lines = [f"• {meses_inv(m)}: {v['deals']} deals, ${v['amount']:,.0f}" for m,v in stats.items() if v['deals']>0]
        return f"📈 Ventas por mes – {periodo}\n" + "\n".join(lines)

    # Comando: top ciudades
    if "top ciudades" in text_original.lower():
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
            ciudades.setdefault(c,{'deals':0,'amount':0})
            ciudades[c]['deals']+=1
            ciudades[c]['amount']+= float(r.get("Amount",0))
        if not ciudades:
            return f"No se encontraron ciudades con ventas en {periodo}."
        topc = sorted(ciudades.items(), key=lambda x:x[1]['amount'], reverse=True)
        lines = [f"{i+1}. {c} — {v['deals']} deals, ${v['amount']:,.0f}" for i,(c,v) in enumerate(topc)]
        return f"🏙️ *Top ciudades por ventas – {periodo}*\n" + "\n".join(lines)

    # Filtrado normal
    data = []
    for r in rows:
        try:
            d = parse(r.get("Date",""))
            if d.year == year and (mes is None or d.month == mes):
                data.append(r)
        except:
            continue

    if text.strip() == "todos":
        reps = sorted(set(normalizar_nombre(r.get("Sales","")) for r in data if r.get("Sales")))
        resumenes = [resumen_individual(data, rep) for rep in reps]
        return f"*📊 Ventas por responsable – {periodo}*\n\n" + "\n".join(resumenes)

    if text:
        data = [r for r in data if text in normalizar(normalizar_nombre(r.get("Sales","")))]

    if not data:
        return f"No se encontraron resultados para *{text_original}* en {periodo}."

    deals = len(data)
    amount = sum(float(r.get("Amount",0)) for r in data)
    reps = [normalizar_nombre(r.get("Sales","")) for r in data if r.get("Sales")]
    ciudades = [r.get("Class","").split(":")[-1].strip() for r in data if r.get("Class")]

    top_rep = Counter(reps).most_common(1)[0][0].title() if reps else "N/A"
    top_ciudad = Counter(ciudades).most_common(1)[0][0].title() if ciudades else "N/A"

    return f"""📊 *Resumen de ventas – {periodo}*
• Deals: *{deals}*
• Monto total estimado: *${amount:,.0f}*
• Responsable top: *{top_rep}*
• Ciudad top: *{top_ciudad}*"""

def procesar_y_responder(response_url, text):
    resp = filtrar_y_resumir(text)
    WebhookClient(response_url).send(text=resp)

@app.post("/slack/ventas")
async def ventas(background_tasks: BackgroundTasks, response_url: str = Form(...), text: str = Form("")):
    background_tasks.add_task(procesar_y_responder, response_url, text)
    return Response(status_code=200)
