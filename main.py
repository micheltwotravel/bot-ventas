# ==================== IMPORTS ====================
import os, re, csv, io, requests, datetime, smtplib
from email.mime.text import MIMEText
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

# ==================== APP ====================
app = FastAPI()

# ==================== CONFIG (ENV) ====================
VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN") or "").strip()
WA_TOKEN     = (os.getenv("WA_ACCESS_TOKEN") or "").strip()
WA_PHONE_ID  = (os.getenv("WA_PHONE_NUMBER_ID") or "").strip()

# Bot
BOT_NAME = (os.getenv("BOT_NAME") or "Luna").strip()

# HubSpot
HUBSPOT_TOKEN       = (os.getenv("HUBSPOT_TOKEN") or "").strip()
HUBSPOT_OWNER_SOFIA = (os.getenv("HUBSPOT_OWNER_SOFIA") or "").strip()
HUBSPOT_OWNER_ROSS  = (os.getenv("HUBSPOT_OWNER_ROSS")  or "").strip()
HUBSPOT_OWNER_RAY   = (os.getenv("HUBSPOT_OWNER_RAY")   or "").strip()
HUBSPOT_PIPELINE_ID  = (os.getenv("HUBSPOT_PIPELINE_ID")  or "").strip()
HUBSPOT_DEALSTAGE_ID = (os.getenv("HUBSPOT_DEALSTAGE_ID") or "").strip()

# Calendarios (y WhatsApp de owners)
CAL_RAY   = (os.getenv("CAL_RAY")   or "https://meetings.hubspot.com/ray-kanevsky").strip()
CAL_SOFIA = (os.getenv("CAL_SOFIA") or "https://marketing.two.travel/meetings/sofia217").strip()
CAL_ROSS  = (os.getenv("CAL_ROSS")  or "https://meetings.hubspot.com/ross334").strip()

OWNER_WA = {
    "sofia": (os.getenv("OWNER_SOFIA_WA") or "+573000000001").strip(),
    "ross":  (os.getenv("OWNER_ROSS_WA")  or "+573000000002").strip(),
    "ray":   (os.getenv("OWNER_RAY_WA")   or "+573000000003").strip(),
}

# Catálogo
GOOGLE_SHEET_CSV_URL = (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()
TOP_K = int(os.getenv("TOP_K", "3"))

# Correo ventas (SMTP)
SMTP_HOST    = (os.getenv("SMTP_HOST") or "").strip()
SMTP_PORT    = int(os.getenv("SMTP_PORT") or "587")
SMTP_USER    = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASS    = (os.getenv("SMTP_PASS") or "").strip()
SALES_EMAILS = [e.strip() for e in (os.getenv("SALES_EMAILS") or "michel@two.travel").split(",") if e.strip()]

# Estado en memoria
SESSIONS   = {}    # { phone: {step, lang, city, service_type, ...}}
LAST_MSGID = {}    # evitar reprocesar el mismo mensaje WA

# ==================== REGEX / VALIDACIONES ====================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def valid_name(fullname: str) -> bool:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return len(tokens) >= 2

def normalize_name(fullname: str) -> str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return " ".join(tokens[:3]).title()

# ==================== WHATSAPP HELPERS ====================
def _post_graph(path: str, payload: dict):
    url = f"https://graph.facebook.com/v23.0/{path}"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type":"application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=25)
    print(f"WA -> {r.status_code} {r.text[:240]}")
    return r

def wa_send_text(to: str, body: str):
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}}
    return _post_graph(f"{WA_PHONE_ID}/messages", payload)

def wa_send_buttons(to: str, body_text: str, buttons: list):
    """
    buttons: [{"id":"BTN_ID","title":"Title"}, ...]  (max 3)
    """
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
    """
    rows: [{"id":"ROW_ID","title":"Title","description":"..."}]
    """
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

# ==================== EMAIL (VENTAS) ====================
def send_sales_email(subject: str, body: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SALES_EMAILS):
        print("EMAIL [noop]>", subject, "\n", body[:600])
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(SALES_EMAILS)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, SALES_EMAILS, msg.as_string())
        print("EMAIL sent to:", SALES_EMAILS)
        return True
    except Exception as e:
        print("EMAIL error:", e)
        return False

