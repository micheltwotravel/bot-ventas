# main.py
import os, re, csv, io, requests, datetime, smtplib
from email.mime.text import MIMEText
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

# ==================== ENV / CONFIG ====================
VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN") or "").strip()
WA_TOKEN     = (os.getenv("WA_ACCESS_TOKEN") or "").strip()
WA_PHONE_ID  = (os.getenv("WA_PHONE_NUMBER_ID") or "").strip()

# Bot
BOT_NAME = (os.getenv("BOT_NAME") or "Luna").strip()  # p.ej. "Luna"

# HubSpot
HUBSPOT_TOKEN       = (os.getenv("HUBSPOT_TOKEN") or "").strip()
HUBSPOT_OWNER_SOFIA = (os.getenv("HUBSPOT_OWNER_SOFIA") or "").strip()
HUBSPOT_OWNER_ROSS  = (os.getenv("HUBSPOT_OWNER_ROSS")  or "").strip()
HUBSPOT_OWNER_RAY   = (os.getenv("HUBSPOT_OWNER_RAY")   or "").strip()
HUBSPOT_PIPELINE_ID  = (os.getenv("HUBSPOT_PIPELINE_ID")  or "").strip()
HUBSPOT_DEALSTAGE_ID = (os.getenv("HUBSPOT_DEALSTAGE_ID") or "").strip()

# Calendarios
CAL_RAY   = (os.getenv("CAL_RAY")   or "https://meetings.hubspot.com/ray-kanevsky?uuid=280bb17d-4006-4bd1-9560-9cefa9752d5d").strip()
CAL_SOFIA = (os.getenv("CAL_SOFIA") or "https://marketing.two.travel/meetings/sofia217").strip()
CAL_ROSS  = (os.getenv("CAL_ROSS")  or "https://meetings.hubspot.com/ross334?uuid=68031520-950b-4493-b5ad-9cde268edbc8").strip()

# Catálogo
GOOGLE_SHEET_CSV_URL = (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()
TOP_K = int(os.getenv("TOP_K", "3"))

# Correo ventas (SMTP)
SMTP_HOST   = (os.getenv("SMTP_HOST") or "").strip()
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASS   = (os.getenv("SMTP_PASS") or "").strip()
SALES_EMAILS = [e.strip() for e in (os.getenv("SALES_EMAILS") or "").split(",") if e.strip()]

# Estado (MVP en memoria)
SESSIONS = {}      # { phone: {...} }
LAST_MSG_ID = {}   # { phone: last_msg_id }

# ==================== WhatsApp helpers ====================
def _post_graph(path: str, payload: dict):
    url = f"https://graph.facebook.com/v23.0/{path}"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print(f"WA -> {r.status_code} {r.text[:240]}")
    return r

def wa_send_text(to: str, body: str):
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}}
    return _post_graph(f"{WA_PHONE_ID}/messages", payload)

def wa_send_buttons(to: str, body_text: str, buttons: list):
    payload = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text": body_text},
            "action":{"buttons":[{"type":"reply","reply":b} for b in buttons[:3]]}
        }
    }
    return _post_graph(f"{WA_PHONE_ID}/messages", payload)

def wa_send_list(to: str, header_text: str, body_text: str, button_text: str, rows: list):
    payload = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"interactive",
        "interactive":{
            "type":"list",
            "header":{"type":"text","text": header_text},
            "body":{"text": body_text},
            "footer":{"text":"Two Travel"},
            "action":{
                "button": button_text,
                "sections":[{"title":"Select one","rows": rows[:10]}]
            }
        }
    }
    return _post_graph(f"{WA_PHONE_ID}/messages", payload)

def extract_text_or_reply(m: dict):
    t = (m.get("type") or "").lower()
    if t == "text":
        return ((m.get("text") or {}).get("body") or "").strip(), None
    if t == "interactive":
        inter = m.get("interactive") or {}
        if inter.get("type") == "button_reply":
            br = inter.get("button_reply") or {}
            return (br.get("title") or "").strip(), (br.get("id") or "").strip()
        if inter.get("type") == "list_reply":
            lr = inter.get("list_reply") or {}
            return (lr.get("title") or "").strip(), (lr.get("id") or "").strip()
    if t == "button":
        btn = (m.get("button") or {})
        return ((btn.get("text") or "").strip(), None)
    return "", None

# ==================== Email helper (ventas) ====================
def send_sales_email(subject: str, body: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SALES_EMAILS):
        print("EMAIL (noop)>", subject, "\n", body[:500])
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(SALES_EMAILS)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, SALES_EMAILS, msg.as_string())
        print("EMAIL sent to:", SALES_EMAILS)
        return True
    except Exception as e:
        print("EMAIL error:", e)
        return False

