# main.py
import os, re, csv, io, requests
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

# ====== ENV (con strip para evitar saltos ocultos) ======
VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN") or "").strip()
WA_TOKEN     = (os.getenv("WA_ACCESS_TOKEN") or "").strip()
WA_PHONE_ID  = (os.getenv("WA_PHONE_NUMBER_ID") or "").strip()

# ====== Config ======
TOP_K = int(os.getenv("TOP_K", "3"))  # Cambia en Render a 2 o 3 según prefieras

HUBSPOT_TOKEN         = (os.getenv("HUBSPOT_TOKEN") or "").strip()  # Private App
GOOGLE_SHEET_CSV_URL  = (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()  # CSV público

# ====== Estado simple en memoria (MVP) ======
SESSIONS = {}  # { phone: {"lang":"ES/EN","step":"...","name":"","email":"","service_type":...} }

# ====== Helpers WhatsApp ======
def wa_send_text(to: str, body: str):
    phone_id = (WA_PHONE_ID or "").strip()
    url = f"https://graph.facebook.com/v23.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {(WA_TOKEN or '').strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print(f"WA send -> {r.status_code} to={to} len={len(body)} resp={r.text[:180]}")
    if r.status_code == 401:
        print("⚠️ WA TOKEN INVALID/EXPIRED. Revisa WA_ACCESS_TOKEN en Render.")
    if r.status_code == 400:
        print(f"⚠️ BAD REQUEST. phone_id={repr(phone_id)}")
    return r.status_code

def extract_text(m: dict) -> str:
    t = (m.get("type") or "").lower()
    if t == "text":
        return ((m.get("text") or {}).get("body") or "").strip()
    if t == "button":
        return ((m.get("button") or {}).get("text") or "").strip()
    if t == "interactive":
        inter = m.get("interactive") or {}
        if inter.get("type") == "button_reply":
            return ((inter.get("button_reply") or {}).get("title") or "").strip()
        if inter.get("type") == "list_reply":
            return ((inter.get("list_reply") or {}).get("title") or "").strip()
    return ""  # sticker/imagen/audio/etc.

# ====== Helpers HubSpot ======
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def hubspot_upsert_contact(name: str, email: str, phone: str, lang: str):
    if not HUBSPOT_TOKEN:
        print("WARN: HUBSPOT_TOKEN missing")
        return False

    base = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    props = {
        "email": email,
        "firstname": (name.split()[0] if name else None),
        "lastname": (" ".join(name.split()[1:]) if name and len(name.split()) > 1 else None),
        "phone": phone,
        "hs_lead_status": "NEW",
        "lifecyclestage": "lead",
        "preferred_language": ("es" if (lang or "ES").upper().startswith("ES") else "en"),
        "source": "WhatsApp Bot",
    }

    # 1) create
    r = requests.post(base, headers=headers, json={"properties": props}, timeout=20)
    if r.status_code == 201:
        print("HubSpot contact created", r.json().get("id"))
        return True

    # 2) if conflict, update
    if r.status_code == 409:
        search_url = f"{base}/search"
        payload = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email
                }]
            }],
            "properties": ["email"]
        }
        s = requests.post(search_url, headers=headers, json=payload, timeout=20)
        if s.ok and s.json().get("results"):
            cid = s.json()["results"][0]["id"]
            up = requests.patch(f"{base}/{cid}", headers=headers, json={"properties": props}, timeout=20)
            print("HubSpot update:", up.status_code, up.text[:150])
            return up.ok

    print("HubSpot upsert error:", r.status_code, r.text[:200])
    return False

# ====== Helpers Google Sheet ======
def load_catalog():
    if not GOOGLE_SHEET_CSV_URL:
        print("WARN: GOOGLE_SHEET_CSV_URL missing")
        return []

    r = requests.get(GOOGLE_SHEET_CSV_URL, timeout=30)
    if not r.ok:
        print("Catalog download error:", r.status_code, r.text[:200])
        return []

    rows = []
    content = r.content.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    print("Catalog rows:", len(rows))
    return rows

