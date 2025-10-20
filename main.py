# ==================== IMPORTS ====================
import os, re, csv, io, requests, smtplib
import urllib.parse
import unicodedata
from email.mime.text import MIMEText
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from datetime import datetime
from zoneinfo import ZoneInfo
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

# Calendarios (opcional mostrar)
CAL_RAY   = (os.getenv("CAL_RAY")   or "https://meetings.hubspot.com/ray-kanevsky").strip()

# Dueño global único (todo cae con Ray)
# ✅ Corrección pedida: sin "Mr." y con nombre bien escrito.
OWNER_GLOBAL_NAME = "Ray Kanevsky"
OWNER_GLOBAL_WA   = (os.getenv("OWNER_GLOBAL_WA") or "+1 212 653 0000").strip()

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

# ==================== Regex / Normalización robusta ====================
# Email laxo (tolerante a mayúsculas/minúsculas)
EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)
ZERO_WIDTH = "".join([
    "\u200B", "\u200C", "\u200D", "\uFEFF",  # caracteres invisibles
])

def strip_invisibles(s: str) -> str:
    if not s: return ""
    for ch in ZERO_WIDTH:
        s = s.replace(ch, "")
    return s

def sanitize_email_input(s: str) -> str:
    s = strip_invisibles((s or "").strip())
    s = re.sub(r"(?i)\b(correo|email|mail|e[-\s]?mail|mailto)\s*:\s*", "", s)
    s = s.strip(" .;,!:)>]\"'")  # quita punticos y cierres colgantes
    # Formato "Nombre <correo@dominio.com>"
    m = re.search(r"<\s*([^<>@\s]+@[^<>@\s]+\.[^<>@\s]+)\s*>", s)
    if m:
        s = m.group(1)
    return s

def extract_first_email(s: str) -> str:
    s = strip_invisibles(s or "")
    m = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", s, re.IGNORECASE)
    return m.group(1) if m else ""

def strip_accents(s: str) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))

def norm(s: str) -> str:
    s = (s or "").strip()
    s = strip_accents(s).lower()
    s = re.sub(r"\s+", " ", s)
    return s

# ==================== Helpers de nombre ====================
def valid_name(fullname: str) -> bool:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return len(tokens) >= 1  # acepta nombre sin apellido

def normalize_name(fullname: str) -> str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return " ".join(tokens[:2]).title()

# ==================== Preferencias / etiquetas legibles ====================
def is_es(lang: str) -> bool:
    return (lang or "EN").upper().startswith("ES")

# ======= DESCRIPCIÓN (ES/EN) =======
def get_locale(user_lang: str) -> str:
    return "es" if (user_lang or "").lower().startswith("es") else "en"

def pick_description(row: dict, user_lang: str) -> str:
    lang = get_locale(user_lang)
    desc = (row.get(f"description_{lang}") or "").strip()
    if not desc:
        desc = (row.get("description_en") or row.get("description_es") or "").strip()
    # WA: límite de líneas – recorta un poco si es muy largo
    if len(desc) > 360:
        desc = desc[:357].rstrip() + "…"
    return desc


def human_pref_label(service: str, lang: str, category_tag: str) -> str:
    es = is_es(lang)
    service = (service or "").lower()
    tag = (category_tag or "").lower()

    if service == "villas":
        if es:
            m = {"bed_3_6":"3–6 habitaciones","bed_7_10":"7–10 habitaciones","bed_11_14":"11–14 habitaciones","bed_15_plus":"15+ habitaciones"}
        else:
            m = {"bed_3_6":"3–6 bedrooms","bed_7_10":"7–10 bedrooms","bed_11_14":"11–14 bedrooms","bed_15_plus":"15+ bedrooms"}
        return m.get(tag, "")
    if service == "boats":
        if es:
            m = {"type_speedboat":"Speedboat","type_yacht":"Yacht","type_catamaran":"Catamarán"}
        else:
            m = {"type_speedboat":"Speedboat","type_yacht":"Yacht","type_catamaran":"Catamaran"}
        return m.get(tag, "")
    if service == "islands":
        if es:
            m = {"size_small":"Isla pequeña","size_medium":"Isla mediana","size_large":"Isla grande"}
        else:
            m = {"size_small":"Small island","size_medium":"Medium island","size_large":"Large island"}
        return m.get(tag, "")
    return ""

def append_history(state: dict, service: str):
    hist = state.setdefault("history", [])
    entry = {
        "service": (service or "").lower(),
        "pax": state.get("pax"),
        "date": state.get("date"),
        "category_tag": state.get("category_tag"),
        "city": state.get("city"),
        "lang": state.get("lang"),
    }
    if not hist or any(entry.get(k) != hist[-1].get(k) for k in ("service","pax","date","category_tag","city")):
        hist.append(entry)