def notify_sales(event: str, state: dict, phone: str, extra: str = "", cal_url: str = "", owner_name: str = "", pretty_city: str = ""):
    name  = state.get("name") or "-"
    email = state.get("email") or "-"
    lang  = state.get("lang") or "-"
    svc   = state.get("service_type") or "-"
    city  = pretty_city or (state.get("city") or "-")
    date  = state.get("date") or state.get("wed_month") or "-"
    pax   = state.get("pax") or state.get("wed_guests") or "-"
    top   = state.get("last_top") or []
    tops  = "; ".join([f"{r.get('name')}→{r.get('url')}" for r in top[:TOP_K]]) if top else "-"
    lines = [
        f"Event: {event}",
        f"Service: {svc}",
        f"City: {city}",
        f"Date/Month: {date}",
        f"Pax/Guests: {pax}",
        f"Lang: {lang}",
        f"Contact name: {name}",
        f"Contact phone (WA): {phone}",
        f"Contact email: {email}",
        f"Owner: {owner_name or '-'}",
        f"Calendar: {cal_url or '-'}",
        f"Top shown: {tops}",
    ]
    if extra:
        lines.append(f"Extra: {extra}")
    subject = f"[Two Travel WA] {svc.title()} – {city} – {name}"
    body = "\n".join(lines)
    send_sales_email(subject, body)

# ==================== Catalog helpers ====================
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

def find_top_relaxed(service: str, city: str, pax: int, prefs: str, top_k: int = TOP_K):
    rows = load_catalog()
    if not rows:
        return []
    service = (service or "").strip().lower()
    city    = (city or "").strip().lower()

    def ok(r, use_service=True, use_city=True, use_pax=True):
        if use_service and service and (r.get("service_type","").lower() != service):
            return False
        if use_city and city and (r.get("city","").lower() != city):
            return False
        if use_pax:
            try:
                cap = int(float(r.get("capacity_max","0") or "0"))
            except:
                cap = 0
            if pax and cap < pax:
                return False
        return True

    attempts = [
        dict(use_service=True, use_city=True, use_pax=True),
        dict(use_service=True, use_city=True, use_pax=False),
        dict(use_service=True, use_city=False,use_pax=False),
        dict(use_service=False,use_city=True, use_pax=False),
        dict(use_service=False,use_city=False,use_pax=False),
    ]

    def price_val(r):
        try:
            return float(r.get("price_from_usd","999999") or "999999")
        except:
            return 999999.0

    for flags in attempts:
        cand = [r for r in rows if ok(r, **flags)]
        cand.sort(key=price_val)
        if cand:
            return cand[:max(1, int(top_k or 1))]
    return []

# ==================== Copy / UI ====================
def is_es(lang: str) -> bool:
    return (lang or "EN").upper().startswith("ES")

def welcome_text():
    return ("*Two Travel*\n"
            "Before we start, choose your language (so prices/dates show clearly).\n\n"
            "Elige tu idioma / Choose your language:")

def opener_buttons():
    return [
        {"id":"LANG_EN","title":"🇺🇸 English"},
        {"id":"LANG_ES","title":"🇪🇸 Español"},
    ]

def human_intro(lang):
    if is_es(lang):
        return f"¡Hola! Soy *{BOT_NAME}*, tu asistente de Two Travel. Estoy aquí para ayudarte a cotizar y organizar todo. ¿Cómo puedo ayudarte hoy?"
    else:
        return f"Hi! I’m *{BOT_NAME}*, your Two Travel assistant. I’ll help you plan and get quotes. How can I help you today?"

def ask_fullname(lang):
    return ("Por favor escribe tu *Nombre y Apellido*."
            if is_es(lang) else
            "Please type your *First and Last Name*.")

def ask_email(lang):
    return ("¿Quieres dejar tu correo? Puedes *Saltar* y continuar."
            if is_es(lang) else
            "Would you like to add your email? You can *Skip* and continue.")

def email_buttons(lang):
    return [
        {"id":"EMAIL_ENTER","title":("Ingresar email" if is_es(lang) else "Enter email")},
        {"id":"EMAIL_USE_WA","title":("Usar mi WhatsApp" if is_es(lang) else "Use my WhatsApp")},
        {"id":"EMAIL_SKIP","title":("Saltar" if is_es(lang) else "Skip")},
    ]

