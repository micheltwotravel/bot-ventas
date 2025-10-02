# main.py
import os, re, csv, io, requests, datetime
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

# =============== ENV =================
VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN") or "").strip()
WA_TOKEN     = (os.getenv("WA_ACCESS_TOKEN") or "").strip()
WA_PHONE_ID  = (os.getenv("WA_PHONE_NUMBER_ID") or "").strip()

TOP_K = int(os.getenv("TOP_K", "3"))

HUBSPOT_TOKEN        = (os.getenv("HUBSPOT_TOKEN") or "").strip()
HUBSPOT_PIPELINE_ID  = (os.getenv("HUBSPOT_PIPELINE_ID") or "default").strip()
HUBSPOT_DEALSTAGE_ID = (os.getenv("HUBSPOT_DEALSTAGE_ID") or "appointmentscheduled").strip()
GOOGLE_SHEET_CSV_URL = (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()

# Owners por ciudad
CITY_OWNERS = {
    "cartagena": "sofia@two.travel",
    "tulum":     "sofia@two.travel",
    "medellin":  "ross@two.travel",
    "méxico":    "ray@two.travel",
    "mexico":    "ray@two.travel",
    "cdmx":      "ray@two.travel",
}
RAY_EMAIL = "ray@two.travel"

ALLOWED_CITIES = ["Cartagena", "Medellín", "Tulum", "CDMX", "México"]

# =============== STATE ===============
SESSIONS = {}  # { phone: {..., "attempts":{}, "options":{"type":..., "items":[...]}} }

# =============== WhatsApp ===============
BASE_URL = "https://graph.facebook.com/v23.0"

def _post_wa(path, payload):
    url = f"{BASE_URL}/{WA_PHONE_ID}/{path}"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print("WA send:", r.status_code, r.text[:200])
    return r

def wa_send_text(to, body):
    _post_wa("messages", {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}})

def wa_send_buttons(to, body, buttons):
    # buttons: [("ID","Title"),... max 3]
    btns = [{"type":"reply","reply":{"id":bid,"title":title[:20]}} for bid,title in buttons[:3]]
    payload = {
        "messaging_product":"whatsapp","to":to,"type":"interactive",
        "interactive":{"type":"button","body":{"text":body},"action":{"buttons":btns}}
    }
    _post_wa("messages", payload)

def wa_send_list(to, body, section_title, rows):
    # rows: [("id","Title","desc"), ...]
    rows_payload = [{"id":rid,"title":title[:24],"description":(desc or "")[:60]} for rid,title,desc in rows]
    payload = {
      "messaging_product":"whatsapp","to":to,"type":"interactive",
      "interactive":{"type":"list","body":{"text":body},"action":{"button":"Choose","sections":[{"title":section_title,"rows":rows_payload}]}}
    }
    _post_wa("messages", payload)

def extract_text(m: dict) -> str:
    t = (m.get("type") or "").lower()
    if t == "text": return ((m.get("text") or {}).get("body") or "").strip()
    if t == "button": return ((m.get("button") or {}).get("text") or "").strip()
    if t == "interactive":
        inter = m.get("interactive") or {}
        if inter.get("type") == "button_reply":
            return ((inter.get("button_reply") or {}).get("title") or "").strip()
        if inter.get("type") == "list_reply":
            return ((inter.get("list_reply") or {}).get("title") or "").strip()
    return ""

def save_options(state, opt_type, items_titles):
    state["options"] = {"type": opt_type, "items": items_titles}

def resolve_numeric_choice(text, state):
    if not state.get("options"): return None
    m = re.fullmatch(r"\s*([1-9])\s*", (text or ""))
    if not m: return None
    idx = int(m.group(1)) - 1
    items = state["options"].get("items") or []
    return items[idx] if 0 <= idx < len(items) else None

# =============== HubSpot ===============
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def hubspot_get_owner_id_by_email(email: str):
    try:
        r = requests.get("https://api.hubapi.com/crm/v3/owners",
                         headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}"}, timeout=20)
        if not r.ok: 
            print("owners err:", r.status_code, r.text[:200]); 
            return None
        for owner in r.json().get("results", []):
            if (owner.get("email") or "").lower() == (email or "").lower():
                return owner.get("id")
    except Exception as e:
        print("owners ex:", e)
    return None