def build_history_lines(state: dict) -> str:
    lang = state.get("lang") or "EN"
    es = is_es(lang)
    hist = state.get("history") or []
    if not hist:
        return ""
    lines = []
    for h in hist:
        svc = h.get("service") or "-"
        pax = h.get("pax") or ("por definir" if es else "TBD")
        date = h.get("date") or ("por definir" if es else "TBD")
        pref = human_pref_label(svc, lang, h.get("category_tag"))
        svc_label = {
            "villas": ("Villas" if es else "Villas"),
            "boats": ("Botes/Yates" if es else "Boats/Yachts"),
            "islands": ("Islas" if es else "Islands"),
            "weddings": ("Bodas" if es else "Weddings"),
            "concierge": ("Concierge" if es else "Concierge"),
            "team": ("Equipo" if es else "Team"),
        }.get(svc, svc.title())
        extra = f" — {pref}" if pref else ""
        if es:
            lines.append(f"• {svc_label}{extra}; Pax: {pax}; Fecha: {date}")
        else:
            lines.append(f"• {svc_label}{extra}; Guests: {pax}; Date: {date}")
    return "\n".join(lines)

# ==================== City / Service ====================
def canonical_city(city: str) -> str:
    x = norm(city)
    aliases = {
        "cartagena de indias": "cartagena",
        "cartagena": "cartagena",
        "medellin": "medellin",
        "medellín": "medellin",
        "cdmx": "mexico city",
        "mexico": "mexico city",
        "mexico city": "mexico city",
        "mxcity": "mexico city",
        "tulum": "tulum",
    }
    return aliases.get(x, x)

def canonical_service(service: str) -> str:
    x = norm(service)
    aliases = {
        "villa": "villas", "villas": "villas",
        "boat": "boats", "boats": "boats", "yacht": "boats", "yachts": "boats",
        "island": "islands", "islands": "islands",
        "wedding": "weddings", "weddings": "weddings",
        "concierge": "concierge", "team": "team"
    }
    return aliases.get(x, x)

# ==================== WHATSAPP HELPERS ====================
def _post_graph(path: str, payload: dict):
    url = f"https://graph.facebook.com/v23.0/{path}"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type":"application/json"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=25)
        print(f"WA -> {r.status_code} {r.text[:240]}")
        return r
    except Exception as e:
        print("WA POST error:", e)
        class Dummy: status_code=599; text=str(e)
        return Dummy()

def wa_send_text(to: str, body: str):
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body[:4096]}}
    return _post_graph(f"{WA_PHONE_ID}/messages", payload)

def wa_send_buttons(to: str, body_text: str, buttons: list):
    payload = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text": body_text[:1024]},
            "action":{"buttons":[{"type":"reply","reply":b} for b in buttons[:3]]}
        }
    }
    return _post_graph(f"{WA_PHONE_ID}/messages", payload)