def main_menu_list(lang):
    header = "Two Travel"
    body   = ("Elige una opción:" if is_es(lang) else "Choose an option:")
    rows = [
        {"id":"SVC_VILLAS",   "title":"1) Villas & Homes 🏠",         "description":("Alojamiento premium" if is_es(lang) else "Premium stays")},
        {"id":"SVC_BOATS",    "title":"2) Boats & Yachts 🚤",         "description":("Días en el mar" if is_es(lang) else "Days at sea")},
        {"id":"SVC_ISLANDS",  "title":"3) Private Islands 🏝️",       "description":("Islas privadas" if is_es(lang) else "Private islands")},
        {"id":"SVC_WEDDINGS", "title":"4) Weddings & Events 💍🎉",    "description":("Bodas/Eventos" if is_es(lang) else "Weddings/Events")},
        {"id":"SVC_CONCIERGE","title":"5) Concierge ✨",              "description":("Plan a medida" if is_es(lang) else "Bespoke planning")},
        {"id":"SVC_TEAM",     "title":"6) Talk to the Team 👤",       "description":("Conecta con nuestro equipo" if is_es(lang) else "Connect with our team")},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def city_list(lang):
    header = ("Ciudad / City" if is_es(lang) else "City")
    body   = ("Elige la ciudad." if is_es(lang) else "Choose the city.")
    rows = [
        {"id":"CITY_CARTAGENA","title":"Cartagena","description":"Colombia"},
        {"id":"CITY_MEDELLIN", "title":"Medellín","description":"Colombia"},
        {"id":"CITY_TULUM",    "title":"Tulum","description":"Mexico"},
        {"id":"CITY_MXCITY",   "title":"Mexico City","description":"Mexico"},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def ask_date_text(lang):
    return ("Selecciona fecha aproximada (puedes elegir *No sé*)."
            if is_es(lang) else
            "Select an approximate date (you can choose *Don’t know*).")

def date_buttons(lang):
    return [
        {"id":"DATE_TODAY","title":("Hoy" if is_es(lang) else "Today")},
        {"id":"DATE_TOMORROW","title":("Mañana" if is_es(lang) else "Tomorrow")},
        {"id":"DATE_UNKNOWN","title":("No sé" if is_es(lang) else "Don’t know")},
    ]

def date_quick_list(lang):
    header = ("Fechas rápidas" if is_es(lang) else "Quick dates")
    body = ("Elige una opción:" if is_es(lang) else "Pick one:")
    rows = [
        {"id":"DATE_THIS_WEEKEND","title":("Este fin de semana" if is_es(lang) else "This weekend"),"description":""},
        {"id":"DATE_NEXT_WEEKEND","title":("Próximo fin de semana" if is_es(lang) else "Next weekend"),"description":""},
        {"id":"DATE_NEXT_MONTH","title":("Próximo mes" if is_es(lang) else "Next month"),"description":""},
        {"id":"DATE_TBD","title":"TBD","description":("A definir" if is_es(lang) else "To be defined")},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def pax_list(lang):
    header = ("Personas" if is_es(lang) else "Guests")
    body   = ("Elige un rango:" if is_es(lang) else "Choose a range:")
    rows = [
        {"id":"PAX_2_4","title":"2–4","description":""},
        {"id":"PAX_5_8","title":"5–8","description":""},
        {"id":"PAX_9_12","title":"9–12","description":""},
        {"id":"PAX_13_16","title":"13–16","description":""},
        {"id":"PAX_17_PLUS","title":"17+","description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def concierge_needs_list(lang):
    header = ("Necesidades" if is_es(lang) else "Needs")
    body   = ("¿Qué necesitas?" if is_es(lang) else "What do you need?")
    rows = [
        {"id":"CC_RESERVATIONS","title":("Reservas" if is_es(lang) else "Reservations"),"description":""},
        {"id":"CC_TRANSPORT","title":("Transporte" if is_es(lang) else "Transport"),"description":""},
        {"id":"CC_CHEF","title":("Chef privado" if is_es(lang) else "Private chef"),"description":""},
        {"id":"CC_SECURITY","title":("Seguridad" if is_es(lang) else "Security"),"description":""},
        {"id":"CC_EXPERIENCES","title":("Experiencias privadas" if is_es(lang) else "Private experiences"),"description":""},
        {"id":"CC_OTHER","title":("Otro / No estoy seguro" if is_es(lang) else "Other / Not sure"),"description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def weddings_month_list(lang):
    header = ("Fecha aproximada" if is_es(lang) else "Approx date")
    body   = ("Elige un mes aproximado:" if is_es(lang) else "Choose an approximate month:")
    today = datetime.date.today()
    rows = []
    for i in range(0, 9):  # WhatsApp list ~10 items
        base = today.replace(day=1)
        d = (base + datetime.timedelta(days=32*i)).replace(day=1)
        rows.append({"id": f"WED_MONTH_{d.year}_{d.month:02d}",
                     "title": f"{d.year}-{d.month:02d}",
                     "description": ""})
    rows.append({"id":"WED_MONTH_TBD","title":"TBD","description":("A definir" if is_es(lang) else "To be defined")})
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def weddings_guests_list(lang):
    header = ("Invitados" if is_es(lang) else "Guests")
    body   = ("Elige un rango de invitados:" if is_es(lang) else "Choose a guest range:")
    rows = [
        {"id":"WED_G_20_50","title":"20–50","description":""},
        {"id":"WED_G_51_80","title":"51–80","description":""},
        {"id":"WED_G_81_120","title":"81–120","description":""},
        {"id":"WED_G_121_200","title":"121–200","description":""},
        {"id":"WED_G_200_PLUS","title":"200+","description":""},
        {"id":"WED_G_UNKNOWN","title":("No sé" if is_es(lang) else "Don’t know"),"description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def reply_topN(lang: str, items: list, unit: str):
    if not items:
        return ("No veo opciones ahora mismo, te conecto con el equipo para una propuesta a medida."
                if is_es(lang) else
                "Couldn’t find matches now; I’ll connect you with the team for a bespoke proposal.")
    es = is_es(lang)
    lines = []
    if es:
        lines.append(f"Estas son nuestras mejores {len(items)} opción(es) (precios *desde*):")
        for r in items:
            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} pax) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("La *disponibilidad final* la confirma nuestro equipo antes de reservar.")
    else:
        lines.append(f"Here are the top {len(items)} option(s) (*prices from*):")
        for r in items:
            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} guests) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("Final *availability* is confirmed by our team before booking.")
    return "\n".join(lines)

def after_results_buttons(lang):
    return [
        {"id":"POST_ADD_SERVICE","title":("Añadir otro servicio" if is_es(lang) else "Add another service")},
        {"id":"POST_TALK_TEAM","title":("Hablar con el equipo" if is_es(lang) else "Talk to the team")},
        {"id":"POST_MENU","title":("Volver al menú" if is_es(lang) else "Back to menu")},
    ]

def handoff_text(lang, owner_name, cal_url, city):
    if is_es(lang):
        return (f"Te conecto con *{owner_name}* (equipo Two Travel – {city}) para confirmar disponibilidad.\n\n"
                f"Agenda aquí: {cal_url}")
    else:
        return (f"I’ll connect you with *{owner_name}* (Two Travel team – {city}) to confirm availability.\n\n"
                f"Schedule here: {cal_url}")

# ==================== HubSpot helpers ====================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def hubspot_find_or_create_contact(name: str, email: str, phone: str, lang: str):
    if not HUBSPOT_TOKEN:
        print("WARN: HUBSPOT_TOKEN missing")
        return None
    base = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    cid = None
    if email:
        s = requests.post(f"{base}/search", headers=headers, json={
            "filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}],
            "properties":["email"]
        }, timeout=20)
        if s.ok and s.json().get("results"):
            cid = s.json()["results"][0]["id"]

    props = {
        "email": email or None,
        "firstname": (name.split()[0] if name else None),
        "lastname": (" ".join(name.split()[1:]) if name and len(name.split()) > 1 else None),
        "phone": phone,
        "hs_lead_status": "NEW",
        "lifecyclestage": "lead",
        "preferred_language": ("es" if is_es(lang) else "en"),
        "source": "WhatsApp Bot",
    }

    if cid:
        up = requests.patch(f"{base}/{cid}", headers=headers, json={"properties": props}, timeout=20)
        print("HubSpot contact update:", up.status_code, up.text[:150])
        return cid if up.ok else None

    r = requests.post(base, headers=headers, json={"properties": props}, timeout=20)
    if r.status_code == 201:
        cid = r.json().get("id")
        print("HubSpot contact created", cid)
        return cid
    print("HubSpot contact error:", r.status_code, r.text[:200])
    return None

def owner_for_city(city: str):
    c = (city or "").strip().lower()
    if c in ("cartagena","ctg","tulum"):
        return ("Sofía", HUBSPOT_OWNER_SOFIA or None, CAL_SOFIA, "Cartagena/Tulum")
    if c in ("medellin","medellín"):
        return ("Ross", HUBSPOT_OWNER_ROSS or None, CAL_ROSS, "Medellín")
    if c in ("mexico city","mexico","méxico","cdmx","mxcity"):
        return ("Ray", HUBSPOT_OWNER_RAY or None, CAL_RAY, "Mexico City")
    return ("Two Travel Team", None, CAL_SOFIA, city or "—")

def hubspot_create_deal(contact_id, owner_id, title, desc):
    if not HUBSPOT_TOKEN:
        print("WARN: HUBSPOT_TOKEN missing")
        return None
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    base = "https://api.hubapi.com/crm/v3/objects/deals"
    props = {"dealname": title, "description": desc}
    if HUBSPOT_PIPELINE_ID:  props["pipeline"]  = HUBSPOT_PIPELINE_ID
    if HUBSPOT_DEALSTAGE_ID: props["dealstage"] = HUBSPOT_DEALSTAGE_ID
    if owner_id:             props["hubspot_owner_id"] = owner_id
    r = requests.post(base, headers=headers, json={"properties": props}, timeout=20)
    if not r.ok:
        print("HubSpot deal error:", r.status_code, r.text[:200])
        return None
    deal_id = r.json().get("id")
    try:
        assoc_url = f"https://api.hubapi.com/crm/v4/objects/deals/{deal_id}/associations/contacts/{contact_id}"
        a = requests.put(assoc_url, headers=headers, json=[{"associationCategory":"HUBSPOT_DEFINED","associationTypeId": 3}], timeout=20)
        print("Deal association:", a.status_code, a.text[:120])
    except Exception as e:
        print("Deal association error:", e)
    print("Deal created:", deal_id)
    return deal_id

# ==================== Validaciones / util ====================
def valid_name(fullname: str) -> bool:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return len(tokens) >= 2

def normalize_name(fullname: str) -> str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return " ".join(tokens[:3]).title()

def parse_quick_date(choice_id: str):
    today = datetime.date.today()
    if choice_id == "DATE_THIS_WEEKEND":
        days_ahead = (5 - today.weekday()) % 7  # Saturday
        return (today + datetime.timedelta(days=days_ahead)).isoformat()
    if choice_id == "DATE_NEXT_WEEKEND":
        days_ahead = (5 - today.weekday()) % 7
        return (today + datetime.timedelta(days=days_ahead + 7)).isoformat()
    if choice_id == "DATE_NEXT_MONTH":
        y = today.year + (1 if today.month == 12 else 0)
        m = 1 if today.month == 12 else today.month + 1
        return datetime.date(y, m, 1).isoformat()
    if choice_id in ("DATE_TBD","DATE_UNKNOWN"):
        return ""
    return None

def pax_from_id(pid: str) -> int:
    mapping = {
        "PAX_2_4": 4,
        "PAX_5_8": 8,
        "PAX_9_12": 12,
        "PAX_13_16": 16,
        "PAX_17_PLUS": 20,
    }
    return mapping.get(pid, 2)

# ==================== Startup / Health ====================
@app.on_event("startup")
async def show_routes():
    print("BOOT> Routes:", [r.path for r in app.router.routes])
    print("BOOT> WA_PHONE_ID:", repr(WA_PHONE_ID))
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

@app.get("/")
def root():
    return {"ok": True, "routes": [r.path for r in app.router.routes]}

# ==================== Webhook Verify (GET) ====================
@app.get("/wa-webhook")
async def verify(req: Request):
    mode = req.query_params.get("hub.mode")
    token = req.query_params.get("hub.verify_token")
    challenge = req.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("forbidden", status_code=403)

# ==================== Webhook Incoming (POST) ====================
@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", data)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if value.get("statuses"):
                continue

            for m in value.get("messages", []):
                user = m.get("from")
                if not user:
                    continue

                # Evitar reprocesar (reintentos webhook)
                msg_id = m.get("id")
                if msg_id and LAST_MSG_ID.get(user) == msg_id:
                    continue
                if msg_id:
                    LAST_MSG_ID[user] = msg_id

                # Reinicio manual
                txt_raw = ((m.get("text") or {}).get("body") or "").strip().lower()
                if txt_raw in ("hola","hello","/start","start","inicio","menu"):
                    SESSIONS[user] = {"step":"lang","lang":"EN","attempts_email":0}
                    wa_send_buttons(user, welcome_text(), opener_buttons())
                    continue

                # Primera vez
                if user not in SESSIONS:
                    SESSIONS[user] = {"step":"lang","lang":"EN","attempts_email":0}
                    wa_send_buttons(user, welcome_text(), opener_buttons())
                    continue

                text, reply_id = extract_text_or_reply(m)
                state = SESSIONS[user]

                # ========== UNIVERSAL EMAIL CAPTURE ==========
                if state.get("step") in ("contact_email_choice", "contact_email_enter") and EMAIL_RE.match(text or ""):
                    state["email"] = (text or "").strip()
                    state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                    h,b,btn,rows = main_menu_list(state["lang"])
                    wa_send_list(user, h, b, btn, rows)
                    state["step"] = "menu"
                    continue

                # ===== 0) idioma =====
                if state["step"] == "lang":
                    rid = (reply_id or "").upper().strip()
                    low = (text or "").strip().lower()
                    if rid == "LANG_ES" or "español" in low or low == "es":
                        state["lang"] = "ES"
                    elif rid == "LANG_EN" or "english" in low or low == "en":
                        state["lang"] = "EN"
                    else:
                        wa_send_buttons(user, welcome_text(), opener_buttons())
                        continue
                    state["step"] = "contact_name"
                    wa_send_text(user, human_intro(state["lang"]))
                    wa_send_text(user, ask_fullname(state["lang"]))
                    continue

                # ===== 1) Nombre =====
                if state["step"] == "contact_name":
                    if not valid_name(text):
                        wa_send_text(user, ask_fullname(state["lang"]))
                        continue
                    state["name"] = normalize_name(text)
                    state["step"] = "contact_email_choice"
                    state["attempts_email"] = 0
                    wa_send_text(user, ask_email(state["lang"]))
                    wa_send_buttons(user, " ", email_buttons(state["lang"]))
                    continue

                # ===== 2) Email (choice) =====
                if state["step"] == "contact_email_choice":
                    rid = (reply_id or "").upper()
                    low = (text or "").strip().lower()

                    if rid == "" and low in ("skip","saltar","usar whatsapp","use whatsapp","si","sí","yes","ok","dale","listo"):
                        state["email"] = f"{user}@whatsapp" if "whatsapp" in low else ""
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        h,b,btn,rows = main_menu_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        state["step"] = "menu"
                        continue

                    if rid == "EMAIL_ENTER":
                        state["step"] = "contact_email_enter"
                        wa_send_text(user, ("Escribe tu correo (ej. nombre@dominio.com)." if is_es(state["lang"]) else "Type your email (e.g., name@domain.com)."))
                        continue

                    if rid == "EMAIL_USE_WA":
                        state["email"] = f"{user}@whatsapp"
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        state["step"] = "menu"; continue

                    if rid == "EMAIL_SKIP":
                        state["email"] = ""
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        state["step"] = "menu"; continue

                    wa_send_buttons(user, " ", email_buttons(state["lang"]))
                    continue

                # ===== 2b) Email (enter) =====
                if state["step"] == "contact_email_enter":
                    if EMAIL_RE.match(text or ""):
                        state["email"] = (text or "").strip()
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        state["step"] = "menu"; continue
                    low = (text or "").strip().lower()
                    if low in ("", "skip","saltar","si","sí","yes","ok","dale","listo"):
                        state["email"] = ""
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        state["step"] = "menu"; continue
                    state["attempts_email"] = state.get("attempts_email", 0) + 1
                    if state["attempts_email"] >= 1:
                        state["email"] = ""
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        state["step"] = "menu"; continue
                    wa_send_buttons(user, " ", email_buttons(state["lang"]))
                    state["step"] = "contact_email_choice"; continue

                # ===== 3) Menú =====
                if state["step"] == "menu":
                    rid = (reply_id or "").upper()
                    if rid in ("SVC_VILLAS","SVC_ISLANDS","SVC_BOATS","SVC_WEDDINGS","SVC_CONCIERGE","SVC_TEAM"):
                        svc = {
                            "SVC_VILLAS":"villas",
                            "SVC_ISLANDS":"villas",
                            "SVC_BOATS":"boats",
                            "SVC_WEDDINGS":"weddings",
                            "SVC_CONCIERGE":"concierge",
                            "SVC_TEAM":"team",
                        }[rid]
                        state["service_type"] = svc
                        if state.get("city"):
                            if svc in ("villas","boats"):
                                state["step"] = "ask_date"
                                wa_send_text(user, ask_date_text(state["lang"]))
                                wa_send_buttons(user, " ", date_buttons(state["lang"]))
                                h,b,btn,rows = date_quick_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                            elif svc == "weddings":
                                state["step"] = "wed_month"
                                h,b,btn,rows = weddings_month_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                            elif svc == "concierge":
                                state["step"] = "cc_needs"
                                h,b,btn,rows = concierge_needs_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                            elif svc == "team":
                                state["step"] = "handoff_city_done"
                                owner_name, owner_id, cal_url, pretty_city = owner_for_city(state.get("city"))
                                wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city))
                                contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                                title = f"[{pretty_city}] Talk to the Team via WhatsApp"
                                desc  = f"City: {pretty_city}\nService: {svc}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                                if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                                notify_sales("Talk to Team", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                                wa_send_buttons(user, ("¿Qué más necesitas?" if is_es(state["lang"]) else "What else do you need?"), after_results_buttons(state["lang"]))
                            continue
                        # pedir ciudad
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        if svc in ("villas","boats"): state["step"] = "ask_city"
                        elif svc == "weddings":       state["step"] = "wed_city"
                        elif svc == "concierge":      state["step"] = "cc_city"
                        elif svc == "team":           state["step"] = "handoff_city"
                        continue
                    h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue

                # ===== Villas / Boats: ciudad =====
                if state["step"] == "ask_city":
                    rid = (reply_id or "").upper()
                    city_map = {
                        "CITY_CARTAGENA":"cartagena",
                        "CITY_MEDELLIN":"medellín",
                        "CITY_TULUM":"tulum",
                        "CITY_MXCITY":"mexico city",
                    }
                    city = city_map.get(rid)
                    if not city:
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    state["city"] = city
                    state["step"] = "ask_date"
                    wa_send_text(user, ask_date_text(state["lang"]))
                    wa_send_buttons(user, " ", date_buttons(state["lang"]))
                    h,b,btn,rows = date_quick_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                    continue

                # ===== Fecha =====
                if state["step"] == "ask_date":
                    rid = (reply_id or "").upper()
                    if rid in ("DATE_TODAY","DATE_TOMORROW","DATE_UNKNOWN"):
                        state["date"] = datetime.date.today().isoformat() if rid=="DATE_TODAY" else ((datetime.date.today()+datetime.timedelta(days=1)).isoformat() if rid=="DATE_TOMORROW" else "")
                        h,b,btn,rows = pax_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        state["step"] = "ask_pax"; continue
                    if rid in ("DATE_THIS_WEEKEND","DATE_NEXT_WEEKEND","DATE_NEXT_MONTH","DATE_TBD"):
                        state["date"] = parse_quick_date(rid) or ""
                        h,b,btn,rows = pax_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        state["step"] = "ask_pax"; continue
                    wa_send_buttons(user, " ", date_buttons(state["lang"]))
                    h,b,btn,rows = date_quick_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                    continue

                # ===== Pax =====
                if state["step"] == "ask_pax":
                    rid = (reply_id or "").upper()
                    if rid.startswith("PAX_"):
                        pax = pax_from_id(rid)
                        state["pax"] = pax
                        svc = state.get("service_type") or "villas"
                        unit = ("noche" if is_es(state["lang"]) else "night") if svc=="villas" else ("día" if is_es(state["lang"]) else "day")

                        if not GOOGLE_SHEET_CSV_URL:
                            owner_name, owner_id, cal_url, pretty_city = owner_for_city(state.get("city"))
                            wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city))
                            contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                            title = f"[{pretty_city}] {svc.title()} via WhatsApp"
                            desc  = f"City: {pretty_city}\nDate: {state.get('date') or 'TBD'}\nPax: {pax}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                            if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                            notify_sales("Lead (no catalog)", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                            state["step"] = "post_results"
                            wa_send_buttons(user, ("¿Quieres hacer algo más?" if is_es(state["lang"]) else "Anything else?"), after_results_buttons(state["lang"]))
                            continue

                        top = find_top_relaxed(service=svc, city=state.get("city"), pax=pax, prefs="", top_k=TOP_K)
                        wa_send_text(user, reply_topN(state["lang"], top, unit=unit))
                        state["last_top"] = top
                        # notificar ventas (lead calificado)
                        owner_name, owner_id, cal_url, pretty_city = owner_for_city(state.get("city"))
                        notify_sales("Lead (villas/boats)", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                        state["step"] = "post_results"
                        wa_send_buttons(user, ("¿Cómo seguimos?" if is_es(state["lang"]) else "How shall we proceed?"), after_results_buttons(state["lang"]))
                        continue
                    h,b,btn,rows = pax_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                    continue

                # ===== Weddings =====
                if state["step"] == "wed_city":
                    rid = (reply_id or "").upper()
                    city_map = {
                        "CITY_CARTAGENA":"cartagena",
                        "CITY_MEDELLIN":"medellín",
                        "CITY_TULUM":"tulum",
                        "CITY_MXCITY":"mexico city",
                    }
                    city = city_map.get(rid)
                    if not city:
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    state["city"] = city
                    state["step"] = "wed_month"
                    h,b,btn,rows = weddings_month_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                    continue

                if state["step"] == "wed_month":
                    rid = (reply_id or "").upper()
                    if rid.startswith("WED_MONTH_"):
                        state["wed_month"] = rid.replace("WED_MONTH_","")
                        state["step"] = "wed_guests"
                        h,b,btn,rows = weddings_guests_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        continue
                    h,b,btn,rows = weddings_month_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                    continue

                if state["step"] == "wed_guests":
                    rid = (reply_id or "").upper()
                    if rid.startswith("WED_G_"):
                        state["wed_guests"] = rid.replace("WED_G_","")
                        owner_name, owner_id, cal_url, pretty_city = owner_for_city(state.get("city"))
                        wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city))
                        contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        title = f"[{pretty_city}] Weddings & Events via WhatsApp"
                        desc  = f"City: {pretty_city}\nMonth: {state.get('wed_month')}\nGuests: {state.get('wed_guests')}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                        if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                        notify_sales("Lead (weddings)", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                        state["step"] = "post_results"
                        wa_send_buttons(user, ("¿Quieres algo más?" if is_es(state["lang"]) else "Anything else?"), after_results_buttons(state["lang"]))
                        continue
                    h,b,btn,rows = weddings_guests_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                    continue

                # ===== Concierge =====
                if state["step"] == "cc_city":
                    rid = (reply_id or "").upper()
                    city_map = {
                        "CITY_CARTAGENA":"cartagena",
                        "CITY_MEDELLIN":"medellín",
                        "CITY_TULUM":"tulum",
                        "CITY_MXCITY":"mexico city",
                    }
                    city = city_map.get(rid)
                    if not city:
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    state["city"] = city
                    state["step"] = "cc_needs"
                    h,b,btn,rows = concierge_needs_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                    continue

                if state["step"] == "cc_needs":
                    rid = (reply_id or "").upper()
                    if rid.startswith("CC_"):
                        state["cc_need"] = rid
                        owner_name, owner_id, cal_url, pretty_city = owner_for_city(state.get("city"))
                        wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city))
                        contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        title = f"[{pretty_city}] Concierge via WhatsApp"
                        desc  = f"City: {pretty_city}\nNeed: {state.get('cc_need')}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                        if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                        notify_sales("Lead (concierge)", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city, extra=state.get("cc_need"))
                        state["step"] = "post_results"
                        wa_send_buttons(user, ("¿Algo más?" if is_es(state["lang"]) else "Anything else?"), after_results_buttons(state["lang"]))
                        continue
                    h,b,btn,rows = concierge_needs_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                    continue

                # ===== Handoff directo (TEAM) =====
                if state["step"] == "handoff_city":
                    rid = (reply_id or "").upper()
                    city_map = {
                        "CITY_CARTAGENA":"cartagena",
                        "CITY_MEDELLIN":"medellín",
                        "CITY_TULUM":"tulum",
                        "CITY_MXCITY":"mexico city",
                    }
                    city = city_map.get(rid)
                    if not city:
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    state["city"] = city
                    state["step"] = "handoff_city_done"

                if state["step"] == "handoff_city_done":
                    owner_name, owner_id, cal_url, pretty_city = owner_for_city(state.get("city"))
                    wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city))
                    contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                    title = f"[{pretty_city}] Talk to the Team via WhatsApp"
                    desc  = f"City: {pretty_city}\nService: {state.get('service_type') or 'N/A'}\nDate: {state.get('date') or 'TBD'}\nPax: {state.get('pax') or 'TBD'}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                    if state.get("last_top"):
                        tops = "; ".join([f"{r.get('name')}→{r.get('url')}" for r in state["last_top"][:TOP_K]])
                        desc += f"\nTop shown: {tops}"
                    if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                    notify_sales("Talk to Team", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                    state["step"] = "post_results"
                    wa_send_buttons(user, ("¿Qué más necesitas?" if is_es(state["lang"]) else "What else do you need?"), after_results_buttons(state["lang"]))
                    continue

                # ===== Post-results =====
                if state["step"] == "post_results":
                    rid = (reply_id or "").upper()
                    if rid == "POST_ADD_SERVICE":
                        state["step"] = "menu"
                        h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    if rid == "POST_TALK_TEAM":
                        if state.get("city"):
                            state["step"] = "handoff_city_done"
                            owner_name, owner_id, cal_url, pretty_city = owner_for_city(state.get("city"))
                            wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city))
                            contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                            title = f"[{pretty_city}] Talk to the Team via WhatsApp"
                            desc  = f"City: {pretty_city}\nService: {state.get('service_type') or 'N/A'}\nDate: {state.get('date') or 'TBD'}\nPax: {state.get('pax') or 'TBD'}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                            if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                            notify_sales("Talk to Team", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                            wa_send_buttons(user, ("¿Qué más necesitas?" if is_es(state["lang"]) else "What else do you need?"), after_results_buttons(state["lang"]))
                        else:
                            state["step"] = "handoff_city"
                            h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows)
                        continue
                    if rid == "POST_MENU":
                        state["step"] = "menu"
                        h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    wa_send_buttons(user, ("¿Quieres añadir otro servicio o hablar con el equipo?"
                                           if is_es(state["lang"]) else
                                           "Would you like to add another service or talk to the team?"),
                                    after_results_buttons(state["lang"]))
                    continue

    return {"ok": True}