def find_top(service: str, city: str, pax: int, prefs: str, top_k: int = TOP_K):
    service = (service or "").strip().lower()
    city    = (city or "").strip().lower()
    prefs_l = [p.strip().lower() for p in (prefs or "").split(",") if p.strip()]

    rows = load_catalog()
    if not rows:
        return []

    def row_ok(r):
        if (r.get("service_type","").lower() != service):
            return False
        if city and (r.get("city","").lower() != city):
            return False
        try:
            cap = int(float(r.get("capacity_max","0") or "0"))
        except:
            cap = 0
        if pax and cap < pax:
            return False
        if prefs_l:
            tags = [t.strip().lower() for t in (r.get("preference_tags","") or "").split(",") if t.strip()]
            if not any(p in tags for p in prefs_l):
                return False
        return True

    filtered = [r for r in rows if row_ok(r)]

    def price_val(r):
        try:
            return float(r.get("price_from_usd","999999") or "999999")
        except:
            return 999999.0

    filtered.sort(key=price_val)
    return filtered[:max(1, int(top_k or 1))]

# ====== Mensajes y helpers de copy (bilingüe) ======
def is_es(lang: str) -> bool:
    return (lang or "ES").upper().startswith("ES")

def opener_bi():
    return (
        "ES: ¡Hola! Soy tu concierge virtual de TWOTRAVEL 🛎️✨. Estoy aquí para ayudarte con villas, botes, islas, bodas/eventos y concierge. ¿En qué idioma prefieres continuar?\n\n"
        "EN: Hi! I’m your TWOTRAVEL virtual concierge 🛎️✨. I can help with villas, boats, islands, weddings/events and concierge. Which language would you prefer?"
    )

def ask_name_again(lang: str):
    return "¿Me confirmas tu *nombre y apellido*?" if is_es(lang) else "Could you share *name and last name*?"

def ask_email_again(lang: str):
    return "Ese correo no parece válido, ¿puedes revisarlo?" if is_es(lang) else "That email looks invalid, mind checking it?"

def main_menu(lang: str):
    return (
        "Genial. ¿Qué necesitas hoy?\n"
        "• *Villas & Casas* 🏠\n"
        "• *Botes & Yates* 🚤\n"
        "• *Islas Privadas* 🏝️\n"
        "• *Bodas & Eventos* 💍🎉\n"
        "• *Concierge* ✨\n"
        "• *Hablar con ventas* 👤"
        if is_es(lang) else
        "Great. What do you need today?\n"
        "• *Villas & Homes* 🏠\n"
        "• *Boats & Yachts* 🚤\n"
        "• *Private Islands* 🏝️\n"
        "• *Weddings & Events* 💍🎉\n"
        "• *Concierge* ✨\n"
        "• *Talk to sales* 👤"
    )

def q_villas_city(lang):  return "¿En qué *ciudad* buscas? (Cartagena / Medellín / Tulum / CDMX)" if is_es(lang) else "Which *city*?"
def q_villas_dates(lang): return "¿Fechas de *check-in y check-out*? (YYYY-MM-DD)" if is_es(lang) else "Check-in and check-out dates? (YYYY-MM-DD)"
def q_villas_pax(lang):   return "¿Para cuántas *personas*?" if is_es(lang) else "How many *guests*?"
def q_villas_prefs(lang): return "¿Alguna *preferencia*? Frente al mar / Centro histórico / Zona exclusiva / Cualquiera" if is_es(lang) else "Any *preference*? Oceanfront / Historic center / Exclusive area / No preference"

def q_boats_city(lang):   return "¿Ciudad/puerto de salida? (Solo *Cartagena*)" if is_es(lang) else "City/port of departure? (Cartagena)"
def q_boats_date(lang):   return "¿*Fecha* del paseo? (YYYY-MM-DD; *día o noche*?)" if is_es(lang) else "Trip *date*? (YYYY-MM-DD; *day or night*?)"
def q_boats_pax(lang):    return "¿Número de *pasajeros*?" if is_es(lang) else "Number of *passengers*?"
def q_boats_type(lang):   return "Tipo: *Lancha* / *Yate* / *Catamarán*. ¿Tipo de tour? *Cholón*, *Islas del Rosario*, etc." if is_es(lang) else "Type: *Speedboat* / *Yacht* / *Catamaran*. Tour type: *Cholón*, *Rosario Islands*, etc."