def hubspot_upsert_contact(name, email, phone, lang):
    if not HUBSPOT_TOKEN: return None
    base = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}","Content-Type":"application/json"}
    props = {
        "email": email or None,
        "firstname": (name.split()[0] if name else None),
        "lastname": (" ".join(name.split()[1:]) if name and len(name.split())>1 else None),
        "phone": phone,
        "lifecyclestage":"lead",
        "preferred_language": ("es" if (lang or "ES").upper().startswith("ES") else "en"),
        "source":"WhatsApp Bot",
    }
    r = requests.post(base, headers=headers, json={"properties":props}, timeout=20)
    if r.status_code == 201:
        return r.json().get("id")
    if r.status_code == 409 and email:
        s = requests.post(f"{base}/search", headers=headers, json={
            "filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}],
            "properties":["email"]}, timeout=20)
        if s.ok and s.json().get("results"):
            cid = s.json()["results"][0]["id"]
            requests.patch(f"{base}/{cid}", headers=headers, json={"properties":props}, timeout=20)
            return cid
    print("contact err:", r.status_code, r.text[:200]); 
    return None

def hubspot_create_deal(state, contact_id):
    if not HUBSPOT_TOKEN: return None
    svc  = (state.get("service_type") or "villas")
    city = (state.get("city") or "Cartagena")
    pax  = int(state.get("pax") or 0)
    date = (state.get("trip_date_iso") or "")

    dealname = f"{svc.title()} - {city} - {pax or 'N/A'} pax"
    owner_email = state.get("force_owner_email") or CITY_OWNERS.get(city.lower()) or CITY_OWNERS.get(city.lower().replace("é","e"))
    owner_id = hubspot_get_owner_id_by_email(owner_email) if owner_email else None

    props = {
        "dealname": dealname,
        "pipeline": HUBSPOT_PIPELINE_ID,
        "dealstage": HUBSPOT_DEALSTAGE_ID,
        "hs_lead_status": "NEW",
        "source": "WhatsApp Bot",
        "city": city,
        "service_type": svc,
        "trip_date": date or None,
        "party_size": str(pax) if pax else None,
    }
    if owner_id: props["hubspot_owner_id"] = owner_id

    r = requests.post("https://api.hubapi.com/crm/v3/objects/deals",
                      headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}","Content-Type":"application/json"},
                      json={"properties":props}, timeout=20)
    if not r.ok:
        print("deal err:", r.status_code, r.text[:200]); 
        return None
    deal_id = r.json().get("id")
    if contact_id:
        try:
            requests.put(f"https://api.hubapi.com/crm/v4/objects/deals/{deal_id}/associations/contacts/{contact_id}/deal_to_contact",
                         headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}"}, timeout=20)
        except Exception as e:
            print("assoc err:", e)
    return deal_id

# =============== Catálogo ===============
def load_catalog():
    if not GOOGLE_SHEET_CSV_URL:
        print("WARN: no catalog URL"); 
        return []
    r = requests.get(GOOGLE_SHEET_CSV_URL, timeout=30)
    if not r.ok:
        print("catalog err:", r.status_code, r.text[:200]); 
        return []
    rows = []
    reader = csv.DictReader(io.StringIO(r.content.decode("utf-8", errors="ignore")))
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k,v in row.items()})
    return rows

def find_top(service, city, pax, prefs, top_k=TOP_K):
    service = (service or "").strip().lower()
    city    = (city or "").strip().lower()
    prefs_l = [p.strip().lower() for p in (prefs or "").split(",") if p.strip()]
    rows = load_catalog()
    if not rows: return []
    def ok(r):
        if (r.get("service_type","").lower() != service): return False
        if city and (r.get("city","").lower() != city): return False
        try: cap = int(float(r.get("capacity_max","0") or "0"))
        except: cap = 0
        if pax and cap < pax: return False
        if prefs_l:
            tags = [t.strip().lower() for t in (r.get("preference_tags","") or "").split(",") if t.strip()]
            if not any(p in tags for p in prefs_l): return False
        return True
    cands = [r for r in rows if ok(r)]
    def price(r):
        try: return float(r.get("price_from_usd","999999") or "999999")
        except: return 999999.0
    cands.sort(key=price)
    return cands[:max(1, int(top_k or 1))]

# =============== Copy ===================
def is_es(lang): return (lang or "ES").upper().startswith("ES")

def opener_text():
    return "Welcome to Two Travel ✨\nChoose your language:"

def contact_name_msg(lang):
    return ("Para cotizar, escribe tu *nombre y apellido*."
            if is_es(lang) else
            "To get your quote, please type your *first & last name*.")

def contact_email_msg(lang):
    return ("📧 *Correo electrónico* (o escribe *saltar*)"
            if is_es(lang) else
            "📧 *Email address* (or type *skip*)")

