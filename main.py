# main.py
import os, re, csv, io, requests, datetime
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

# ================== ENV ==================
VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN") or "").strip()
WA_TOKEN     = (os.getenv("WA_ACCESS_TOKEN") or "").strip()
WA_PHONE_ID  = (os.getenv("WA_PHONE_NUMBER_ID") or "").strip()

TOP_K = int(os.getenv("TOP_K", "3"))

HUBSPOT_TOKEN         = (os.getenv("HUBSPOT_TOKEN") or "").strip()
HUBSPOT_PIPELINE_ID   = (os.getenv("HUBSPOT_PIPELINE_ID") or "default").strip()
HUBSPOT_DEALSTAGE_ID  = (os.getenv("HUBSPOT_DEALSTAGE_ID") or "appointmentscheduled").strip()
GOOGLE_SHEET_CSV_URL  = (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()

# Dueños por ciudad (email)
CITY_OWNERS = {
    "cartagena": "sofia@two.travel",
    "medellin":  "ross@two.travel",
    "méxico":    "ray@two.travel",
    "mexico":    "ray@two.travel",
    "tulum":     "sofia@two.travel",
    "cdmx":      "ray@two.travel",
}

ALLOWED_CITIES = ["Cartagena", "Medellín", "Tulum", "CDMX", "México"]

# ================== STATE ==================
SESSIONS = {}  # { phone: {..., "attempts": {"step": int}, "options": {"type":..., "items":[...] } } }

# ================== WhatsApp helpers ==================
BASE_URL = "https://graph.facebook.com/v23.0"

def _post_wa(path, payload):
    url = f"{BASE_URL}/{WA_PHONE_ID}/{path}"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print("WA send:", r.status_code, r.text[:200])
    if r.status_code == 401:
        print("⚠️ WA TOKEN INVALID/EXPIRED. Revisa WA_ACCESS_TOKEN en Render.")
    return r

def wa_send_text(to: str, body: str):
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}}
    _post_wa("messages", payload)

def wa_send_buttons(to: str, body: str, buttons):
    """
    buttons = [("ID1","Título 1"), ("ID2","Título 2"), ("ID3","Título 3")]
    """
    btns = [{"type":"reply","reply":{"id":bid, "title":title[:20]}} for bid,title in buttons[:3]]
    payload = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":body},
            "action":{"buttons": btns}
        }
    }
    _post_wa("messages", payload)

def wa_send_list(to: str, body: str, section_title: str, rows):
    """
    rows = [("id","Título","desc"), ...]  # hasta ~10
    """
    rows_payload = [{"id":rid, "title":title[:24], "description":(desc or "")[:60]} for rid,title,desc in rows]
    payload = {
      "messaging_product":"whatsapp","to":to,"type":"interactive",
      "interactive":{
        "type":"list",
        "body":{"text":body},
        "action":{"button":"Elegir","sections":[{"title":section_title, "rows":rows_payload}]}
      }
    }
    _post_wa("messages", payload)

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
    return ""

def save_options(state: dict, opt_type: str, items_titles):
    """
    Guarda opciones para permitir elección con números.
    items_titles = ["Cartagena","Medellín","Tulum"]
    """
    state["options"] = {"type": opt_type, "items": items_titles}

def resolve_numeric_choice(text: str, state: dict):
    if not state.get("options"): return None
    m = re.fullmatch(r"\s*([1-9])\s*", (text or ""))
    if not m: return None
    idx = int(m.group(1)) - 1
    items = state["options"].get("items") or []
    if 0 <= idx < len(items):
        return items[idx]
    return None

# ================== HubSpot helpers ==================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def hubspot_get_owner_id_by_email(email: str):
    try:
        r = requests.get(
            "https://api.hubapi.com/crm/v3/owners",
            headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}"},
            timeout=20
        )
        if not r.ok: 
            print("HubSpot owners:", r.status_code, r.text[:200]); 
            return None
        for owner in r.json().get("results", []):
            if (owner.get("email") or "").lower() == (email or "").lower():
                return owner.get("id")
    except Exception as e:
        print("HubSpot owners error:", e)
    return None