def notify_sales(event: str, state: dict, phone: str, extra: str = "", cal_url: str = "", owner_name: str = "", pretty_city: str = ""):
    """
    Manda correo con el resumen + top mostrado.
    """
    name  = state.get("name") or "-"
    email = state.get("email") or "-"
    lang  = state.get("lang") or "-"
    svc   = state.get("service_type") or "-"
    city  = pretty_city or (state.get("city") or "-")
    date  = state.get("date") or state.get("wed_month") or "-"
    pax   = state.get("pax") or state.get("wed_guests") or "-"
    top   = state.get("last_top") or []
    tops  = "\n".join([f"- {r.get('name')} → {r.get('url_page')}" for r in top[:TOP_K]]) if top else "-"
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
        f"Top shown:\n{tops}",
    ]
    if extra:
        lines.append(f"Extra: {extra}")
    subject = f"[Two Travel WA] {svc.title()} – {city} – {name}"
    body = "\n".join(lines)
    send_sales_email(subject, body)

# ==================== HUBSPOT HELPERS ====================
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
        "preferred_language": ("es" if (lang or "").upper().startswith("ES") else "en"),
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

def owner_for_city(city: str):
    c = (city or "").strip().lower()
    if c in ("cartagena","ctg","tulum"):
        return ("Sofía", HUBSPOT_OWNER_SOFIA or None, CAL_SOFIA, "Cartagena/Tulum", OWNER_WA["sofia"])
    if c in ("medellin","medellín"):
        return ("Ross", HUBSPOT_OWNER_ROSS or None, CAL_ROSS, "Medellín", OWNER_WA["ross"])
    if c in ("mexico city","mexico","méxico","cdmx","mxcity"):
        return ("Ray", HUBSPOT_OWNER_RAY or None, CAL_RAY, "Mexico City", OWNER_WA["ray"])
    return ("Two Travel Team", None, CAL_SOFIA, city or "—", OWNER_WA["sofia"])

# ==================== CATALOGO (Google Sheet CSV) ====================
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

def _tag_hit(pref_tags: str, required_tag: str) -> bool:
    if not required_tag:
        return True
    tags = [t.strip().lower() for t in (pref_tags or "").split(",") if t.strip()]
    return required_tag.lower() in tags

def _price_val(r):
    try:
        return float(r.get("price_from_usd","999999") or "999999")
    except:
        return 999999.0

def filter_catalog(service, city, pax=0, category_tag=None, top_k=TOP_K):
    rows = load_catalog()
    if not rows:
        return []
    svc = (service or "").strip().lower()
    cty = (city or "").strip().lower()

    res = []
    for r in rows:
        if (r.get("service_type","").lower() != svc): 
            continue
        if (r.get("city","").lower() != cty):
            continue
        if pax:
            try:
                cap = int(float(r.get("capacity_max","0") or "0"))
            except:
                cap = 0
            if cap and cap < pax:
                continue
        if category_tag and not _tag_hit(r.get("preference_tags",""), category_tag):
            continue
        res.append(r)

    res.sort(key=_price_val)
    return res[:max(1,int(top_k or 1))]

# ==================== TEXTOS / UI ====================
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

def services_for_city(city: str):
    c = (city or "").strip().lower()
    if c == "cartagena": return ["villas","boats","islands","weddings","concierge","team"]
    if c == "medellín":  return ["villas","weddings","concierge","team"]
    if c == "mexico city": return ["villas","weddings","concierge","team"]
    if c == "tulum":     return ["villas","boats","weddings","concierge","team"]
    return ["villas","boats","islands","weddings","concierge","team"]