def menu_title(lang): 
    return ("¿Qué necesitas hoy?" if is_es(lang) else "What do you need today?")

def city_title(lang):
    return ("Elige *ciudad*:" if is_es(lang) else "Choose *city*:")

def ask_date(lang):
    return ("¿Cuándo es el viaje? (ej. 2025-11-12 / 12-11-2025 / hoy / mañana)\nSi no seguro, escribe *no sé*."
            if is_es(lang) else
            "When is the trip? (e.g. 2025-11-12 / 11-12-2025 / today / tomorrow)\nIf not sure, type *don’t know*.")

def ask_pax(lang):
    return ("¿Para cuántas *personas*? (número)"
            if is_es(lang) else
            "How many *guests*? (number)")

def reply_topN(lang, items, unit):
    if not items:
        return ("No veo opciones con esos filtros. Probemos con otro tamaño de grupo."
                if is_es(lang) else "No matches. Try a different party size.")
    lines = []
    if is_es(lang):
        lines.append(f"Top {len(items)} opciones (precios *desde*):")
        for r in items:
            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} pax) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("La disponibilidad final la confirma *ventas*. ¿Conecto con *Ray*?")
    else:
        lines.append(f"Top {len(items)} options (*prices from*):")
        for r in items:
            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} guests) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("Final availability is confirmed by *Sales*. Connect with *Ray*?")
    return "\n".join(lines)

def add_more_or_ray(lang):
    return ("¿Deseas *añadir otro servicio* o *hablar con Ray*?"
            if is_es(lang) else
            "Would you like to *add another service* or *talk to Ray*?")

# =============== Parsing suave ==========
def normalize_name(fullname):
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    if len(tokens) < 2: return ""
    return " ".join(tokens[:3]).title()

