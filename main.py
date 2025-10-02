# main.py
import os, re, csv, io, requests, datetime
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

# ==================== ENV / CONFIG ====================
VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN") or "").strip()
WA_TOKEN     = (os.getenv("WA_ACCESS_TOKEN") or "").strip()
WA_PHONE_ID  = (os.getenv("WA_PHONE_NUMBER_ID") or "").strip()

# HubSpot
HUBSPOT_TOKEN       = (os.getenv("HUBSPOT_TOKEN") or "").strip()
HUBSPOT_OWNER_SOFIA = (os.getenv("HUBSPOT_OWNER_SOFIA") or "").strip()  # opcional
HUBSPOT_OWNER_ROSS  = (os.getenv("HUBSPOT_OWNER_ROSS")  or "").strip()  # opcional
HUBSPOT_OWNER_RAY   = (os.getenv("HUBSPOT_OWNER_RAY")   or "").strip()  # opcional
HUBSPOT_PIPELINE_ID  = (os.getenv("HUBSPOT_PIPELINE_ID")  or "").strip()  # opcional
HUBSPOT_DEALSTAGE_ID = (os.getenv("HUBSPOT_DEALSTAGE_ID") or "").strip()  # opcional

# Calendarios (fallback a los que pasaste)
CAL_RAY   = (os.getenv("CAL_RAY")   or "https://meetings.hubspot.com/ray-kanevsky?uuid=280bb17d-4006-4bd1-9560-9cefa9752d5d").strip()
CAL_SOFIA = (os.getenv("CAL_SOFIA") or "https://marketing.two.travel/meetings/sofia217").strip()
CAL_ROSS  = (os.getenv("CAL_ROSS")  or "https://meetings.hubspot.com/ross334?uuid=68031520-950b-4493-b5ad-9cde268edbc8").strip()