def hubspot_upsert_contact(name: str, email: str, phone: str, lang: str):
    if not HUBSPOT_TOKEN:
        print("WARN: HUBSPOT_TOKEN missing"); 
        return None
    base = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    props = {
        "email": email or None,
        "firstname": (name.split()[0] if name else None),
        "lastname": (" ".join(name.split()[1:]) if name and len(name.split())>1 else None),
        "phone": phone,
        "lifecyclestage": "lead",
        "preferred_language": ("es" if (lang or "ES").upper().startswith("ES") else "en"),
        "source": "WhatsApp Bot",
    }
    # create
    r = requests.post(base, headers=headers, json={"properties": props}, timeout=20)
    if r.status_code == 201:
        return r.json().get("id")
    # conflict → search + update
    if r.status_code == 409 and email:
        s = requests.post(f"{base}/search", headers=headers, json={
            "filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}],
            "properties":["email"]}, timeout=20)
        if s.ok and s.json().get("results"):
            cid = s.json()["results"][0]["id"]
            requests.patch(f"{base}/{cid}", headers=headers, json={"properties": props}, timeout=20)
            return cid
    print("HubSpot contact error:", r.status_code, r.text[:200])
    return None

def hubspot_create_deal(state: dict, contact_id: str):
    if not HUBSPOT_TOKEN: 
        return None
    city = (state.get("city") or "Cartagena").lower()
    svc  = (state.get("service_type") or "villas")
    date = (state.get("trip_date_iso") or "")
    pax  = int(state.get("pax") or 0)
    dealname = f"{svc.title()} - {city.title()} - {pax or 'N/A'} pax"
    owner_email = CITY_OWNERS.get(city) or CITY_OWNERS.get(city.replace("é","e"), None)
    owner_id = hubspot_get_owner_id_by_email(owner_email) if owner_email else None

    payload = {"properties":{
        "dealname": dealname,
        "pipeline": HUBSPOT_PIPELINE_ID,
        "dealstage": HUBSPOT_DEALSTAGE_ID,
        "closedate": None,
        "notes_last_contacted": None,
        "hs_lead_status":"NEW",
        "source":"WhatsApp Bot",
        "city": city.title(),
        "service_type": svc,
        "trip_date": date or None,
        "party_size": str(pax) if pax else None,
    }}
    if owner_id:
        payload["properties"]["hubspot_owner_id"] = owner_id

    r = requests.post("https://api.hubapi.com/crm/v3/objects/deals",
                      headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}","Content-Type":"application/json"},
                      json=payload, timeout=20)
    if not r.ok:
        print("HubSpot deal error:", r.status_code, r.text[:200])
        return None
    deal_id = r.json().get("id")
    # Associate contact → deal
    if contact_id:
        try:
            requests.put(f"https://api.hubapi.com/crm/v4/objects/deals/{deal_id}/associations/contacts/{contact_id}/deal_to_contact",
                         headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}"}, timeout=20)
        except: pass
    return deal_id

# ================== Catalog ==================
def load_catalog():
    if not GOOGLE_SHEET_CSV_URL:
        print("WARN: GOOGLE_SHEET_CSV_URL missing")
        return []
    r = requests.get(GOOGLE_SHEET_CSV_URL, timeout=30)
    if not r.ok:
        print("Catalog download error:", r.status_code, r.text[:200]); 
        return []
    rows = []
    content = r.content.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k,v in row.items()})
    return rows

def find_top(service: str, city: str, pax: int, prefs: str, top_k: int = TOP_K):
    service = (service or "").strip().lower()
    city    = (city or "").strip().lower()
    prefs_l = [p.strip().lower() for p in (prefs or "").split(",") if p.strip()]
    rows = load_catalog()
    if not rows: 
        return []
    def row_ok(r):
        if (r.get("service_type","").lower() != service): return False
        if city and (r.get("city","").lower() != city):   return False
        try: cap = int(float(r.get("capacity_max","0") or "0"))
        except: cap = 0
        if pax and cap < pax: return False
        if prefs_l:
            tags = [t.strip().lower() for t in (r.get("preference_tags","") or "").split(",") if t.strip()]
            if not any(p in tags for p in prefs_l): return False
        return True
    candidates = [r for r in rows if row_ok(r)]
    def price_val(r):
        try:   return float(r.get("price_from_usd","999999") or "999999")
        except:return 999999.0
    candidates.sort(key=price_val)
    return candidates[:max(1,int(top_k or 1))]

# ================== Copy / UX ==================
def is_es(lang): return (lang or "ES").upper().startswith("ES")