def main_menu_list(lang, city=None):
    header = "Two Travel"
    body   = ("Elige un servicio:" if is_es(lang) else "Choose a service:")
    svc = services_for_city(city) if city else ["villas","boats","islands","weddings","concierge","team"]
    rows = []
    if "villas" in svc: rows.append({"id":"SVC_VILLAS","title":"Villas 🏠","description":("Alojamiento premium" if is_es(lang) else "Premium stays")})
    if "boats" in svc: rows.append({"id":"SVC_BOATS","title":"Boats 🚤","description":("Días en el mar" if is_es(lang) else "Days at sea")})
    if "islands" in svc: rows.append({"id":"SVC_ISLANDS","title":"Islands 🏝️","description":("Islas privadas" if is_es(lang) else "Private islands")})
    if "weddings" in svc: rows.append({"id":"SVC_WEDDINGS","title":"Weddings 💍","description":("Venues & eventos" if is_es(lang) else "Venues & Events")})
    if "concierge" in svc: rows.append({"id":"SVC_CONCIERGE","title":"Concierge ✨","description":("Plan a medida" if is_es(lang) else "Bespoke planning")})
    if "team" in svc: rows.append({"id":"SVC_TEAM","title":"Team 👤","description":("Hablar con el equipo" if is_es(lang) else "Talk to the team")})
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