def q_wed_city(lang):     return "Ciudad y *fecha aproximada* / *# invitados* / Tipo de *venue* (playa, histórico, finca, moderno) / ¿*Full planning*?" if is_es(lang) else "City & *approx date* / *guest count* / *venue* type (beach, historic, estate, modern) / *Full planning*?"
def r_wed_estimate(lang): return ("Con esa información preparo un *estimado* según venue y servicios. ¿Te conecto con nuestro equipo de *Weddings* para afinar propuesta y agenda de visitas?" if is_es(lang) else "We’ll prepare an *estimate* based on venue and services. Connect with *Weddings* to refine proposal and site visits?")

def q_concierge(lang):    return "Ciudad / Fechas / Servicios (reservas, transporte, chef, seguridad, experiencias privadas)." if is_es(lang) else "City / Dates / Services (reservations, transport, private chef, security, private experiences)."
def r_concierge(lang):    return ("Servicio 100% personalizado. *Desde* USD estimado por persona por viaje. *Ventas* confirma el valor final según agenda y servicios. ¿Te conecto con ventas?" if is_es(lang) else "100% personalized service. *From* an estimated USD per person per trip. *Sales* will confirm final pricing. Connect with sales?")

def reply_topN(lang: str, items: list, unit: str = "noche"):
    if not items:
        return ("No veo opciones con esos filtros. ¿Quieres que intente con *fechas cercanas (±3 días)* o ajustar el *tamaño del grupo*?"
                if is_es(lang) else
                "I couldn’t find matches. Try *nearby dates (±3 days)* or adjust *party size*?")
    es = is_es(lang)
    lines = []
    if es:
        lines.append(f"Estas son nuestras mejores {len(items)} opción(es) (precios *desde*):")
        for r in items:
            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} pax) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("La *disponibilidad final* la confirma nuestro equipo de *ventas* antes de reservar. ¿Te conecto con ventas para confirmación y cotización final?")
    else:
        lines.append(f"Here are the top {len(items)} option(s) (*prices from*):")
        for r in items:
            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} guests) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("Final *availability* is confirmed by our *sales* team before booking. Connect with sales?")
    return "\n".join(lines)

def add_another_or_sales(lang: str):
    return ("¿Quieres *cotizar otro servicio* además de este?\n• *Añadir otro servicio*  \n• *Conectar con ventas*"
            if is_es(lang) else
            "Do you want to *quote another service* as well?\n• *Add another service*  \n• *Connect with sales*")

def handoff_client(lang: str, owner_name: str, team: str):
    return (f"Te conecto con [{owner_name} – Ventas {team}] para confirmar *disponibilidad* y cerrar la *reserva*."
            if is_es(lang) else
            f"Connecting you with [{owner_name} – Sales {team}] to confirm *availability* and finalize the *booking*.")

# ====== Prompts cortos para captura paso a paso ======
def ask_contact(lang: str):
    return (
        "Para enviarte opciones y una cotización personalizada, necesito tus datos:\n"
        " 📛 *Nombre completo:*\n"
        " _(Luego te pido el correo)_"
        if is_es(lang)
        else
        "To share options and a personalized quote, I’ll need your details:\n"
        " 📛 *Full name:*\n"
        " _(I’ll ask your email next)_"
    )

def ask_email(lang: str):
    return "📧 *Correo electrónico:*" if is_es(lang) else "📧 *Email address:*"

def valid_name(fullname: str) -> bool:
    return len((fullname or "").split()) >= 2

# ====== Startup logs ======
@app.on_event("startup")
async def show_routes():
    print("BOOT> Routes:", [r.path for r in app.router.routes])
    print("BOOT> WA_PHONE_ID:", repr(WA_PHONE_ID))
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

# ====== Health root ======
@app.get("/")
def root():
    return {"ok": True, "routes": [r.path for r in app.router.routes]}