def parse_date_soft(text, lang):
    if not text: return None
    t = (text or "").strip().lower()
    today = datetime.date.today()
    if t in ("no se","no sé","dont know","don't know","no estoy seguro","not sure","skip","saltar"):
        return None
    if t in ("hoy","today"): return today.isoformat()
    if t in ("mañana","manana","tomorrow"): return (today + datetime.timedelta(days=1)).isoformat()
    t = re.sub(r"[.]", "-", t)
    m = re.match(r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*$", t)
    if m:
        y,mm,dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try: return datetime.date(y,mm,dd).isoformat()
        except: return None
    m = re.match(r"^\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s*$", t)
    if m:
        a,b,y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12 or is_es(lang): d,mm = a,b
        else: mm,d = a,b
        try: return datetime.date(y,mm,d).isoformat()
        except: return None
    return None

def parse_int_soft(text, default):
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else default

def inc_attempt(state, step):
    state.setdefault("attempts", {}).setdefault(step, 0)
    state["attempts"][step] += 1
    return state["attempts"][step]

# =============== FastAPI ===============
@app.on_event("startup")
async def startup():
    print("BOOT> WA_PHONE_ID:", repr(WA_PHONE_ID))
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

@app.get("/")
def root():
    return {"ok": True}

# Verify webhook
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
            if v.get("statuses"):
                continue

            for m in v.get("messages", []):
                user = m.get("from")
                if not user: 
                    continue

                # Primera vez → welcome + idioma
                if user not in SESSIONS:
                    SESSIONS[user] = {"step":"lang","lang":"ES"}
                    wa_send_buttons(user, opener_text(), [("ES","ES 🇪🇸"),("EN","EN 🇺🇸")])
                    save_options(SESSIONS[user], "lang", ["ES","EN"])
                    continue

                text = extract_text(m)
                state = SESSIONS[user]
                choice = resolve_numeric_choice(text, state)

                # ===== idioma
                if state["step"] == "lang":
                    sel = (choice or text or "").strip().lower()
                    if sel.startswith("es"): state["lang"]="ES"
                    elif sel.startswith("en"): state["lang"]="EN"
                    else:
                        if inc_attempt(state,"lang")==1:
                            wa_send_buttons(user, opener_text(), [("ES","ES 🇪🇸"),("EN","EN 🇺🇸")])
                            save_options(state,"lang",["ES","EN"])
                            continue
                        state["lang"]="EN"  # default
                    state["step"]="contact_name"
                    wa_send_text(user, contact_name_msg(state["lang"]))
                    continue

                # ===== nombre (escrito, obligatorio 2 palabras)
                if state["step"] == "contact_name":
                    name = normalize_name(text)
                    if not name:
                        if inc_attempt(state,"contact_name")==1:
                            wa_send_text(user, contact_name_msg(state["lang"]))
                            continue
                        name = "Two Travel Guest"
                    state["name"]=name
                    state["step"]="contact_email"
                    wa_send_text(user, contact_email_msg(state["lang"]))
                    continue

                # ===== email (opcional; skip)
                if state["step"] == "contact_email":
                    email = None
                    if text and EMAIL_RE.match(text): email = text.strip()
                    if text and text.lower() in ("skip","saltar","omitir","no"):
                        email = None
                    state["email"]=email
                    try:
                        state["hubspot_contact_id"] = hubspot_upsert_contact(state.get("name"), email, user, state.get("lang"))
                    except Exception as e:
                        print("contact ex:", e)
                    # Menú por LISTA (numerado)
                    state["step"]="menu"
                    rows = [
                        ("M_VILLAS",   ("1. Villas & Homes 🏠"  if not is_es(state["lang"]) else "1. Villas & Casas 🏠"),   ""),
                        ("M_BOATS",    ("2. Boats & Yachts 🚤"  if not is_es(state["lang"]) else "2. Botes & Yates 🚤"),   ""),
                        ("M_WEDDINGS", ("3. Weddings & Events 💍" if not is_es(state["lang"]) else "3. Bodas & Eventos 💍"), ""),
                        ("M_CG",       ("4. Concierge ✨"        if not is_es(state["lang"]) else "4. Concierge ✨"),       ""),
                        ("M_RAY",      ("5. Talk to Ray 👤"      if not is_es(state["lang"]) else "5. Hablar con Ray 👤"), ""),
                    ]
                    wa_send_list(user, menu_title(state["lang"]), "Menu", rows)
                    wa_send_text(user, ("También puedes responder con *1–5*." if is_es(state["lang"]) else "You can also reply with *1–5*."))
                    save_options(state,"menu",[r[1] for r in rows])  # títulos
                    continue

                # ===== menú
                if state["step"] == "menu":
                    t = (choice or text or "").lower()
                    state["force_owner_email"] = None
                    if "ray" in t:
                        state["force_owner_email"] = RAY_EMAIL
                        state["step"]="handoff"
                        wa_send_text(user, ("Te conecto con *Ray – Two Travel* para confirmar disponibilidad y cerrar la reserva."
                                            if is_es(state["lang"]) else
                                            "Connecting you with *Ray – Two Travel* to confirm availability and finalize the booking."))
                        # crea deal simple asignado a Ray
                        try: state["hubspot_deal_id"] = hubspot_create_deal(state, state.get("hubspot_contact_id"))
                        except Exception as e: print("deal ex:", e)
                        continue
                    if "villa" in t or "casa" in t:
                        state["service_type"]="villas"
                    elif "boat" in t or "yate" in t or "bote" in t:
                        state["service_type"]="boats"
                    elif "wedding" in t or "boda" in t or "evento" in t:
                        state["service_type"]="weddings"
                    elif "concierge" in t:
                        state["service_type"]="concierge"
                    else:
                        if inc_attempt(state,"menu")==1:
                            rows = [
                                ("M_VILLAS",   ("1. Villas & Homes 🏠" if not is_es(state["lang"]) else "1. Villas & Casas 🏠"), ""),
                                ("M_BOATS",    ("2. Boats & Yachts 🚤" if not is_es(state["lang"]) else "2. Botes & Yates 🚤"), ""),
                                ("M_WEDDINGS", ("3. Weddings & Events 💍" if not is_es(state["lang"]) else "3. Bodas & Eventos 💍"), ""),
                                ("M_CG",       ("4. Concierge ✨"       if not is_es(state["lang"]) else "4. Concierge ✨"), ""),
                                ("M_RAY",      ("5. Talk to Ray 👤"     if not is_es(state["lang"]) else "5. Hablar con Ray 👤"), ""),
                            ]
                            wa_send_list(user, menu_title(state["lang"]), "Menu", rows)
                            wa_send_text(user, ("Responde con *1–5*." if is_es(state["lang"]) else "Reply with *1–5*."))
                            save_options(state,"menu",[r[1] for r in rows]); 
                            continue
                        state["service_type"]="villas"
                    # Ciudad
                    state["step"]="city"
                    rows = [(f"CT_{ct.lower()}", f"{i}. {ct}", "") for i,ct in enumerate(ALLOWED_CITIES, start=1)]
                    wa_send_list(user, city_title(state["lang"]), "Cities", rows)
                    wa_send_text(user, ("También puedes responder con *1–5*." if is_es(state["lang"]) else "You can also reply with *1–5*."))
                    save_options(state,"city",ALLOWED_CITIES)
                    continue

                # ===== ciudad
                if state["step"] == "city":
                    chosen = (choice or text or "").strip()
                    # match por número o por texto
                    if choice:
                        city = chosen.split(". ",1)[-1] if ". " in chosen else chosen
                    else:
                        low = chosen.lower().replace("méxico", "mexico")
                        city = None
                        for ct in ALLOWED_CITIES:
                            if low == ct.lower() or ct.lower().startswith(low):
                                city = ct; break
                    if not city:
                        if inc_attempt(state,"city")==1:
                            rows = [(f"CT_{ct.lower()}", f"{i}. {ct}", "") for i,ct in enumerate(ALLOWED_CITIES, start=1)]
                            wa_send_list(user, city_title(state["lang"]), "Cities", rows)
                            save_options(state,"city",ALLOWED_CITIES)
                            continue
                        city = "Cartagena"
                    state["city"]=city
                    state["step"]="date"
                    wa_send_text(user, ask_date(state["lang"]))
                    continue

                # ===== fecha (suave)
                if state["step"] == "date":
                    iso = parse_date_soft(text, state.get("lang"))
                    if not iso and inc_attempt(state,"date")==1:
                        wa_send_text(user, ask_date(state["lang"]))
                        continue
                    state["trip_date_iso"]=iso  # puede ser None
                    state["step"]="pax"
                    wa_send_text(user, ask_pax(state["lang"]))
                    continue

                # ===== pax
                if state["step"] == "pax":
                    state["pax"] = parse_int_soft(text, 2)
                    # Top opciones (si hay catálogo)
                    if GOOGLE_SHEET_CSV_URL:
                        unit = "noche" if is_es(state["lang"]) else "night"
                        svc = "villas" if state.get("service_type")!="boats" else "boats"
                        top = find_top(svc, (state.get("city") or "").lower(), int(state.get("pax") or 0), "", TOP_K)
                        wa_send_text(user, reply_topN(state["lang"], top, unit))
                    else:
                        wa_send_text(user, ("Te muestro opciones en seguida con nuestro equipo de ventas." if is_es(state["lang"]) else "Our sales team will share options shortly."))
                    # Deal (owner por ciudad)
                    try:
                        state["hubspot_deal_id"] = hubspot_create_deal(state, state.get("hubspot_contact_id"))
                    except Exception as e:
                        print("deal ex:", e)
                    state["step"]="post"
                    wa_send_buttons(user, add_more_or_ray(state["lang"]),
                                    [("ADD","Add / Añadir"),("RAY","Ray")])
                    save_options(state,"post",["Add","Ray"])
                    continue

                # ===== post
                if state["step"] == "post":
                    t = (choice or text or "").lower()
                    if "add" in t or "añad" in t or "otro" in t:
                        state["step"]="menu"
                        rows = [
                            ("M_VILLAS",   ("1. Villas & Homes 🏠" if not is_es(state["lang"]) else "1. Villas & Casas 🏠"), ""),
                            ("M_BOATS",    ("2. Boats & Yachts 🚤" if not is_es(state["lang"]) else "2. Botes & Yates 🚤"), ""),
                            ("M_WEDDINGS", ("3. Weddings & Events 💍" if not is_es(state["lang"]) else "3. Bodas & Eventos 💍"), ""),
                            ("M_CG",       ("4. Concierge ✨"       if not is_es(state["lang"]) else "4. Concierge ✨"), ""),
                            ("M_RAY",      ("5. Talk to Ray 👤"     if not is_es(state["lang"]) else "5. Hablar con Ray 👤"), ""),
                        ]
                        wa_send_list(user, menu_title(state["lang"]), "Menu", rows)
                        save_options(state,"menu",[r[1] for r in rows])
                        continue
                    # Talk to Ray
                    state["force_owner_email"]=RAY_EMAIL
                    try:
                        state["hubspot_deal_id"] = hubspot_create_deal(state, state.get("hubspot_contact_id"))
                    except Exception as e: print("deal ex:", e)
                    wa_send_text(user, ("Te conecto con *Ray – Two Travel* en este mismo chat." 
                                        if is_es(state["lang"]) else 
                                        "Connecting you with *Ray – Two Travel* in this chat."))
                    state["step"]="handoff"
                    continue

    return {"ok": True}