def opener_text():
    return ("¡Bienvenid@ a Two Travel! ✨\n"
            "Elige tu idioma / Choose your language:")

def contact_name_msg(lang):
    return ("Para cotizar, ¿cuál es tu *nombre completo*?\n(Escribe o toca *1. Usar mi nombre de WhatsApp*)"
            if is_es(lang) else
            "To quote, what's your *full name*?\n(Type or tap *1. Use my WhatsApp name*)")

def contact_email_msg(lang):
    return ("📧 *Correo electrónico* (o toca *1. Saltar por ahora*)"
            if is_es(lang) else
            "📧 *Email address* (or tap *1. Skip for now*)")

def main_menu(lang):
    return ("¿Qué necesitas hoy?" if is_es(lang) else "What do you need today?")

MENU_BUTTONS = [
    ("VILLAS","Villas & Casas 🏠"),
    ("BOATS","Botes & Yates 🚤"),
    ("WEDDINGS","Bodas & Eventos 💍🎉"),
]  # luego enviamos otro bloque con Concierge / Ventas si hace falta

def q_city(lang): 
    return ("Elige *ciudad*:" if is_es(lang) else "Choose *city*:")

def q_date(lang):
    return ("¿Cuándo viajan? (ej. 2025-11-12 / 12-11-2025 / hoy / mañana) "
            "\n*1. Aún no sé la fecha*"
            if is_es(lang) else
            "When is the trip? (e.g. 2025-11-12 / 11-12-2025 / today / tomorrow) "
            "\n*1. I don't know yet*")

def q_pax(lang):
    return ("¿Para cuántas *personas*?\n*1. 2 personas*" if is_es(lang) else "How many *guests*?\n*1. 2 guests*")

def reply_topN(lang, items, unit):
    if not items:
        return ("No veo opciones con esos filtros. ¿Probamos con otro tamaño de grupo?"
                if is_es(lang) else "No matches. Try a different party size?")
    lines = []
    if is_es(lang):
        lines.append(f"Top {len(items)} opciones (precios *desde*):")
        for r in items:
            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} pax) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("La *disponibilidad final* la confirma el equipo de *ventas*. ¿Quieres que te conecte?")
    else:
        lines.append(f"Top {len(items)} options (*prices from*):")
        for r in items:
            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} guests) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("Final *availability* is confirmed by *sales*. Connect you now?")
    return "\n".join(lines)

def add_more_or_sales(lang):
    return ("¿Deseas *añadir otro servicio* o *conectar con ventas*?"
            if is_es(lang) else "Do you want to *add another service* or *connect with sales*?")

# ================== Parsing tolerante ==================
def normalize_name(fullname: str) -> str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    if not tokens: return ""
    return " ".join(tokens[:3]).title()