# ====== Webhook Verify (GET) ======
@app.get("/wa-webhook")
async def verify(req: Request):
    mode = req.query_params.get("hub.mode")
    token = req.query_params.get("hub.verify_token")
    challenge = req.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)  # 👈 TEXTO PLANO
    return PlainTextResponse("forbidden", status_code=403)

# ====== Webhook Incoming (POST) ======
@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", data)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Estados de delivery (sent/delivered/read) → ignorar
            if value.get("statuses"):
                continue

            for m in value.get("messages", []):
                user = m.get("from")
                if not user:
                    continue

                text = extract_text(m)
                state = SESSIONS.setdefault(user, {"step": "lang", "lang": "ES"})

                # 0) Inicio: idioma
                if state["step"] == "lang":
                    low = (text or "").strip().lower()
                    if low in ("es","español","1"):
                        state["lang"] = "ES"
                    elif low in ("en","english","2"):
                        state["lang"] = "EN"
                    else:
                        wa_send_text(user, opener_bi())
                        continue
                    state["step"] = "contact_name"
                    wa_send_text(user, ask_contact(state["lang"]))
                    continue

                # 1) Captura nombre
                if state["step"] == "contact_name":
                    if not valid_name(text):
                        wa_send_text(user, ask_name_again(state["lang"]))
                        continue
                    state["name"] = text
                    state["step"] = "contact_email"
                    wa_send_text(user, ask_email(state["lang"]))
                    continue

                # 2) Captura email
                if state["step"] == "contact_email":
                    if not EMAIL_RE.match(text or ""):
                        wa_send_text(user, ask_email_again(state["lang"]))
                        continue
                    state["email"] = text

                    # HubSpot upsert (no bloquea flujo si falla)
                    try:
                        hubspot_upsert_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                    except Exception as e:
                        print("HubSpot error:", e)

                    state["step"] = "menu"
                    wa_send_text(user, main_menu(state["lang"]))
                    continue

                # 3) Menú → enrutar servicio
                if state["step"] == "menu":
                    t = (text or "").strip().lower()
                    if any(k in t for k in ("villas","villa","casas","homes","home")):
                        state["service_type"] = "villas"
                        state["step"] = "villas_city"
                        wa_send_text(user, q_villas_city(state["lang"]))
                        continue
                    if any(k in t for k in ("botes","yates","boats","yachts","lancha","catamarán","catamaran")):
                        state["service_type"] = "boats"
                        state["step"] = "boats_city"
                        wa_send_text(user, q_boats_city(state["lang"]))
                        continue
                    if any(k in t for k in ("islas","island","islands","private islands")):
                        state["service_type"] = "islands"
                        state["step"] = "villas_city"  # reutiliza preguntas de villas
                        wa_send_text(user, q_villas_city(state["lang"]))
                        continue
                    if any(k in t for k in ("bodas","eventos","weddings","events")):
                        state["service_type"] = "weddings"
                        state["step"] = "weddings_form"
                        wa_send_text(user, q_wed_city(state["lang"]))
                        continue
                    if any(k in t for k in ("concierge","conserje","experiencias","experiences")):
                        state["service_type"] = "concierge"
                        state["step"] = "concierge_form"
                        wa_send_text(user, q_concierge(state["lang"]))
                        continue
                    if any(k in t for k in ("venta","ventas","sales","talk to sales","hablar con ventas")):
                        state["step"] = "handoff"
                        owner_name, team = "Laura", "TwoTravel"
                        wa_send_text(user, handoff_client(state["lang"], owner_name, team))
                        continue
                    # fuera de flujo → re-mostrar menú
                    wa_send_text(user, main_menu(state["lang"]))
                    continue

                # ===== VILLAS / ISLAS =====
                if state["step"] == "villas_city":
                    state["city"] = (text or "")
                    state["step"] = "villas_dates"
                    wa_send_text(user, q_villas_dates(state["lang"]))
                    continue

                if state["step"] == "villas_dates":
                    state["dates"] = (text or "")
                    state["step"] = "villas_pax"
                    wa_send_text(user, q_villas_pax(state["lang"]))
                    continue

                if state["step"] == "villas_pax":
                    try:
                        state["pax"] = int(re.sub(r"[^\d]", "", text or "") or "0")
                    except:
                        state["pax"] = 0
                    state["step"] = "villas_prefs"
                    wa_send_text(user, q_villas_prefs(state["lang"]))
                    continue

                if state["step"] == "villas_prefs":
                    state["prefs"] = (text or "")

                    # Si no hay catálogo configurado
                    if not GOOGLE_SHEET_CSV_URL:
                        wa_send_text(user, "⚠️ Aún no tengo el catálogo conectado. Te puedo conectar con *ventas* para una cotización manual.")
                        state["step"] = "post_results"
                        continue

                    svc = "villas" if state.get("service_type") in ("villas","islands","islas") else state.get("service_type")
                    top = find_top(
                        service=svc or "villas",
                        city=(state.get("city") or ""),
                        pax=int(state.get("pax") or 0),
                        prefs=(state.get("prefs") or ""),
                        top_k=TOP_K
                    )
                    unit_villas = "noche" if is_es(state["lang"]) else "night"
                    wa_send_text(user, reply_topN(state["lang"], top, unit=unit_villas))
                    state["step"] = "post_results"
                    continue

                # ===== BOATS & YACHTS =====
                if state["step"] == "boats_city":
                    state["city"] = (text or "Cartagena")
                    state["step"] = "boats_date"
                    wa_send_text(user, q_boats_date(state["lang"]))
                    continue

                if state["step"] == "boats_date":
                    state["date"] = (text or "")
                    state["step"] = "boats_pax"
                    wa_send_text(user, q_boats_pax(state["lang"]))
                    continue

                if state["step"] == "boats_pax":
                    try:
                        state["pax"] = int(re.sub(r"[^\d]", "", text or "") or "0")
                    except:
                        state["pax"] = 0
                    state["step"] = "boats_type"
                    wa_send_text(user, q_boats_type(state["lang"]))
                    continue

                if state["step"] == "boats_type":
                    state["boat_type"] = (text or "")

                    # Si no hay catálogo configurado
                    if not GOOGLE_SHEET_CSV_URL:
                        wa_send_text(user, "⚠️ Aún no tengo el catálogo conectado. Te puedo conectar con *ventas* para una cotización manual.")
                        state["step"] = "post_results"
                        continue

                    top = find_top(
                        service="boats",
                        city=(state.get("city") or "cartagena"),
                        pax=int(state.get("pax") or 0),
                        prefs=(state.get("boat_type") or ""),
                        top_k=TOP_K
                    )
                    unit_boats = "día" if is_es(state["lang"]) else "day"
                    wa_send_text(user, reply_topN(state["lang"], top, unit=unit_boats))
                    state["step"] = "post_results"
                    continue

                # ===== WEDDINGS & EVENTS =====
                if state["step"] == "weddings_form":
                    state["weddings_info"] = (text or "")
                    wa_send_text(user, r_wed_estimate(state["lang"]))
                    state["step"] = "post_results"
                    continue

                # ===== CONCIERGE =====
                if state["step"] == "concierge_form":
                    state["concierge_info"] = (text or "")
                    wa_send_text(user, r_concierge(state["lang"]))
                    state["step"] = "post_results"
                    continue

                # ===== POST-RESULTS: ofrecer más o ventas =====
                if state["step"] == "post_results":
                    t = (text or "").lower()
                    if ("otro" in t) or ("add" in t) or ("another" in t):
                        state["step"] = "menu"
                        wa_send_text(user, main_menu(state["lang"]))
                        continue
                    if ("venta" in t) or ("sales" in t) or ("conectar" in t) or ("connect" in t)):
                        state["step"] = "handoff"
                        owner_name, team = "Laura", "TwoTravel"
                        wa_send_text(user, handoff_client(state["lang"], owner_name, team))
                        continue
                    wa_send_text(user, add_another_or_sales(state["lang"]))
                    continue

    return {"ok": True}