# Categorías
def villa_categories(lang):
    header = "Villas"
    body   = ("Elige rango de habitaciones:" if is_es(lang) else "Choose bedrooms range:")
    rows = [
        {"id":"VILLA_3_6","title":"3–6 BR","description":""},
        {"id":"VILLA_7_10","title":"7–10 BR","description":""},
        {"id":"VILLA_11_14","title":"11–14 BR","description":""},
        {"id":"VILLA_15P","title":"15+ BR","description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def boat_categories(lang):
    header = "Boats / Yachts"
    body   = ("Elige tipo de bote:" if is_es(lang) else "Choose boat type:")
    rows = [
        {"id":"BOAT_SPEED","title":"Speedboat","description":""},
        {"id":"BOAT_YACHT","title":"Yacht","description":""},
        {"id":"BOAT_CAT","title":"Catamaran","description":""},
        {"id":"BOAT_LUX","title":"Luxury","description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def island_categories(lang):
    header = "Islands"
    body   = ("Elige tamaño de isla:" if is_es(lang) else "Choose island size:")
    rows = [
        {"id":"ISL_SMALL","title":"Small <50 pax","description":""},
        {"id":"ISL_MED","title":"Medium 50–150","description":""},
        {"id":"ISL_LARGE","title":"Large 150+","description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def pax_list(lang):
    header = ("Personas" if is_es(lang) else "Guests")
    body   = ("Elige un rango:" if is_es(lang) else "Choose a range:")
    rows = [
        {"id":"PAX_4","title":"2–4","description":""},
        {"id":"PAX_8","title":"5–8","description":""},
        {"id":"PAX_12","title":"9–12","description":""},
        {"id":"PAX_16","title":"13–16","description":""},
        {"id":"PAX_20","title":"17+","description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def weddings_guests_list(lang):
    header = ("Invitados" if is_es(lang) else "Guests")
    body   = ("Elige un rango de invitados:" if is_es(lang) else "Choose a guest range:")
    rows = [
        {"id":"WED_PAX_50","title":"20–50","description":""},
        {"id":"WED_PAX_80","title":"51–80","description":""},
        {"id":"WED_PAX_120","title":"81–120","description":""},
        {"id":"WED_PAX_200","title":"121–200","description":""},
        {"id":"WED_PAX_300","title":"200+","description":""},
        {"id":"WED_PAX_UNK","title":("No sé" if is_es(lang) else "Don’t know"),"description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def format_results(lang: str, items: list, unit: str):
    if not items:
        return ("No se encontraron opciones; nuestro equipo te ayuda de forma personalizada."
                if is_es(lang) else
                "No matches found; our team will assist you directly.")
    lines = []
    es = is_es(lang)
    if es:
        lines.append(f"Estas son nuestras mejores {len(items)} opción(es) (precios *desde*):")
        for r in items:
            desc = r.get("description_es") or ""
            why  = r.get("why_pick_es") or ""
            tags = r.get("preference_tags") or ""
            lines.append(
                f"• {r.get('name')} — USD {r.get('price_from_usd','?')}/{unit}\n"
                f"{desc}\n✨ {why}\n🔖 {tags}\n→ {r.get('url_page')}"
            )
        lines.append("La *disponibilidad final* la confirma nuestro equipo antes de reservar.")
    else:
        lines.append(f"Here are the top {len(items)} option(s) (*prices from*):")
        for r in items:
            desc = r.get("description_en") or ""
            why  = r.get("why_pick_en") or ""
            tags = r.get("preference_tags") or ""
            lines.append(
                f"• {r.get('name')} — USD {r.get('price_from_usd','?')}/{unit}\n"
                f"{desc}\n✨ {why}\n🔖 {tags}\n→ {r.get('url_page')}"
            )
        lines.append("Final *availability* is confirmed by our team before booking.")
    return "\n".join(lines)

def after_results_buttons(lang):
    return [
        {"id":"POST_ADD_SERVICE","title":("Añadir otro servicio" if is_es(lang) else "Add another service")},
        {"id":"POST_TALK_TEAM","title":("Hablar con el equipo" if is_es(lang) else "Talk to the team")},
        {"id":"POST_MENU","title":("Volver al menú" if is_es(lang) else "Back to menu")},
    ]

def handoff_text(lang, owner_name, cal_url, city, wa_num):
    wa_link = f"https://wa.me/{wa_num.replace('+','')}"
    if is_es(lang):
        return (f"Te conecto con *{owner_name}* (equipo Two Travel – {city}) para confirmar disponibilidad.\n\n"
                f"📅 Agenda aquí: {cal_url}\n"
                f"💬 WhatsApp directo: {wa_link}")
    else:
        return (f"I’ll connect you with *{owner_name}* (Two Travel team – {city}) to confirm availability.\n\n"
                f"📅 Schedule here: {cal_url}\n"
                f"💬 Direct WhatsApp: {wa_link}")

# ==================== PAX HELPERS ====================
def pax_from_reply(rid: str) -> int:
    if rid.startswith("PAX_"):
        try: return int(rid.split("_")[1])
        except: return 2
    if rid.startswith("WED_PAX_"):
        m = rid.split("_")[2]
        if m == "UNK": return 0
        try: return int(m)
        except: return 0
    return 0

# ==================== STARTUP / HEALTH ====================
@app.on_event("startup")
async def show_routes():
    print("BOOT> Routes:", [r.path for r in app.router.routes])
    print("BOOT> WA_PHONE_ID:", repr(WA_PHONE_ID))
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

@app.get("/")
def root():
    return {"ok": True, "routes": [r.path for r in app.router.routes]}

# ==================== WEBHOOK VERIFY (GET) ====================
@app.get("/wa-webhook")
async def verify(req: Request):
    mode = req.query_params.get("hub.mode")
    token = req.query_params.get("hub.verify_token")
    challenge = req.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("forbidden", status_code=403)

# ==================== WEBHOOK INCOMING (POST) ====================
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

                # Evitar reprocesar
                msg_id = m.get("id")
                if msg_id and LAST_MSGID.get(user) == msg_id:
                    continue
                if msg_id:
                    LAST_MSGID[user] = msg_id

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

                # ========== UNIVERSAL: CAPTURA EMAIL SI ESTABA EN STEP DE EMAIL ==========
                if state.get("step") in ("contact_email_choice", "contact_email_enter") and EMAIL_RE.match(text or ""):
                    state["email"] = (text or "").strip()
                    state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                    # ciudad primero
                    state["step"] = "city"
                    h,b,btn,rows = city_list(state["lang"])
                    wa_send_list(user, h, b, btn, rows)
                    continue

                # ===== 0) idioma =====
                if state["step"] == "lang":
                    rid = (reply_id or "").upper().strip()
                    low = (text or "").strip().lower()
                    if rid == "LANG_ES" or "español" in low or low == "es":
                        state["lang"] = "ES"
                    else:
                        state["lang"] = "EN"
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

                    if rid == "EMAIL_ENTER":
                        state["step"] = "contact_email_enter"
                        wa_send_text(user, ("Escribe tu correo (ej. nombre@dominio.com)." if is_es(state["lang"]) else "Type your email (e.g., name@domain.com)."))
                        continue

                    if rid == "EMAIL_USE_WA" or low in ("usar whatsapp","use whatsapp"):
                        state["email"] = f"{user}@whatsapp"
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        continue

                    if rid == "EMAIL_SKIP" or low in ("skip","saltar"):
                        state["email"] = ""
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        continue

                    # si no eligió nada, re-mostrar
                    wa_send_buttons(user, " ", email_buttons(state["lang"]))
                    continue

                # ===== 2b) Email (enter) =====
                if state["step"] == "contact_email_enter":
                    if EMAIL_RE.match(text or ""):
                        state["email"] = (text or "").strip()
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        continue
                    # si no válido, permitir saltar
                    low = (text or "").strip().lower()
                    if low in ("", "skip","saltar","si","sí","yes","ok","dale","listo"):
                        state["email"] = ""
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        continue
                    state["attempts_email"] = state.get("attempts_email", 0) + 1
                    if state["attempts_email"] >= 1:
                        state["email"] = ""
                        state["contact_id"] = hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        continue
                    wa_send_buttons(user, " ", email_buttons(state["lang"]))
                    state["step"] = "contact_email_choice"
                    continue

                # ===== 3) Ciudad =====
                if state["step"] == "city":
                    rid = (reply_id or "").upper()
                    city_map = {
                        "CITY_CARTAGENA":"cartagena",
                        "CITY_MEDELLIN":"medellín",
                        "CITY_TULUM":"tulum",
                        "CITY_MXCITY":"mexico city",
                    }
                    city = city_map.get(rid)
                    if not city:
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        continue
                    state["city"] = city
                    state["step"] = "menu"
                    h,b,btn,rows = main_menu_list(state["lang"], state["city"])
                    wa_send_list(user, h, b, btn, rows)
                    continue

                # ===== 4) Menú (servicio) =====
                if state["step"] == "menu":
                    rid = (reply_id or "").upper()
                    if rid in ("SVC_VILLAS","SVC_BOATS","SVC_ISLANDS","SVC_WEDDINGS","SVC_CONCIERGE","SVC_TEAM"):
                        svc_map = {
                            "SVC_VILLAS":"villas",
                            "SVC_BOATS":"boats",
                            "SVC_ISLANDS":"islands",
                            "SVC_WEDDINGS":"weddings",
                            "SVC_CONCIERGE":"concierge",
                            "SVC_TEAM":"team",
                        }
                        state["service_type"] = svc_map[rid]

                        # Redirigir a categoría / flujo específico
                        if state["service_type"] == "villas":
                            state["step"] = "villa_cat"
                            h,b,btn,rows = villa_categories(state["lang"])
                            wa_send_list(user, h, b, btn, rows); continue

                        if state["service_type"] == "boats":
                            state["step"] = "boat_cat"
                            h,b,btn,rows = boat_categories(state["lang"])
                            wa_send_list(user, h, b, btn, rows); continue

                        if state["service_type"] == "islands":
                            state["step"] = "island_cat"
                            h,b,btn,rows = island_categories(state["lang"])
                            wa_send_list(user, h, b, btn, rows); continue

                        if state["service_type"] == "weddings":
                            state["step"] = "wed_guests"
                            h,b,btn,rows = weddings_guests_list(state["lang"])
                            wa_send_list(user, h, b, btn, rows); continue

                        if state["service_type"] in ("concierge","team"):
                            state["step"] = "handoff"
                            # directo a handoff
                            owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                            wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city, wa_num))
                            contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                            title = f"[{pretty_city}] {state['service_type'].title()} via WhatsApp"
                            desc  = f"City: {pretty_city}\nService: {state['service_type']}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                            if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                            notify_sales("Talk to Team / Concierge", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                            state["step"] = "post_results"
                            wa_send_buttons(user, ("¿Qué más necesitas?" if is_es(state["lang"]) else "What else do you need?"), after_results_buttons(state["lang"]))
                            continue

                    # si escribió texto raro, re-mostrar
                    h,b,btn,rows = main_menu_list(state["lang"], state.get("city"))
                    wa_send_list(user, h, b, btn, rows)
                    continue

                # ===== 5) VILLAS → categoría =====
                if state["step"] == "villa_cat":
                    rid = (reply_id or "").upper()
                    if rid not in ("VILLA_3_6","VILLA_7_10","VILLA_11_14","VILLA_15P"):
                        h,b,btn,rows = villa_categories(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    # mapear a tag esperada en preference_tags
                    category_tag = {
                        "VILLA_3_6":"bed_3_6",
                        "VILLA_7_10":"bed_7_10",
                        "VILLA_11_14":"bed_11_14",
                        "VILLA_15P":"bed_15_plus",
                    }[rid]
                    state["category_tag"] = category_tag
                    state["step"] = "villa_pax"
                    h,b,btn,rows = pax_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue

                # ===== 6) VILLAS → pax y resultados =====
                if state["step"] == "villa_pax":
                    rid = (reply_id or "").upper()
                    if not rid or not rid.startswith("PAX_"):
                        h,b,btn,rows = pax_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    pax = pax_from_reply(rid); state["pax"] = pax
                    top = filter_catalog("villas", state["city"], pax, state.get("category_tag"))
                    state["last_top"] = top
                    reply = format_results(state["lang"], top, "night" if not is_es(state["lang"]) else "noche")
                    wa_send_text(user, reply)
                    # notify ventas
                    owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                    notify_sales("Lead Villas", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                    state["step"] = "post_results"
                    wa_send_buttons(user, ("¿Cómo seguimos?" if is_es(state["lang"]) else "How shall we proceed?"), after_results_buttons(state["lang"]))
                    continue

                # ===== 5b) BOATS → categoría =====
                if state["step"] == "boat_cat":
                    rid = (reply_id or "").upper()
                    if rid not in ("BOAT_SPEED","BOAT_YACHT","BOAT_CAT","BOAT_LUX"):
                        h,b,btn,rows = boat_categories(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    category_tag = {
                        "BOAT_SPEED":"type_speedboat",
                        "BOAT_YACHT":"type_yacht",
                        "BOAT_CAT":"type_catamaran",
                        "BOAT_LUX":"luxury",
                    }[rid]
                    state["category_tag"] = category_tag
                    state["step"] = "boat_pax"
                    h,b,btn,rows = pax_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue

                # ===== 6b) BOATS → pax y resultados =====
                if state["step"] == "boat_pax":
                    rid = (reply_id or "").upper()
                    if not rid or not rid.startswith("PAX_"):
                        h,b,btn,rows = pax_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    pax = pax_from_reply(rid); state["pax"] = pax
                    top = filter_catalog("boats", state["city"], pax, state.get("category_tag"))
                    state["last_top"] = top
                    reply = format_results(state["lang"], top, "day" if not is_es(state["lang"]) else "día")
                    wa_send_text(user, reply)
                    owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                    notify_sales("Lead Boats", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                    state["step"] = "post_results"
                    wa_send_buttons(user, ("¿Cómo seguimos?" if is_es(state["lang"]) else "How shall we proceed?"), after_results_buttons(state["lang"]))
                    continue

                # ===== 5c) ISLANDS → categoría =====
                if state["step"] == "island_cat":
                    rid = (reply_id or "").upper()
                    if rid not in ("ISL_SMALL","ISL_MED","ISL_LARGE"):
                        h,b,btn,rows = island_categories(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    category_tag = {
                        "ISL_SMALL":"size_small",
                        "ISL_MED":"size_medium",
                        "ISL_LARGE":"size_large",
                    }[rid]
                    state["category_tag"] = category_tag
                    state["step"] = "island_pax"
                    # para islas, usamos rangos más altos
                    header = "Guests" if not is_es(state["lang"]) else "Invitados"
                    body   = "Choose a range:" if not is_es(state["lang"]) else "Elige un rango:"
                    rows = [
                        {"id":"PAX_50","title":"0–50","description":""},
                        {"id":"PAX_150","title":"50–150","description":""},
                        {"id":"PAX_300","title":"150+","description":""},
                    ]
                    wa_send_list(user, header, body, ("Choose" if not is_es(state["lang"]) else "Elegir"), rows)
                    continue

                # ===== 6c) ISLANDS → pax y resultados =====
                if state["step"] == "island_pax":
                    rid = (reply_id or "").upper()
                    if not rid or not rid.startswith("PAX_"):
                        header = "Guests" if not is_es(state["lang"]) else "Invitados"
                        body   = "Choose a range:" if not is_es(state["lang"]) else "Elige un rango:"
                        rows = [
                            {"id":"PAX_50","title":"0–50","description":""},
                            {"id":"PAX_150","title":"50–150","description":""},
                            {"id":"PAX_300","title":"150+","description":""},
                        ]
                        wa_send_list(user, header, body, ("Choose" if not is_es(state["lang"]) else "Elegir"), rows)
                        continue
                    pax = pax_from_reply(rid); state["pax"] = pax
                    top = filter_catalog("islands", state["city"], pax, state.get("category_tag"))
                    state["last_top"] = top
                    reply = format_results(state["lang"], top, "night" if not is_es(state["lang"]) else "noche")
                    wa_send_text(user, reply)
                    owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                    notify_sales("Lead Islands", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                    state["step"] = "post_results"
                    wa_send_buttons(user, ("¿Cómo seguimos?" if is_es(state["lang"]) else "How shall we proceed?"), after_results_buttons(state["lang"]))
                    continue

                # ===== 5d) WEDDINGS → invitados & resultados =====
                if state["step"] == "wed_guests":
                    rid = (reply_id or "").upper()
                    if rid not in ("WED_PAX_50","WED_PAX_80","WED_PAX_120","WED_PAX_200","WED_PAX_300","WED_PAX_UNK"):
                        h,b,btn,rows = weddings_guests_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    pax = pax_from_reply(rid); state["pax"] = pax
                    top = filter_catalog("weddings", state["city"], pax)
                    state["last_top"] = top
                    reply = format_results(state["lang"], top, "event")
                    wa_send_text(user, reply)
                    owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                    notify_sales("Lead Weddings", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                    state["step"] = "post_results"
                    wa_send_buttons(user, ("¿Cómo seguimos?" if is_es(state["lang"]) else "How shall we proceed?"), after_results_buttons(state["lang"]))
                    continue

                # ===== 7) POST-RESULTS =====
                if state["step"] == "post_results":
                    rid = (reply_id or "").upper()
                    if rid == "POST_ADD_SERVICE":
                        state["step"] = "menu"
                        h,b,btn,rows = main_menu_list(state["lang"], state.get("city"))
                        wa_send_list(user, h, b, btn, rows)
                        continue
                    if rid == "POST_TALK_TEAM":
                        state["step"] = "handoff"
                        owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                        wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city, wa_num))
                        contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        title = f"[{pretty_city}] Talk to the Team via WhatsApp"
                        desc  = f"City: {pretty_city}\nService: {state.get('service_type') or 'N/A'}\nDate: {state.get('date') or 'TBD'}\nPax: {state.get('pax') or 'TBD'}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                        if state.get("last_top"):
                            tops = "; ".join([f"{r.get('name')}→{r.get('url_page')}" for r in state["last_top"][:TOP_K]])
                            desc += f"\nTop shown: {tops}"
                        if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                        notify_sales("Talk to Team", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)
                        state["step"] = "post_results"
                        wa_send_buttons(user, ("¿Qué más necesitas?" if is_es(state["lang"]) else "What else do you need?"), after_results_buttons(state["lang"]))
                        continue
                    if rid == "POST_MENU":
                        state["step"] = "menu"
                        h,b,btn,rows = main_menu_list(state["lang"], state.get("city"))
                        wa_send_list(user, h, b, btn, rows)
                        continue

                    # si nada, re-ofrecer
                    wa_send_buttons(user, ("¿Quieres añadir otro servicio o hablar con el equipo?"
                                           if is_es(state["lang"]) else
                                           "Would you like to add another service or talk to the team?"),
                                    after_results_buttons(state["lang"]))
                    continue

                # fallback de seguridad
                SESSIONS[user] = state

    return {"ok": True}