# Catálogo
GOOGLE_SHEET_CSV_URL = (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()
TOP_K = int(os.getenv("TOP_K", "3"))  # 2 o 3

# Estado (MVP en memoria)
SESSIONS = {}  # { phone: {...} }

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
                "sections":[{"title":"Select one","rows": rows}]
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

def extract_pax(text: str) -> int:
    if not text:
        return 0
    m = re.search(r'(\d{1,3})\s*(pax|persona|personas|guest|guests)?', text.lower())
    try:
        return int(m.group(1)) if m else 0
    except:
        return 0

def find_top_relaxed(service: str, city: str, pax: int, prefs: str, top_k: int = TOP_K):
    rows = load_catalog()
    if not rows:
        return []

    service = (service or "").strip().lower()
    city    = (city or "").strip().lower()
    prefs_l = [p.strip().lower() for p in (prefs or "").split(",") if p.strip()]

    def ok(r, use_service=True, use_city=True, use_pax=True, use_prefs=True):
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
        if use_prefs and prefs_l:
            tags = [t.strip().lower() for t in (r.get("preference_tags","") or "").split(",") if t.strip()]
            if not any(p in tags for p in prefs_l):
                return False
        return True

    attempts = [
        dict(use_service=True, use_city=True, use_pax=True,  use_prefs=True),
        dict(use_service=True, use_city=True, use_pax=True,  use_prefs=False),
        dict(use_service=True, use_city=True, use_pax=False, use_prefs=False),
        dict(use_service=True, use_city=False,use_pax=False, use_prefs=False),
        dict(use_service=False,use_city=True, use_pax=False, use_prefs=False),
        dict(use_service=False,use_city=False,use_pax=False, use_prefs=False),
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
    return (lang or "ES").upper().startswith("ES")

def welcome_text():
    return ("*Two Travel*\n"
            "Bienvenido/a 🛎️✨\n\n"
            "Elige tu idioma / Choose your language:")

def opener_buttons():
    return [
        {"id":"LANG_ES","title":"🇪🇸 Español"},
        {"id":"LANG_EN","title":"🇺🇸 English"}
    ]

def ask_fullname(lang):
    return ("Para iniciar tu cotización, por favor escribe tu *Nombre y Apellido*."
            if is_es(lang) else
            "To start your quote, please type your *First and Last Name*.")

def ask_email(lang):
    return ("Perfecto. Ahora tu *correo electrónico* (ej. nombre@dominio.com)."
            if is_es(lang) else
            "Great. Now your *email address* (e.g., name@domain.com).")

def polite_email_retry(lang):
    return ("Ese correo no parece válido. ¿Puedes revisarlo? Si prefieres, seguimos y lo confirmamos luego."
            if is_es(lang) else
            "That email looks invalid. Could you check it? If you prefer, we can proceed and confirm later.")

def main_menu_list(lang):
    header = "Two Travel"
    body   = ("¿Qué necesitas hoy?" if is_es(lang) else "What do you need today?")
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
    header = "Ciudad / City"
    body   = ("Elige la ciudad." if is_es(lang) else "Choose the city.")
    rows = [
        {"id":"CITY_CARTAGENA","title":"Cartagena","description":("Colombia" if is_es(lang) else "Colombia")},
        {"id":"CITY_MEDELLIN", "title":"Medellín","description":("Colombia" if is_es(lang) else "Colombia")},
        {"id":"CITY_TULUM",    "title":"Tulum","description":("México" if is_es(lang) else "Mexico")},
        {"id":"CITY_CDMX",     "title":"CDMX","description":("México" if is_es(lang) else "Mexico")},
    ]
    button = ("Elegir" if is_es(lang) else "Choose")
    return header, body, button, rows

def ask_date(lang):
    return ("¿En qué fecha será tu viaje/estancia? Formato *YYYY-MM-DD*.\n\n"
            "O usa los botones:" if is_es(lang) else
            "When will your trip/stay be? Use *YYYY-MM-DD* format.\n\n"
            "Or use the buttons:")

def date_buttons(lang):
    return [
        {"id":"DATE_TODAY","title":("Hoy" if is_es(lang) else "Today")},
        {"id":"DATE_TOMORROW","title":("Mañana" if is_es(lang) else "Tomorrow")},
        {"id":"DATE_UNKNOWN","title":("Aún no sé" if is_es(lang) else "I don’t know")}
    ]

def ask_pax(lang):
    return ("¿Para cuántas *personas*? Puedes escribir 'somos 6' o elegir un rango:"
            if is_es(lang) else
            "How many *guests*? You may type 'we are 6' or choose a range:")

def pax_buttons(lang):
    return [
        {"id":"PAX_2_4","title":"2–4"},
        {"id":"PAX_5_8","title":"5–8"},
        {"id":"PAX_9_PLUS","title":"9+"},
    ]

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
    if c in ("mexico","méxico","cdmx"):
        return ("Ray", HUBSPOT_OWNER_RAY or None, CAL_RAY, "CDMX")
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

# ==================== Validaciones ====================
def valid_name(fullname: str) -> bool:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return len(tokens) >= 2

def normalize_name(fullname: str) -> str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return " ".join(tokens[:3]).title()

def parse_date_or_button(id_or_text: str, lang: str):
    v = (id_or_text or "").strip()
    low = v.lower()
    today = datetime.date.today()
    if low in ("date_today","hoy","today"):
        return today.isoformat()
    if low in ("date_tomorrow","mañana","tomorrow"):
        return (today + datetime.timedelta(days=1)).isoformat()
    # strict YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        try:
            datetime.date.fromisoformat(v)
            return v
        except:
            return None
    if low in ("date_unknown","aún no sé","aun no se","i don’t know","i don't know","dontknow","unknown"):
        return ""
    return None

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

                # ===== Reinicio manual (hola/start/inicio/menu) =====
                txt_raw = ((m.get("text") or {}).get("body") or "").strip().lower()
                if txt_raw in ("hola","hello","/start","start","inicio","menu"):
                    SESSIONS[user] = {"step":"lang","lang":"ES","attempts_email":0}
                    # 👇 Welcome como mensaje interactivo (NO texto aparte)
                    wa_send_buttons(user, welcome_text(), opener_buttons())
                    continue

                # ===== Primera vez → Welcome con botones idioma =====
                if user not in SESSIONS:
                    SESSIONS[user] = {"step":"lang","lang":"ES","attempts_email":0}
                    wa_send_buttons(user, welcome_text(), opener_buttons())
                    continue

                text, reply_id = extract_text_or_reply(m)
                state = SESSIONS[user]

                # ===== 0) idioma =====
                if state["step"] == "lang":
                    rid = (reply_id or "").upper()
                    low = (text or "").strip().lower()
                    if rid == "LANG_ES" or low in ("es","español","spanish"):
                        state["lang"] = "ES"
                    elif rid == "LANG_EN" or low in ("en","english"):
                        state["lang"] = "EN"
                    else:
                        wa_send_buttons(user, welcome_text(), opener_buttons())
                        continue
                    state["step"] = "contact_name"
                    wa_send_text(user, ask_fullname(state["lang"]))
                    continue

                # ===== 1) Nombre =====
                if state["step"] == "contact_name":
                    if not valid_name(text):
                        wa_send_text(user, ask_fullname(state["lang"]))
                        continue
                    state["name"] = normalize_name(text)
                    state["step"] = "contact_email"
                    wa_send_text(user, ask_email(state["lang"]))
                    continue

                # ===== 2) Email (validación suave) =====
                if state["step"] == "contact_email":
                    if EMAIL_RE.match(text or ""):
                        state["email"] = (text or "").strip()
                        # crea/actualiza contacto
                        state["contact_id"] = hubspot_find_or_create_contact(
                            state.get("name"), state.get("email"), user, state.get("lang")
                        )
                        # menú
                        h,b,btn,rows = main_menu_list(state["lang"])
                        wa_send_list(user, h, b, btn, rows)
                        state["step"] = "menu"
                        continue
                    else:
                        state["attempts_email"] = state.get("attempts_email",0) + 1
                        if state["attempts_email"] >= 2:
                            state["email"] = (text or "").strip()
                            state["contact_id"] = hubspot_find_or_create_contact(
                                state.get("name"), state.get("email"), user, state.get("lang")
                            )
                            h,b,btn,rows = main_menu_list(state["lang"])
                            wa_send_list(user, h, b, btn, rows)
                            state["step"] = "menu"
                            continue
                        wa_send_text(user, polite_email_retry(state["lang"]))
                        continue

                # ===== 3) Menú principal =====
                if state["step"] == "menu":
                    rid = (reply_id or "").upper()
                    if rid in ("SVC_VILLAS","SVC_ISLANDS","SVC_BOATS","SVC_WEDDINGS","SVC_CONCIERGE","SVC_TEAM"):
                        svc = {
                            "SVC_VILLAS":"villas",
                            "SVC_ISLANDS":"villas",  # usa mismo flujo que villas
                            "SVC_BOATS":"boats",
                            "SVC_WEDDINGS":"weddings",
                            "SVC_CONCIERGE":"concierge",
                            "SVC_TEAM":"team",
                        }[rid]
                        state["service_type"] = svc
                        h,b,btn,rows = city_list(state["lang"])
                        if svc in ("villas","boats"):
                            wa_send_list(user, h, b, btn, rows); state["step"] = "ask_city"
                        elif svc == "weddings":
                            wa_send_list(user, h, b, btn, rows); state["step"] = "wed_city"
                        elif svc == "concierge":
                            wa_send_list(user, h, b, btn, rows); state["step"] = "cc_city"
                        elif svc == "team":
                            wa_send_list(user, h, b, btn, rows); state["step"] = "handoff_city"
                        continue
                    # si escribió texto raro, re-muestra el menú
                    h,b,btn,rows = main_menu_list(state["lang"])
                    wa_send_list(user, h, b, btn, rows)
                    continue

                # ===== Villas / Boats: ciudad =====
                if state["step"] == "ask_city":
                    rid = (reply_id or "").upper()
                    city_map = {
                        "CITY_CARTAGENA":"cartagena",
                        "CITY_MEDELLIN":"medellín",
                        "CITY_TULUM":"tulum",
                        "CITY_CDMX":"cdmx",
                    }
                    city = city_map.get(rid)
                    if not city:
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    state["city"] = city
                    state["step"] = "ask_date"
                    wa_send_text(user, ask_date(state["lang"]))
                    wa_send_buttons(user, " ", date_buttons(state["lang"]))  # botones fecha
                    continue

                # ===== Fecha =====
                if state["step"] == "ask_date":
                    date_val = parse_date_or_button((reply_id or text), state["lang"])
                    if date_val is None:
                        wa_send_text(user, ask_date(state["lang"]))
                        wa_send_buttons(user, " ", date_buttons(state["lang"]))
                        continue
                    state["date"] = date_val  # "" si unknown
                    state["step"] = "ask_pax"
                    wa_send_text(user, ask_pax(state["lang"]))
                    wa_send_buttons(user, " ", pax_buttons(state["lang"]))
                    continue

                # ===== Pax =====
                if state["step"] == "ask_pax":
                    pax = 0
                    rid = (reply_id or "").upper()
                    if rid == "PAX_2_4": pax = 4
                    elif rid == "PAX_5_8": pax = 8
                    elif rid == "PAX_9_PLUS": pax = 12
                    else:
                        pax = extract_pax(text)
                    if pax <= 0: pax = 2
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
                        state["step"] = "post_results"
                        wa_send_buttons(user, ("¿Quieres hacer algo más?" if is_es(state["lang"]) else "Anything else?"),
                                        after_results_buttons(state["lang"]))
                        continue

                    top = find_top_relaxed(service=svc, city=state.get("city"), pax=pax, prefs="", top_k=TOP_K)
                    wa_send_text(user, reply_topN(state["lang"], top, unit=unit))
                    state["last_top"] = top
                    state["step"] = "post_results"
                    wa_send_buttons(user, ("¿Cómo seguimos?" if is_es(state["lang"]) else "How shall we proceed?"),
                                    after_results_buttons(state["lang"]))
                    continue

                # ===== Weddings =====
                if state["step"] == "wed_city":
                    rid = (reply_id or "").upper()
                    city_map = {
                        "CITY_CARTAGENA":"cartagena",
                        "CITY_MEDELLIN":"medellín",
                        "CITY_TULUM":"tulum",
                        "CITY_CDMX":"cdmx",
                    }
                    city = city_map.get(rid)
                    if not city:
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    state["city"] = city
                    msg = ("Cuéntame *fecha aproximada* y *número de invitados* (ej. 2025-04, 80). Si no lo sabes aún, escribe 'TBD'."
                           if is_es(state["lang"]) else
                           "Tell me *approx date* and *guest count* (e.g., 2025-04, 80). If unknown, type 'TBD'.")
                    wa_send_text(user, msg)
                    state["step"] = "wed_info"
                    continue

                if state["step"] == "wed_info":
                    state["wed_info"] = (text or "")
                    owner_name, owner_id, cal_url, pretty_city = owner_for_city(state.get("city"))
                    wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city))
                    contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                    title = f"[{pretty_city}] Weddings & Events via WhatsApp"
                    desc  = f"City: {pretty_city}\nInfo: {state.get('wed_info')}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                    if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                    state["step"] = "post_results"
                    wa_send_buttons(user, ("¿Quieres algo más?" if is_es(state["lang"]) else "Anything else?"),
                                    after_results_buttons(state["lang"]))
                    continue

                # ===== Concierge =====
                if state["step"] == "cc_city":
                    rid = (reply_id or "").upper()
                    city_map = {
                        "CITY_CARTAGENA":"cartagena",
                        "CITY_MEDELLIN":"medellín",
                        "CITY_TULUM":"tulum",
                        "CITY_CDMX":"cdmx",
                    }
                    city = city_map.get(rid)
                    if not city:
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    state["city"] = city
                    msg = ("¿Qué necesitas? (reservas, transporte, chef, seguridad, experiencias privadas). Responde libremente."
                           if is_es(state["lang"]) else
                           "What do you need? (reservations, transport, private chef, security, private experiences). Reply freely.")
                    wa_send_text(user, msg)
                    state["step"] = "cc_info"
                    continue

                if state["step"] == "cc_info":
                    state["cc_info"] = (text or "")
                    owner_name, owner_id, cal_url, pretty_city = owner_for_city(state.get("city"))
                    wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city))
                    contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                    title = f"[{pretty_city}] Concierge via WhatsApp"
                    desc  = f"City: {pretty_city}\nRequest: {state.get('cc_info')}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                    if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                    state["step"] = "post_results"
                    wa_send_buttons(user, ("¿Algo más?" if is_es(state["lang"]) else "Anything else?"),
                                    after_results_buttons(state["lang"]))
                    continue

                # ===== Handoff directo (TEAM) =====
                if state["step"] == "handoff_city":
                    rid = (reply_id or "").upper()
                    city_map = {
                        "CITY_CARTAGENA":"cartagena",
                        "CITY_MEDELLIN":"medellín",
                        "CITY_TULUM":"tulum",
                        "CITY_CDMX":"cdmx",
                    }
                    city = city_map.get(rid)
                    if not city:
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    state["city"] = city
                    owner_name, owner_id, cal_url, pretty_city = owner_for_city(city)
                    wa_send_text(user, handoff_text(state["lang"], owner_name, cal_url, pretty_city))
                    contact_id = state.get("contact_id") or hubspot_find_or_create_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                    title = f"[{pretty_city}] Talk to the Team via WhatsApp"
                    desc  = f"City: {pretty_city}\nService: {state.get('service_type') or 'N/A'}\nDate: {state.get('date') or 'TBD'}\nPax: {state.get('pax') or 'TBD'}\nLang: {state.get('lang')}\nSource: WhatsApp Bot"
                    if state.get("last_top"):
                        tops = "; ".join([f"{r.get('name')}→{r.get('url')}" for r in state["last_top"][:TOP_K]])
                        desc += f"\nTop shown: {tops}"
                    if contact_id: hubspot_create_deal(contact_id, owner_id, title, desc)
                    state["step"] = "post_results"
                    wa_send_buttons(user, ("¿Qué más necesitas?" if is_es(state["lang"]) else "What else do you need?"),
                                    after_results_buttons(state["lang"]))
                    continue

                # ===== Post-results =====
                if state["step"] == "post_results":
                    rid = (reply_id or "").upper()
                    if rid == "POST_ADD_SERVICE":
                        state["step"] = "menu"
                        h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    if rid == "POST_TALK_TEAM":
                        state["step"] = "handoff_city"
                        h,b,btn,rows = city_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    if rid == "POST_MENU":
                        state["step"] = "menu"
                        h,b,btn,rows = main_menu_list(state["lang"]); wa_send_list(user, h, b, btn, rows); continue
                    wa_send_buttons(user, ("¿Quieres añadir otro servicio o hablar con el equipo?"
                                           if is_es(state["lang"]) else
                                           "Would you like to add another service or talk to the team?"),
                                    after_results_buttons(state["lang"]))
                    continue

    return {"ok": True}