def wa_send_list(to: str, header_text: str, body_text: str, button_text: str, rows: list):
    # Librería WA limita longitudes
    payload = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"interactive",
        "interactive":{
            "type":"list",
            "header":{"type":"text","text": header_text[:60]},
            "body":{"text": body_text[:1024]},
            "footer":{"text":"Two Travel"},
            "action":{
                "button": button_text[:20],
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
    # Imagen, audio, etc. => no bloquear
    return "", None

def wa_click_number(num: str) -> str:
    return re.sub(r"\D", "", num or "")

# ==================== EMAIL (VENTAS) ====================
def send_sales_email(subject: str, body: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SALES_EMAILS):
        print("EMAIL [noop]>", subject, "\n", body[:600])
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject[:200]
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
    name  = state.get("name") or "-"
    email = state.get("email") or "-"
    lang  = state.get("lang") or "-"
    svc   = state.get("service_type") or "-"
    city  = pretty_city or (state.get("city") or "-")
    date  = state.get("date") or "-"
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
        try:
            s = requests.post(f"{base}/search", headers=headers, json={
                "filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}],
                "properties":["email"]
            }, timeout=20)
            if s.ok and s.json().get("results"):
                cid = s.json()["results"][0]["id"]
        except Exception as e:
            print("HubSpot search error:", e)

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
        try:
            up = requests.patch(f"{base}/{cid}", headers=headers, json={"properties": props}, timeout=20)
            print("HubSpot contact update:", up.status_code, up.text[:150])
            return cid if up.ok else None
        except Exception as e:
            print("HubSpot contact update error:", e)
            return None

    try:
        r = requests.post(base, headers=headers, json={"properties": props}, timeout=20)
        if r.status_code == 201:
            cid = r.json().get("id")
            print("HubSpot contact created", cid)
            return cid
        print("HubSpot contact error:", r.status_code, r.text[:200])
    except Exception as e:
        print("HubSpot contact create error:", e)
    return None

def hubspot_create_deal(contact_id, owner_id, title, desc):
    if not HUBSPOT_TOKEN:
        print("WARN: HUBSPOT_TOKEN missing")
        return None
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    base = "https://api.hubapi.com/crm/v3/objects/deals"
    props = {"dealname": title[:250], "description": desc[:65530]}
    if HUBSPOT_PIPELINE_ID:  props["pipeline"]  = HUBSPOT_PIPELINE_ID
    if HUBSPOT_DEALSTAGE_ID: props["dealstage"] = HUBSPOT_DEALSTAGE_ID
    if owner_id:             props["hubspot_owner_id"] = owner_id
    try:
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
    except Exception as e:
        print("HubSpot deal exception:", e)
        return None

def owner_for_city(city: str):
    pretty = city or "—"
    name = OWNER_GLOBAL_NAME
    wa   = OWNER_GLOBAL_WA
    cal  = CAL_RAY or ""
    return (name, HUBSPOT_OWNER_RAY or None, cal, pretty, wa)

# ==================== CATÁLOGO ====================
def load_catalog():
    if not GOOGLE_SHEET_CSV_URL:
        print("WARN: GOOGLE_SHEET_CSV_URL missing")
        return []
    try:
        r = requests.get(GOOGLE_SHEET_CSV_URL, timeout=30)
        if not r.ok:
            print("Catalog download error:", r.status_code, r.text[:200])
            return []
        rows = []
        content = r.content.decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            clean = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            rows.append(clean)
        print("Catalog rows:", len(rows))
        return rows
    except Exception as e:
        print("Catalog fetch exception:", e)
        return []

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

    svc_norm = canonical_service(service)
    city_norm = canonical_city(city)

    pool = []
    for r in rows:
        r_svc = canonical_service(r.get("service_type",""))
        r_city = canonical_city(r.get("city",""))
        if r_svc == svc_norm and r_city == city_norm:
            pool.append(r)

    if not pool:
        return []

    def safe_int(x, default=0):
        try:
            return int(float(x))
        except:
            return default

    scored = []
    for r in pool:
        cap = safe_int(r.get("capacity_max"), 0)
        price = _price_val(r)

        if pax and cap:
            gap = cap - pax
            cap_penalty = 9999 if gap < 0 else gap
        else:
            cap_penalty = 0

        bonus = 0
        if category_tag and _tag_hit(r.get("preference_tags",""), category_tag):
            bonus = -10

        scored.append((cap_penalty + bonus, price, r))

    scored.sort(key=lambda t: (t[0], t[1]))
    top_n = [r for _,__,r in scored[:max(1,int(top_k or 1))]]
    return top_n

# ==================== TEXTOS / UI ====================
def welcome_text():
    return ("Hi friend! Welcome to Two Travel 🌴\n\nChoose your language:\n\nElige tu idioma:")

def opener_buttons():
    return [
        {"id":"LANG_EN","title":"🇺🇸 English"},
        {"id":"LANG_ES","title":"🇪🇸 Español"},
    ]

def human_intro(lang):
    return ("¡Hola! Soy *Luna*, tu asistente de Two Travel. 💫\n"
            "¿Cómo te puedo llamar? (solo nombre está perfecto)"
            if is_es(lang)
            else
            "Hi! I’m *Luna*, your Two Travel assistant. 💫\n"
            "What should I call you? (first name is perfect)")

def ask_fullname(lang):
    return ("¿Cómo te puedo llamar? (solo nombre está perfecto)"
            if is_es(lang) else
            "What should I call you? (first name is perfect)")

def ask_email(lang):
    return ("¿Quieres dejar tu correo para enviarte opciones y seguir por ahí? Puedes *Saltar* y continuar."
            if is_es(lang) else
            "Would you like to add your email so we can send options and continue there? You can *Skip* and continue.")

def email_buttons(lang):
    return [
        {"id":"EMAIL_ENTER","title":("Ingresar email" if is_es(lang) else "Enter email")},
        {"id":"EMAIL_USE_WA","title":("Usar mi WhatsApp" if is_es(lang) else "Use my WhatsApp")},
        {"id":"EMAIL_SKIP","title":("Saltar" if is_es(lang) else "Skip")},
    ]

def city_list(lang):
    header = ("Ciudad / City" if is_es(lang) else "City")
    body   = ("¿En qué ciudad estás interesado?" if is_es(lang) else "Which city are you interested in?")
    rows = [
        {"id":"CITY_CARTAGENA","title":"Cartagena","description":"Colombia"},
        {"id":"CITY_MEDELLIN", "title":"Medellín","description":"Colombia"},
        {"id":"CITY_TULUM",    "title":"Tulum","description":"Mexico"},
        {"id":"CITY_MXCITY",   "title":"Mexico City","description":"Mexico"},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def services_for_city(city: str):
    c = canonical_city(city or "")
    if c == "cartagena": return ["villas","boats","islands","weddings","concierge","team"]
    if c == "medellin":  return ["villas","weddings","concierge","team"]
    if c == "mexico city": return ["villas","weddings","concierge","team"]
    if c == "tulum":     return ["villas","boats","weddings","concierge","team"]
    return ["villas","boats","islands","weddings","concierge","team"]

def main_menu_list(lang, city=None):
    header = "Two Travel"
    body = ("¿Qué servicio te gustaría reservar con nosotros?\n\nMira estas opciones:"
            if is_es(lang) else
            "Which service would you like to book with us?\n\nHere are some options:")
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

def villa_categories(lang):
    header = "Villas"
    body   = ("Elige rango de *habitaciones*:" if is_es(lang) else "Choose bedrooms range:")
    rows = [
        {"id":"VILLA_3_6","title":("3–6 Habitaciones" if is_es(lang) else "3–6 Bedrooms"),"description":""},
        {"id":"VILLA_7_10","title":("7–10 Habitaciones" if is_es(lang) else "7–10 Bedrooms"),"description":""},
        {"id":"VILLA_11_14","title":("11–14 Habitaciones" if is_es(lang) else "11–14 Bedrooms"),"description":""},
        {"id":"VILLA_15P","title":("15+ Habitaciones" if is_es(lang) else "15+ Bedrooms"),"description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def boat_categories(lang):
    header = "Boats / Yachts"
    body = ("¿Qué tipo de bote sueñas para ese día?" if is_es(lang) else "What kind of boat do you have in mind for the day?")
    rows = [
        {"id":"BOAT_SPEED","title":"Speedboat","description":""},
        {"id":"BOAT_YACHT","title":"Yacht","description":""},
        {"id":"BOAT_CAT","title":"Catamaran","description":""},
        {"id":"BOAT_ALL","title":"🚤 No estoy segur@ / Interesad@ en todos","description":""},  # 👈 nuevo
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def weddings_guests_list(lang):
    header = ("Invitados" if is_es(lang) else "Guests")
    body   = ("Elige un rango de invitados:" if is_es(lang) else "Choose a guest range:")
    rows = [
        {"id":"WED_PAX_50", "title":"1–50","description":""},
        {"id":"WED_PAX_100","title":"50–100","description":""},
        {"id":"WED_PAX_200","title":"100–200","description":""},
        {"id":"WED_PAX_201","title":"200+","description":""},
        {"id":"WED_PAX_UNK","title":("No sé" if is_es(lang) else "Don’t know"),"description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def pax_list(lang):
    header = ("Personas" if is_es(lang) else "Guests")
    body = ("¿Cuántas personas te van a acompañar en este maravilloso viaje?"
            if is_es(lang) else
            "How many guests will join this trip?")
    rows = [
        {"id":"PAX_5",  "title":"1–5",   "description":""},
        {"id":"PAX_10", "title":"5–10",  "description":""},
        {"id":"PAX_20", "title":"10–20", "description":""},
        {"id":"PAX_21", "title":"20+",   "description":""},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def ask_date(lang):
    return ("¿Tienes una *fecha* o *rango de fechas*?\n\n"
            "Escríbelo así: 15/02/2026, 2026-02-15 o “mayo 2026”.\n\n"
            "Si aún no lo sabes, escribe *Omitir*."
            if is_es(lang) else
            "Do you have a *date* or *date range*?\n\n"
            "Type it like: 2026-02-15, 15/02/2026 or “May 2026”.\n\n"
            "If you don’t know yet, type *Skip*.")


MSG_PAST_ES = "Oye, veo que colocaste una fecha que ya pasó. ¿La revisas y me confirmas?"
MSG_PAST_EN = "Hey, looks like you entered a date that already passed. Could you update it?"

_MONTHS_ES = {
    "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
    "julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12
}
_MONTHS_EN = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12
}

def _parse_date_loose(s: str):
    """Devuelve date o None. Soporta: YYYY-MM-DD, DD/MM/YYYY, 'May 2026'/'mayo 2026' (toma día 1)."""
    if not s: return None
    x = s.strip()
    # ISO
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(x, fmt).date()
        except: pass
    # Latam
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(x, fmt).date()
        except: pass
    # 'May 2026' / 'mayo 2026'
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+|\d{4}", x)
    if len(tokens) >= 2 and tokens[-1].isdigit() and len(tokens[-1]) == 4:
        year = int(tokens[-1])
        month_txt = strip_accents(" ".join(tokens[:-1]).lower()).strip()
        m = _MONTHS_EN.get(month_txt) or _MONTHS_ES.get(month_txt)
        if m:
            return datetime(year, m, 1).date()
    return None

def _validate_future_or_warn(date_text: str, lang: str, tz_name="America/Bogota"):
    """Si detecta fecha y es pasada -> (False, msg). Si no detecta fecha -> (True, None)."""
    d = _parse_date_loose((date_text or "").strip())
    if not d:
        return True, None  # no hay fecha clara; continúa flujo normal
    today_local = datetime.now(ZoneInfo(tz_name)).date()
    if d < today_local:
        return False, (MSG_PAST_ES if is_es(lang) else MSG_PAST_EN)
    return True, None

def after_results_buttons(lang):
    return [
        {"id":"POST_ADD_SERVICE","title":("Añadir otro servicio" if is_es(lang) else "Add another service")},
        {"id":"POST_TALK_TEAM","title":("Hablar con el equipo" if is_es(lang) else "Talk to the team")},
        {"id":"POST_MENU","title":("Volver al menú" if is_es(lang) else "Back to menu")},
    ]

def _fmt_money_usd(v: str) -> str:
    try:
        x = float(v or "0")
        if x <= 0: return ""
        return f"USD {x:,.0f}+"
    except:
        return ""

def format_results(lang, items, unit_label):
    es = is_es(lang)
    if not items:
        return ("No encontré opciones públicas ahora mismo. Te conecto con nuestro equipo para opciones privadas y personalizadas. 🤝"
                if es else
                "I couldn’t find public options right now. I’ll connect you with our team for private & custom options. 🤝")
    lines = []
    head = "Opciones encontradas:" if es else "Options found:"
    lines.append(head)
    for r in items[:TOP_K]:
        name = r.get("name") or r.get("title") or "—"
        loc  = r.get("location") or r.get("city") or ""
        url  = r.get("url_page") or r.get("url") or r.get("link") or ""
        price = _fmt_money_usd(r.get("price_from_usd"))
        capacity = r.get("capacity_max") or ""
        bits = []
        if loc: bits.append(loc)
        if price: bits.append(price + (f"/{unit_label}" if unit_label else ""))
        if capacity:
            bits.append((f"cap. {capacity}" if es else f"cap. {capacity}"))
        sub = " • ".join([b for b in bits if b])

        desc = pick_description(r, lang)  # 👈 NUEVO: descripción debajo

        if url:
            lines.append(f"• {name} — {sub}\n  {url}")
        else:
            lines.append(f"• {name} — {sub}")
        if desc:
            lines.append(f"  — {desc}")  # 👈 línea de descripción
    tail = ("\n¿Quieres que te conecte con nuestro equipo para reservar o ver más opciones?" if es
            else "\nWould you like me to connect you with our team to book or see more options?")
    lines.append(tail)
    return "\n".join(lines)
    
def wa_link_with_text(phone_e164: str, text: str) -> str:
    # Convierte a dígitos (E.164 sin espacios) y arma wa.me con ?text=
    return f"https://wa.me/{wa_click_number(phone_e164)}?text={urllib.parse.quote(text)}"

def build_msg_to_ray(state: dict, owner_name="Ray", city_fallback="Cartagena"):
    """
    Texto prellenado para Ray. EN al inicio + explicación ES. Sin listas ni resumen.
    """
    name = state.get("name") or "Michel"
    city = state.get("city") or city_fallback
    svc  = (state.get("service_type") or "villas").lower()

    # Preferencia legible (usa tus etiquetas ya mapeadas)
    pref = human_pref_label(svc, state.get("lang") or "EN", state.get("category_tag"))
    pref_txt = f" ({pref})" if pref else ""

    # Fecha si el usuario la escribió (si no, no forzar)
    d = state.get("date")
    date_txt = f" for {d}" if d else ""

    # Texto final tal cual solicitaste
    text = (f"Hi Ray, I’m {name}. "
            f"Como le comentaba a Luna, estoy interesad@ en {svc} en {city}{pref_txt}{date_txt}.")
    return text


# ==================== Handoff: mensaje combinado ====================
def handoff_full_message(state, owner_name, wa_num, cal_url, pretty_city):
    # Mensaje compacto con link directo y texto prellenado para Ray
    prefilled = build_msg_to_ray(state, owner_name=owner_name.split()[0], city_fallback=pretty_city or "Cartagena")
    link = wa_link_with_text(wa_num, prefilled)
    es = is_es(state.get("lang"))

    if es:
        lines = [
            f"Te conecto con *{owner_name}* (Two Travel).",
            f"📲 Escribe aquí: {link}",
        ]
        # Si quieres mantener opción de agenda (opcional)
        if cal_url:
            lines.append(f"📆 O agenda una llamada: {cal_url}")
        return "\n".join(lines)
    else:
        lines = [
            f"I’m connecting you with *{owner_name}* (Two Travel).",
            f"📲 Message here: {link}",
        ]
        if cal_url:
            lines.append(f"📆 Or schedule a call: {cal_url}")
        return "\n".join(lines)

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

@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", data)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Ignorar callbacks de estado (delivered/read/etc.)
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

                # Texto / respuesta
                text, reply_id = extract_text_or_reply(m)
                txt_raw = (text or "").strip()

                # ===== /start global =====
                if txt_raw.lower() in ("hola","hello","/start","start","inicio","menu"):
                    SESSIONS[user] = {"step":"lang","lang":"EN","attempts_email":0}
                    wa_send_buttons(user, welcome_text(), opener_buttons())
                    continue

                # Si no hay sesión, forzar idioma
                if user not in SESSIONS:
                    SESSIONS[user] = {"step":"lang","lang":"EN","attempts_email":0}
                    wa_send_buttons(user, welcome_text(), opener_buttons())
                    continue

                state = SESSIONS[user]

                # ===== 0) Idioma =====
                if state["step"] == "lang":
                    rid = (reply_id or "").upper().strip()
                    low = txt_raw.lower()
                    if rid == "LANG_ES" or "español" in low or low == "es":
                        state["lang"] = "ES"
                    else:
                        state["lang"] = "EN"
                    state["step"] = "contact_name"
                    wa_send_text(user, human_intro(state["lang"]))
                    SESSIONS[user] = state
                    continue

                # ===== 1) Nombre =====
                if state["step"] == "contact_name":
                    if not valid_name(txt_raw):
                        wa_send_text(user, ask_fullname(state["lang"]))
                        SESSIONS[user] = state
                        continue
                    state["name"] = normalize_name(txt_raw)
                    state["step"] = "contact_email_choice"
                    state["attempts_email"] = 0
                    wa_send_text(user, ask_email(state["lang"]))
                    wa_send_buttons(user, " ", email_buttons(state["lang"]))
                    SESSIONS[user] = state
                    continue

                # ===== 2) Email (choice) =====
                if state["step"] == "contact_email_choice":
                    rid = (reply_id or "").upper()
                    low = (txt_raw or "").lower()

                    # Permitir teclear email en este paso
                    typed_email = extract_first_email(txt_raw)
                    if typed_email:
                        clean = sanitize_email_input(typed_email)
                        if EMAIL_RE.match(clean):
                            state["email"] = clean
                            state["contact_id"] = hubspot_find_or_create_contact(
                                state.get("name"), state.get("email"), user, state.get("lang")
                            )
                            if is_es(state["lang"]):
                                wa_send_text(user, "¡Perfecto! Registré tu correo. Continuemos 👉")
                            else:
                                wa_send_text(user, "Saved your email. Let’s continue 👉")
                            state["step"] = "city"
                            h,b,btn,rows = city_list(state["lang"])
                            wa_send_list(user, h, b, btn, rows)
                            SESSIONS[user] = state
                            continue

                    if rid == "EMAIL_ENTER":
                        state["step"] = "contact_email_enter"
                        wa_send_text(
                            user,
                            ("Escribe tu correo (ej. nombre@dominio.com)." if is_es(state["lang"])
                             else "Type your email (e.g., name@domain.com).")
                        )
                        SESSIONS[user] = state
                        continue

                    if rid == "EMAIL_USE_WA" or low in ("usar whatsapp","use whatsapp"):
                        state["email"] = f"{user}@whatsapp"
                        state["contact_id"] = hubspot_find_or_create_contact(
                            state.get("name"), state.get("email"), user, state.get("lang")
                        )
                        if is_es(state["lang"]):
                            wa_send_text(user, "Anoté que prefieres continuar por WhatsApp. ¡Gracias! 🙌")
                        else:
                            wa_send_text(user, "Noted you prefer WhatsApp. Thanks! 🙌")
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue

                    if rid == "EMAIL_SKIP" or low in ("skip","saltar","omitir"):
                        state["email"] = ""
                        state["contact_id"] = hubspot_find_or_create_contact(
                            state.get("name"), state.get("email"), user, state.get("lang")
                        )
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue

                    wa_send_buttons(user, " ", email_buttons(state["lang"]))
                    SESSIONS[user] = state
                    continue

                # ===== 2b) Email (enter) =====
                if state["step"] == "contact_email_enter":
                    candidate = sanitize_email_input(txt_raw)
                    if not EMAIL_RE.match(candidate):
                        embedded = extract_first_email(txt_raw)
                        if embedded:
                            candidate = sanitize_email_input(embedded)

                    if EMAIL_RE.match(candidate or ""):
                        state["email"] = candidate
                        state["contact_id"] = hubspot_find_or_create_contact(
                            state.get("name"), state.get("email"), user, state.get("lang")
                        )
                        if is_es(state["lang"]):
                            wa_send_text(user, "¡Perfecto! Registré tu correo. Continuemos 👉")
                        else:
                            wa_send_text(user, "Saved your email. Let’s continue 👉")
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue

                    low = txt_raw.lower()
                    if low in ("", "skip","saltar","omitir","si","sí","yes","ok","dale","listo"):
                        state["email"] = ""
                        state["contact_id"] = hubspot_find_or_create_contact(
                            state.get("name"), state.get("email"), user, state.get("lang")
                        )
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue

                    # Un intento fallido -> avanzar
                    state["attempts_email"] = state.get("attempts_email", 0) + 1
                    if state["attempts_email"] >= 1:
                        state["email"] = ""
                        state["contact_id"] = hubspot_find_or_create_contact(
                            state.get("name"), state.get("email"), user, state.get("lang")
                        )
                        state["step"] = "city"
                        h,b,btn,rows = city_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue

                    wa_send_buttons(user, " ", email_buttons(state["lang"]))
                    state["step"] = "contact_email_choice"
                    SESSIONS[user] = state
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
                        SESSIONS[user] = state
                        continue
                    state["city"] = city
                    state["step"] = "menu"
                    h,b,btn,rows = main_menu_list(state["lang"], state["city"])
                    wa_send_list(user, h, b, btn, rows)
                    SESSIONS[user] = state
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

                        if state["service_type"] == "villas":
                            state["step"] = "villa_pax"
                            h,b,btn,rows = pax_list(state["lang"])
                            wa_send_list(user, h, b, btn, rows)
                            SESSIONS[user] = state
                            continue

                        if state["service_type"] == "boats":
                            state["step"] = "boat_cat"
                            h,b,btn,rows = boat_categories(state["lang"])
                            wa_send_list(user, h, b, btn, rows)
                            SESSIONS[user] = state
                            continue

                        if state["service_type"] == "islands":
                            top = filter_catalog("islands", state.get("city"), 0, None)
                            unit = "noche" if is_es(state["lang"]) else "night"
                            state["last_top"] = top
                            append_history(state, "islands")
                            wa_send_text(user, format_results(state["lang"], top, unit))

                            owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                            notify_sales("Lead Islands", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)

                            if not top:
                                msg = handoff_full_message(state, owner_name, wa_num, cal_url, pretty_city)
                                wa_send_text(user, msg)

                            state["step"] = "post_results"
                            wa_send_buttons(
                                user,
                                ("¿Cómo podemos seguir ayudándote?" if is_es(state["lang"]) else "How can we keep helping?"),
                                after_results_buttons(state["lang"])
                            )
                            SESSIONS[user] = state
                            continue

                        if state["service_type"] == "weddings":
                            state["step"] = "wed_guests"
                            h,b,btn,rows = weddings_guests_list(state["lang"])
                            wa_send_list(user, h, b, btn, rows)
                            SESSIONS[user] = state
                            continue

                        if state["service_type"] in ("concierge","team"):
                            owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                            msg = handoff_full_message(state, owner_name, wa_num, cal_url, pretty_city)
                            wa_send_text(user, msg)

                            contact_id = state.get("contact_id") or hubspot_find_or_create_contact(
                                state.get("name"), state.get("email"), user, state.get("lang")
                            )
                            title = f"[{pretty_city}] {state['service_type'].title()} via WhatsApp"
                            desc  = (f"City: {pretty_city}\n"
                                     f"Service: {state['service_type']}\n"
                                     f"Pax: {state.get('pax') or 'TBD'}\n"
                                     f"Date: {state.get('date') or 'TBD'}\n"
                                     f"Email: {state.get('email') or '—'}\n"
                                     f"Lang: {state.get('lang')}\n"
                                     f"Source: WhatsApp Bot")
                            hist_block = build_history_lines(state)
                            if hist_block:
                                desc += f"\nHistory:\n{hist_block}"
                            if contact_id:
                                hubspot_create_deal(contact_id, owner_id, title, desc)

                            notify_sales("Talk to Team / Concierge", state, user,
                                         cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)

                            state["step"] = "post_results"
                            wa_send_buttons(
                                user,
                                ("¿Qué más necesitas?" if is_es(state["lang"]) else "What else do you need?"),
                                after_results_buttons(state["lang"])
                            )
                            SESSIONS[user] = state
                            continue

                    h,b,btn,rows = main_menu_list(state["lang"], state.get("city"))
                    wa_send_list(user, h, b, btn, rows)
                    SESSIONS[user] = state
                    continue

                # ===== VILLAS → PAX =====
                if state["step"] == "villa_pax":
                    rid = (reply_id or "").upper()
                    if not rid or not rid.startswith("PAX_"):
                        h,b,btn,rows = pax_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue
                    pax = pax_from_reply(rid)
                    state["pax"] = pax
                    state["step"] = "villa_cat"
                    h,b,btn,rows = villa_categories(state["lang"])
                    wa_send_list(user, h, ("Elige rango de *habitaciones*:" if is_es(state["lang"]) else "Choose *bedrooms* range:"), btn, rows)
                    SESSIONS[user] = state
                    continue

                # ===== VILLAS → CATEGORÍA =====
                if state["step"] == "villa_cat":
                    rid = (reply_id or "").upper()
                    if rid not in ("VILLA_3_6","VILLA_7_10","VILLA_11_14","VILLA_15P"):
                        h,b,btn,rows = villa_categories(state["lang"])
                        wa_send_list(user, h, ("Elige rango de *habitaciones*:" if is_es(state["lang"]) else "Choose *bedrooms* range:"), btn, rows)
                        SESSIONS[user] = state
                        continue
                    category_tag = {
                        "VILLA_3_6":"bed_3_6",
                        "VILLA_7_10":"bed_7_10",
                        "VILLA_11_14":"bed_11_14",
                        "VILLA_15P":"bed_15_plus",
                    }[rid]
                    state["category_tag"] = category_tag
                    state["step"] = "date"
                    state["pending_service"] = "villas"
                    wa_send_text(user, ask_date(state["lang"]))
                    SESSIONS[user] = state
                    continue

                # ===== BOATS → categoría =====
                if state["step"] == "boat_cat":
                    rid = (reply_id or "").upper()
                    # 🔧 Acepta también BOAT_ALL (para no filtrar por categoría)
                    if rid not in ("BOAT_SPEED","BOAT_YACHT","BOAT_CAT","BOAT_ALL"):
                        h,b,btn,rows = boat_categories(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue

                    if rid == "BOAT_ALL":
                        state["category_tag"] = None  # mostrar todas las categorías
                    else:
                        state["category_tag"] = {
                            "BOAT_SPEED":"type_speedboat",
                            "BOAT_YACHT":"type_yacht",
                            "BOAT_CAT":"type_catamaran",
                        }[rid]

                    state["step"] = "boat_pax"
                    h,b,btn,rows = pax_list(state["lang"])
                    wa_send_list(user, h, b, btn, rows)
                    SESSIONS[user] = state
                    continue

                # ===== BOATS → pax => FECHA =====
                if state["step"] == "boat_pax":
                    rid = (reply_id or "").upper()
                    if not rid or not rid.startswith("PAX_"):
                        h,b,btn,rows = pax_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue
                    pax = pax_from_reply(rid)
                    state["pax"] = pax
                    state["step"] = "date"
                    state["pending_service"] = "boats"
                    wa_send_text(user, ask_date(state["lang"]))
                    SESSIONS[user] = state
                    continue

                # ===== WEDDINGS → invitados => FECHA =====
                if state["step"] == "wed_guests":
                    rid = (reply_id or "").upper()
                    if rid not in ("WED_PAX_50","WED_PAX_100","WED_PAX_200","WED_PAX_201","WED_PAX_UNK"):
                        h,b,btn,rows = weddings_guests_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue
                    pax = pax_from_reply(rid)
                    state["pax"] = pax
                    state["step"] = "date"
                    state["pending_service"] = "weddings"
                    wa_send_text(user, ask_date(state["lang"]))
                    SESSIONS[user] = state
                    continue

                # ===== FECHA (común) => validación + resultados / handoff =====
                if state["step"] == "date":
                    low = (txt_raw or "").strip().lower()
                    skip_tokens = {"omitir","skip","no sé","nose","tbd","na","n/a","later","después","luego","aún no","no tengo","no se","todavia no","aun no"}

                    # 🔧 Validación de fecha pasada (si el usuario sí escribió una fecha)
                    if low not in skip_tokens:
                        ok_future, warn_msg = _validate_future_or_warn(txt_raw, state.get("lang"))
                        if not ok_future:
                            wa_send_text(user, warn_msg)
                            wa_send_text(user, ask_date(state["lang"]))
                            SESSIONS[user] = state
                            continue

                    state["date"] = None if low in skip_tokens else (text or "").strip()
                    svc = state.get("pending_service")

                    if svc == "villas":
                        top = filter_catalog("villas", state["city"], state.get("pax") or 0, state.get("category_tag"))
                        unit = "noche" if is_es(state["lang"]) else "night"
                        state["last_top"] = top
                        append_history(state, "villas")
                        wa_send_text(user, format_results(state["lang"], top, unit))

                        owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                        notify_sales("Lead Villas", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)

                        if not top:
                            msg = handoff_full_message(state, owner_name, wa_num, cal_url, pretty_city)
                            wa_send_text(user, msg)

                    elif svc == "boats":
                        top = filter_catalog("boats", state["city"], state.get("pax") or 0, state.get("category_tag"))
                        unit = "día" if is_es(state["lang"]) else "day"
                        state["last_top"] = top
                        append_history(state, "boats")
                        wa_send_text(user, format_results(state["lang"], top, unit))

                        owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                        notify_sales("Lead Boats", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)

                        if not top:
                            msg = handoff_full_message(state, owner_name, wa_num, cal_url, pretty_city)
                            wa_send_text(user, msg)

                    elif svc == "weddings":
                        top = filter_catalog("weddings", state["city"], state.get("pax") or 0, state.get("category_tag"))
                        unit = "evento" if is_es(state["lang"]) else "event"
                        state["last_top"] = top
                        append_history(state, "weddings")
                        wa_send_text(user, format_results(state["lang"], top, unit))

                        owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                        notify_sales("Lead Weddings", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)

                        if not top:
                            msg = handoff_full_message(state, owner_name, wa_num, cal_url, pretty_city)
                            wa_send_text(user, msg)

                    state["step"] = "post_results"
                    wa_send_buttons(
                        user,
                        ("¿Cómo podemos seguir ayudándote?" if is_es(state["lang"]) else "How can we keep helping?"),
                        after_results_buttons(state["lang"])
                    )
                    SESSIONS[user] = state
                    continue

                # ===== Post resultados =====
                if state["step"] == "post_results":
                    rid = (reply_id or "").upper()

                    if rid == "POST_ADD_SERVICE":
                        state["step"] = "menu"
                        h,b,btn,rows = main_menu_list(state["lang"], state.get("city"))
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue

                    if rid == "POST_TALK_TEAM":
                        owner_name, owner_id, cal_url, pretty_city, wa_num = owner_for_city(state.get("city"))
                        msg = handoff_full_message(state, owner_name, wa_num, cal_url, pretty_city)
                        wa_send_text(user, msg)

                        contact_id = state.get("contact_id") or hubspot_find_or_create_contact(
                            state.get("name"), state.get("email"), user, state.get("lang")
                        )
                        title = f"[{pretty_city}] Talk to the Team via WhatsApp"
                        desc  = f"City: {pretty_city}\nService: {state.get('service_type') or 'N/A'}\nPax: {state.get('pax') or 'TBD'}\nDate: {state.get('date') or 'TBD'}\nEmail: {state.get('email') or '—'}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                        hist_block = build_history_lines(state)
                        if state.get("last_top"):
                            tops = "; ".join([f"{r.get('name')}→{r.get('url_page')}" for r in state['last_top'][:TOP_K]])
                            desc += f"\nTop shown: {tops}"
                        if hist_block:
                            desc += f"\nHistory:\n{hist_block}"
                        if contact_id:
                            hubspot_create_deal(contact_id, owner_id, title, desc)

                        notify_sales("Talk to Team", state, user, cal_url=cal_url, owner_name=owner_name, pretty_city=pretty_city)

                        state["step"] = "post_results"
                        wa_send_buttons(
                            user,
                            ("¿Qué más necesitas?" if is_es(state["lang"]) else "What else do you need?"),
                            [
                                {"id":"POST_ADD_SERVICE","title":("Añadir otro servicio" if is_es(state["lang"]) else "Add another service")},
                                {"id":"POST_MENU","title":("Volver al menú" if is_es(state["lang"]) else "Back to menu")},
                            ]
                        )
                        SESSIONS[user] = state
                        continue

                    if rid == "POST_MENU":
                        state["step"] = "menu"
                        h,b,btn,rows = main_menu_list(state["lang"], state.get("city"))
                        wa_send_list(user, h, b, btn, rows)
                        SESSIONS[user] = state
                        continue

                    wa_send_buttons(
                        user,
                        ("¿Quieres añadir otro servicio o hablar con el equipo?" if is_es(state["lang"]) else
                         "Would you like to add another service or talk to the team?"),
                        after_results_buttons(state["lang"])
                    )
                    SESSIONS[user] = state
                    continue

    return {"ok": True}