def parse_date_soft(text: str, lang: str):
    if not text: return None
    t = text.strip().lower()
    today = datetime.date.today()
    if t in ("1", "skip", "saltar", "aun no", "aún no", "no se", "no sé", "i don't know", "i dont know"):
        return None
    if t in ("hoy","today"):     return today.isoformat()
    if t in ("mañana","manana","tomorrow"): return (today + datetime.timedelta(days=1)).isoformat()
    t = re.sub(r"[\.]", "-", t)
    # YYYY-MM-DD or YYYY/M/D
    m = re.match(r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*$", t)
    if m:
        y,mn,d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try: return datetime.date(y,mn,d).isoformat()
        except: return None
    # DD/MM/YYYY  or  MM-DD-YYYY (detect by >12)
    m = re.match(r"^\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s*$", t)
    if m:
        a,b,y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # if first > 12 → it's DD/MM/YYYY
        if a > 12:
            d,mn = a,b
        else:
            # assume DD/MM if lang ES; MM/DD if EN
            if is_es(lang):
                d,mn = a,b
            else:
                mn,d = a,b
        try: return datetime.date(y,mn,d).isoformat()
        except: return None
    return None

def parse_int_soft(text, default):
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else default

def inc_attempt(state, step):
    state.setdefault("attempts", {}).setdefault(step, 0)
    state["attempts"][step] += 1
    return state["attempts"][step]

# ================== FastAPI hooks ==================
@app.on_event("startup")
async def startup():
    print("BOOT> WA_PHONE_ID:", repr(WA_PHONE_ID))
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

@app.get("/")
def root():
    return {"ok": True}

# Verify
@app.get("/wa-webhook")
async def verify(req: Request):
    mode = req.query_params.get("hub.mode")
    token = req.query_params.get("hub.verify_token")
    challenge = req.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("forbidden", status_code=403)

# Incoming
@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", data)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            if v.get("statuses"):  # ignore delivery statuses
                continue

            for m in v.get("messages", []):
                user = m.get("from")
                if not user: 
                    continue

                # Primera vez → bienvenida con botones idioma
                if user not in SESSIONS:
                    SESSIONS[user] = {"step":"lang","lang":"ES"}
                    wa_send_buttons(user, opener_text(), [("ES","ES 🇪🇸"),("EN","EN 🇺🇸")])
                    save_options(SESSIONS[user], "lang", ["ES","EN"])
                    continue

                text = extract_text(m)
                state = SESSIONS[user]
                # Si manda un número, intenta mapear a la última lista
                choice_from_number = resolve_numeric_choice(text, state)

                # ===== Step: language =====
                if state["step"] == "lang":
                    low = (choice_from_number or text or "").strip().lower()
                    if low in ("es","español","es 🇪🇸"):
                        state["lang"]="ES"
                    elif low in ("en","english","en 🇺🇸"):
                        state["lang"]="EN"
                    else:
                        # reintento una vez, luego default ES y sigo
                        if inc_attempt(state,"lang") == 1:
                            wa_send_buttons(user, opener_text(), [("ES","ES 🇪🇸"),("EN","EN 🇺🇸")])
                            save_options(state,"lang",["ES","EN"]); 
                            continue
                        state["lang"]="ES"
                    state["step"]="contact_name"
                    wa_send_buttons(user, contact_name_msg(state["lang"]), [("USE_WA","1. Usar mi nombre"),("TYPE","2. Escribir")])
                    save_options(state,"name_mode",["Usar","Escribir"])
                    continue

                # ===== Step: contact_name =====
                if state["step"] == "contact_name":
                    if choice_from_number and choice_from_number.startswith("Usar"):
                        name = (v.get("contacts",[{}])[0].get("profile",{}).get("name") or "")
                        name = normalize_name(name) or "Invitado Two Travel"
                    else:
                        name = normalize_name(text)
                    if not name:
                        if inc_attempt(state,"contact_name") == 1:
                            wa_send_buttons(user, contact_name_msg(state["lang"]), [("USE_WA","1. Usar mi nombre"),("TYPE","2. Escribir")])
                            save_options(state,"name_mode",["Usar","Escribir"]); 
                            continue
                        name = "Invitado Two Travel"
                    state["name"] = name
                    state["step"] = "contact_email"
                    wa_send_buttons(user, contact_email_msg(state["lang"]), [("SKIP","1. Saltar / Skip")])
                    save_options(state,"email",["Skip"])
                    continue

                # ===== Step: contact_email =====
                if state["step"] == "contact_email":
                    email = None
                    if choice_from_number and "skip" in choice_from_number.lower():
                        email = None
                    elif EMAIL_RE.match(text or ""):
                        email = text.strip()
                    else:
                        if inc_attempt(state,"contact_email") == 1:
                            wa_send_buttons(user, contact_email_msg(state["lang"]), [("SKIP","1. Saltar / Skip")])
                            save_options(state,"email",["Skip"]); 
                            continue
                        email = None  # seguimos sin email
                    state["email"] = email
                    # HubSpot: contacto
                    try:
                        state["hubspot_contact_id"] = hubspot_upsert_contact(state.get("name"), email, user, state.get("lang"))
                    except Exception as e:
                        print("HubSpot contact exception:", e)
                    # Menú principal
                    state["step"]="menu"
                    wa_send_buttons(user, main_menu(state["lang"]), MENU_BUTTONS)
                    save_options(state,"menu",[b[1] for b in MENU_BUTTONS])
                    continue

                # ===== Step: menu =====
                if state["step"] == "menu":
                    t = (choice_from_number or text or "").lower()
                    if "villa" in t or "casas" in t:
                        state["service_type"]="villas"; state["step"]="city"
                    elif "yate" in t or "boat" in t or "botes" in t:
                        state["service_type"]="boats";  state["step"]="city"
                    elif "boda" in t or "event" in t:
                        state["service_type"]="weddings"; state["step"]="city"
                    else:
                        if inc_attempt(state,"menu")==1:
                            wa_send_buttons(user, main_menu(state["lang"]), MENU_BUTTONS)
                            save_options(state,"menu",[b[1] for b in MENU_BUTTONS]); 
                            continue
                        state["service_type"]="villas"; state["step"]="city"
                    # Ciudad (lista + números)
                    city_rows = []
                    for i,ct in enumerate(ALLOWED_CITIES, start=1):
                        city_rows.append((f"CT_{ct.lower()}", f"{i}. {ct}", ""))
                    wa_send_list(user, q_city(state["lang"]), "Ciudades", city_rows)
                    wa_send_text(user, ("\nResponde con el *número* 1–5 si prefieres." if is_es(state["lang"]) else "\nYou can simply reply with *number* 1–5."))
                    save_options(state,"city",ALLOWED_CITIES)
                    continue

                # ===== Step: city =====
                if state["step"] == "city":
                    chosen = choice_from_number or text
                    # normaliza y valida
                    city_norm = (chosen or "").strip().lower().replace("méxico","mexico")
                    matched = None
                    for ct in ALLOWED_CITIES:
                        if city_norm == ct.lower() or ct.lower().startswith(city_norm):
                            matched = ct; break
                    if not matched:
                        if inc_attempt(state,"city")==1:
                            # re-mostrar
                            city_rows = []
                            for i,ct in enumerate(ALLOWED_CITIES, start=1):
                                city_rows.append((f"CT_{ct.lower()}", f"{i}. {ct}", ""))
                            wa_send_list(user, q_city(state["lang"]), "Ciudades", city_rows)
                            wa_send_text(user, ("\nResponde con el *número* 1–5." if is_es(state["lang"]) else "\nReply with *1–5*."))
                            save_options(state,"city",ALLOWED_CITIES); 
                            continue
                        matched = "Cartagena"
                    state["city"] = matched
                    state["step"] = "date"
                    wa_send_text(user, q_date(state["lang"]))
                    continue

                # ===== Step: date (soft) =====
                if state["step"] == "date":
                    iso = parse_date_soft(text, state.get("lang"))
                    if not iso and inc_attempt(state,"date")==1:
                        wa_send_text(user, q_date(state["lang"]))
                        continue
                    state["trip_date_iso"] = iso  # puede ser None
                    state["step"] = "pax"
                    wa_send_text(user, q_pax(state["lang"]))
                    continue

                # ===== Step: pax (soft) =====
                if state["step"] == "pax":
                    pax = parse_int_soft(text, 2)
                    state["pax"] = pax
                    # Buscar top
                    if not GOOGLE_SHEET_CSV_URL:
                        wa_send_text(user, "Por ahora te conecto con ventas para una cotización personalizada.")
                    else:
                        unit = "noche" if is_es(state["lang"]) else "night"
                        svc  = "villas" if state.get("service_type")!="boats" else "boats"
                        top  = find_top(svc, (state.get("city") or "").lower(), pax, "", TOP_K)
                        wa_send_text(user, reply_topN(state["lang"], top, unit))
                    # Crear deal
                    try:
                        state["hubspot_deal_id"] = hubspot_create_deal(state, state.get("hubspot_contact_id"))
                    except Exception as e:
                        print("HubSpot deal exception:", e)
                    # Cierre / siguiente
                    state["step"]="post_results"
                    wa_send_buttons(user, add_more_or_sales(state["lang"]), [("ADD","Añadir otro"),("SALES","Conectar ventas")])
                    save_options(state,"post",["Añadir otro","Conectar ventas"])
                    continue

                # ===== Step: post_results =====
                if state["step"] == "post_results":
                    t = (choice_from_number or text or "").lower()
                    if "añadir" in t or "add" in t or "otro" in t or "another" in t:
                        state["step"]="menu"
                        wa_send_buttons(user, main_menu(state["lang"]), MENU_BUTTONS)
                        save_options(state,"menu",[b[1] for b in MENU_BUTTONS])
                        continue
                    # default → ventas
                    owner_name = "Two Travel Sales"
                    team = "TwoTravel"
                    wa_send_text(user, ("Te conecto con nuestro equipo para confirmar *disponibilidad* y cerrar la *reserva*."
                                        if is_es(state["lang"]) else
                                        "Connecting you with our team to confirm *availability* and finalize the *booking*."))
                    state["step"]="handoff"
                    continue

    return {"ok": True}
