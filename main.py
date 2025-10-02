# main.py
import os, re, csv, io, requests
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

# ====== ENV (sanitize) ======
VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN") or "").strip()
WA_TOKEN     = (os.getenv("WA_ACCESS_TOKEN") or "").strip()
WA_PHONE_ID  = (os.getenv("WA_PHONE_NUMBER_ID") or "").strip()

# ====== Config ======
TOP_K = int(os.getenv("TOP_K", "3"))  # 2 o 3
HUBSPOT_TOKEN        = (os.getenv("HUBSPOT_TOKEN") or "").strip()
GOOGLE_SHEET_CSV_URL = (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()

# ====== Estado en memoria ======
SESSIONS = {}  # { phone: {...} }

# ====== WhatsApp helpers ======
def _wa_post(json_payload):
    url = f"https://graph.facebook.com/v23.0/{(WA_PHONE_ID or '').strip()}/messages"
    headers = {
        "Authorization": f"Bearer {(WA_TOKEN or '').strip()}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json=json_payload, timeout=20)
    print("WA ->", r.status_code, r.text[:200])
    if r.status_code == 401:
        print("⚠️ WA TOKEN INVALID/EXPIRED. Actualiza WA_ACCESS_TOKEN.")
    if r.status_code == 400:
        print("⚠️ BAD REQUEST. Revisa WA_PHONE_ID / payload.")
    return r.status_code

def wa_send_text(to: str, body: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    return _wa_post(payload)

def wa_send_buttons(to: str, body: str, buttons: list):
    """
    buttons: [{"id":"btn_id","title":"1️⃣ Texto"}, ...]  # máx 3
    """
    if len(buttons) > 3:
        buttons = buttons[:3]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [{"type":"reply","reply":{"id":b["id"],"title":b["title"]}} for b in buttons]
            }
        }
    }
    return _wa_post(payload)

def wa_send_list(to: str, header_text: str, body_text: str, footer_text: str, rows: list, section_title: str="Opciones"):
    """
    rows: [{"id":"row_id","title":"1️⃣ Opción","description":"texto opcional"}, ...]
    Puede tener muchas filas.
    """
    payload = {
        "messaging_product":"whatsapp",
        "to": to,
        "type":"interactive",
        "interactive":{
            "type":"list",
            "header":{"type":"text","text": header_text[:60] if header_text else "Two Travel"},
            "body":{"text": body_text},
            "footer":{"text": footer_text[:60] if footer_text else ""},
            "action":{
                "button":"Elegir",
                "sections":[{"title": section_title, "rows":[
                    {"id": r["id"], "title": r["title"], **({"description": r["description"]} if r.get("description") else {})}
                    for r in rows
                ]}]
            }
        }
    }
    return _wa_post(payload)

def extract_selection(m: dict):
    """
    Devuelve (text, selection_id) soportando text/button/list.
    """
    t = (m.get("type") or "").lower()
    if t == "text":
        return ((m.get("text") or {}).get("body") or "").strip(), None
    if t == "button":
        btn = (m.get("button") or {})
        return (btn.get("text") or "").strip(), (btn.get("payload") or "").strip() or None
    if t == "interactive":
        inter = m.get("interactive") or {}
        i_type = inter.get("type")
        if i_type == "button_reply":
            br = inter.get("button_reply") or {}
            return (br.get("title") or "").strip(), (br.get("id") or "").strip() or None
        if i_type == "list_reply":
            lr = inter.get("list_reply") or {}
            return (lr.get("title") or "").strip(), (lr.get("id") or "").strip() or None
    return "", None

# ====== HubSpot ======
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def hubspot_upsert_contact(name: str, email: str, phone: str, lang: str):
    if not HUBSPOT_TOKEN:
        print("WARN: HUBSPOT_TOKEN missing")
        return False
    base = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}","Content-Type":"application/json"}
    props = {
        "email": email,
        "firstname": (name.split()[0] if name else None),
        "lastname": (" ".join(name.split()[1:]) if name and len(name.split())>1 else None),
        "phone": phone,
        "hs_lead_status": "NEW",
        "lifecyclestage": "lead",
        "preferred_language": ("es" if (lang or "ES").upper().startswith("ES") else "en"),
        "source": "WhatsApp Bot",
    }
    # create
    r = requests.post(base, headers=headers, json={"properties": props}, timeout=20)
    if r.status_code == 201: return True
    # conflict → update
    if r.status_code == 409:
        s = requests.post(f"{base}/search", headers=headers, json={
            "filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}],
            "properties":["email"]
        }, timeout=20)
        if s.ok and (s.json().get("results") or []):
            cid = s.json()["results"][0]["id"]
            up = requests.patch(f"{base}/{cid}", headers=headers, json={"properties": props}, timeout=20)
            return up.ok
    print("HubSpot upsert error:", r.status_code, r.text[:200])
    return False

# ====== Catálogo (Google Sheet CSV) ======
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
    if not rows: return []
    def row_ok(r):
        if (r.get("service_type","").lower() != service): return False
        if city and (r.get("city","").lower() != city):    return False
        try: cap = int(float(r.get("capacity_max","0") or "0"))
        except: cap = 0
        if pax and cap < pax: return False
        if prefs_l:
            tags = [t.strip().lower() for t in (r.get("preference_tags","") or "").split(",") if t.strip()]
            if not any(p in tags for p in prefs_l): return False
        return True
    filtered = [r for r in rows if row_ok(r)]
    def price_val(r):
        try: return float(r.get("price_from_usd","999999") or "999999")
        except: return 999999.0
    filtered.sort(key=price_val)
    return filtered[:max(1, int(top_k or 1))]

# ====== Copy / tono ======
def is_es(lang: str) -> bool:
    return (lang or "ES").upper().startswith("ES")

def brand_header(lang: str):
    return "Two Travel · Concierge" if is_es(lang) else "Two Travel · Concierge"

def opener_bi():
    return (
        "Two Travel 🛎️✨\n\n"
        "ES: ¡Bienvenido! Soy tu concierge virtual de lujo. ¿En qué idioma prefieres continuar?\n"
        "EN: Welcome! I’m your luxury virtual concierge. Which language would you prefer?"
    )

def ask_contact(lang: str):
    return ("Para enviarte opciones y una cotización personalizada, necesito tus datos.\n"
            "1️⃣ Nombre completo\n2️⃣ Luego tu correo") if is_es(lang) \
        else ("To share options and a personalized quote, I’ll need your details.\n"
              "1️⃣ Full name\n2️⃣ Then your email")

def ask_email(lang: str):
    return "📧 Tu correo electrónico:" if is_es(lang) else "📧 Your email address:"

def ask_name_again(lang: str):
    return "¿Me confirmas tu *nombre y apellido*?" if is_es(lang) else "Could you share *name and last name*?"

def ask_email_again(lang: str):
    return "Ese correo no parece válido, ¿puedes revisarlo?" if is_es(lang) else "That email looks invalid, mind checking it?"

def reply_topN(lang: str, items: list, unit: str):
    if not items:
        return ("No veo opciones con esos filtros. ¿Busco *fechas cercanas (±3 días)* o ajusto *personas*?"
                if is_es(lang) else
                "No matches found. Try *nearby dates (±3 days)* or adjust the *party size*?")
    es = is_es(lang)
    lines = []
    if es:
        lines.append("Estas son nuestras mejores opciones (precios *desde*):")
        for r in items:
            lines.append(f"• {r.get('name')} · {r.get('capacity_max','?')} pax — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("La *disponibilidad final* la confirma nuestro equipo de *Ventas* antes de reservar.")
    else:
        lines.append("Here are top options (*prices from*):")
        for r in items:
            lines.append(f"• {r.get('name')} · {r.get('capacity_max','?')} guests — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
        lines.append("Final *availability* is confirmed by our *Sales* team before booking.")
    return "\n".join(lines)

def add_another_or_sales(lang: str):
    return ("¿Quieres *añadir otro servicio* o *conectar con Ventas*?" if is_es(lang)
            else "Would you like to *add another service* or *connect with Sales*?")

def handoff_client(lang: str, owner_name: str, team: str):
    return (f"Te conecto con [{owner_name} – Ventas {team}] para confirmar *disponibilidad* y cerrar la *reserva*."
            if is_es(lang) else
            f"Connecting you with [{owner_name} – {team} Sales] to confirm *availability* and finalize your *booking*.")

# ====== Validaciones nombre ======
def valid_name(fullname: str) -> bool:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return len(tokens) >= 2

def normalize_name(fullname: str) -> str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return " ".join(tokens[:3]).title()

# ====== Startup ======
@app.on_event("startup")
async def show_routes():
    print("BOOT> Routes:", [r.path for r in app.router.routes])
    print("BOOT> WA_PHONE_ID:", repr(WA_PHONE_ID))
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

# ====== Health ======
@app.get("/")
def root():
    return {"ok": True, "routes": [r.path for r in app.router.routes]}

# ====== Verify ======
@app.get("/wa-webhook")
async def verify(req: Request):
    mode = req.query_params.get("hub.mode")
    token = req.query_params.get("hub.verify_token")
    challenge = req.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("forbidden", status_code=403)

# ====== Incoming ======
@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", data)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Ignorar estatus (sent/delivered/read)
            if value.get("statuses"):
                continue

            for m in value.get("messages", []):
                user = m.get("from")
                if not user:
                    continue

                # Primera vez → saludo + selección de idioma (BOTONES)
                if user not in SESSIONS:
                    SESSIONS[user] = {"step": "lang", "lang": "ES"}
                    wa_send_buttons(
                        user,
                        opener_bi(),
                        [
                            {"id":"lang_es","title":"1️⃣ Español"},
                            {"id":"lang_en","title":"2️⃣ English"},
                        ]
                    )
                    continue

                text, sel_id = extract_selection(m)
                state = SESSIONS[user]

                # ===== LENGUAJE =====
                if state["step"] == "lang":
                    if sel_id == "lang_es" or text.lower() in ("es","español","1"):
                        state["lang"] = "ES"
                    elif sel_id == "lang_en" or text.lower() in ("en","english","2"):
                        state["lang"] = "EN"
                    else:
                        wa_send_buttons(
                            user,
                            opener_bi(),
                            [{"id":"lang_es","title":"1️⃣ Español"},{"id":"lang_en","title":"2️⃣ English"}]
                        )
                        continue
                    state["step"] = "contact_name"
                    wa_send_text(user, ask_contact(state["lang"]))
                    continue

                # ===== NOMBRE =====
                if state["step"] == "contact_name":
                    if not valid_name(text):
                        wa_send_text(user, ask_name_again(state["lang"]))
                        continue
                    state["name"] = normalize_name(text)
                    state["step"] = "contact_email"
                    wa_send_text(user, ask_email(state["lang"]))
                    continue

                # ===== EMAIL =====
                if state["step"] == "contact_email":
                    if not EMAIL_RE.match(text or ""):
                        wa_send_text(user, ask_email_again(state["lang"]))
                        continue
                    state["email"] = (text or "").strip()

                    # HubSpot
                    try:
                        hubspot_upsert_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                    except Exception as e:
                        print("HubSpot error:", e)

                    # Menú → usar LIST (6 opciones numeradas)
                    state["step"] = "menu"
                    rows = [
                        {"id":"menu_1_villas",   "title":"1️⃣ Villas & Casas 🏠"},
                        {"id":"menu_2_boats",    "title":"2️⃣ Botes & Yates 🚤"},
                        {"id":"menu_3_islands",  "title":"3️⃣ Islas Privadas 🏝️"},
                        {"id":"menu_4_weddings", "title":"4️⃣ Bodas & Eventos 💍🎉"},
                        {"id":"menu_5_concierge","title":"5️⃣ Concierge ✨"},
                        {"id":"menu_6_sales",    "title":"6️⃣ Hablar con ventas 👤"},
                    ]
                    wa_send_list(
                        user,
                        brand_header(state["lang"]),
                        ("Genial. ¿Qué necesitas hoy?" if is_es(state["lang"]) else "Great. What do you need today?"),
                        ("Two Travel" if is_es(state["lang"]) else "Two Travel"),
                        rows,
                        section_title=("Servicios" if is_es(state["lang"]) else "Services")
                    )
                    continue

                # ===== MENU =====
                if state["step"] == "menu":
                    sid = (sel_id or "").lower()
                    t = (text or "").lower()
                    if sid=="menu_1_villas" or "villas" in t:
                        state["service_type"]="villas"; state["step"]="villas_city"
                        wa_send_list(
                            user, brand_header(state["lang"]),
                            ("¿En qué ciudad buscas?" if is_es(state["lang"]) else "Which city?"),
                            "Two Travel",
                            [
                                {"id":"villas_city_cartagena","title":"1️⃣ Cartagena"},
                                {"id":"villas_city_medellin","title":"2️⃣ Medellín"},
                                {"id":"villas_city_tulum","title":"3️⃣ Tulum"},
                                {"id":"villas_city_cdmx","title":"4️⃣ CDMX"},
                                {"id":"villas_city_other","title":"5️⃣ Otra / Other"},
                            ],
                            section_title=("Ciudades" if is_es(state["lang"]) else "Cities")
                        )
                        continue
                    if sid=="menu_2_boats" or "botes" in t or "boats" in t:
                        state["service_type"]="boats"; state["step"]="boats_city"
                        wa_send_list(
                            user, brand_header(state["lang"]),
                            ("Ciudad/puerto de salida" if is_es(state["lang"]) else "City / port of departure"),
                            "Two Travel",
                            [{"id":"boats_city_cartagena","title":"1️⃣ Cartagena"}],
                            section_title=("Puertos" if is_es(state["lang"]) else "Ports")
                        )
                        continue
                    if sid=="menu_3_islands" or "isla" in t or "island" in t:
                        state["service_type"]="islands"; state["step"]="villas_city"
                        wa_send_list(
                            user, brand_header(state["lang"]),
                            ("¿En qué ciudad buscas?" if is_es(state["lang"]) else "Which city?"),
                            "Two Travel",
                            [
                                {"id":"villas_city_cartagena","title":"1️⃣ Cartagena"},
                                {"id":"villas_city_medellin","title":"2️⃣ Medellín"},
                                {"id":"villas_city_tulum","title":"3️⃣ Tulum"},
                                {"id":"villas_city_cdmx","title":"4️⃣ CDMX"},
                                {"id":"villas_city_other","title":"5️⃣ Otra / Other"},
                            ],
                            section_title=("Ciudades" if is_es(state["lang"]) else "Cities")
                        )
                        continue
                    if sid=="menu_4_weddings" or "boda" in t or "wedding" in t:
                        state["service_type"]="weddings"; state["step"]="weddings_form"
                        wa_send_text(user,
                                     "Ciudad y fecha aproximada / # invitados / Tipo de venue (playa, histórico, finca, moderno) / ¿Full planning?"
                                     if is_es(state["lang"]) else
                                     "City & approx date / guest count / venue type (beach, historic, estate, modern) / Full planning?")
                        continue
                    if sid=="menu_5_concierge" or "concierge" in t:
                        state["service_type"]="concierge"; state["step"]="concierge_form"
                        wa_send_text(user,
                                     "Ciudad / Fechas / Servicios (reservas, transporte, chef, seguridad, experiencias privadas)."
                                     if is_es(state["lang"]) else
                                     "City / Dates / Services (reservations, transport, private chef, security, private experiences).")
                        continue
                    if sid=="menu_6_sales" or "venta" in t or "sales" in t:
                        state["step"]="handoff"
                        owner_name, team = "Two Travel Advisor", "Two Travel"
                        wa_send_text(user, handoff_client(state["lang"], owner_name, team))
                        continue
                    # Si no pulsó nada válido, re-muestro menú
                    rows = [
                        {"id":"menu_1_villas","title":"1️⃣ Villas & Casas 🏠"},
                        {"id":"menu_2_boats","title":"2️⃣ Botes & Yates 🚤"},
                        {"id":"menu_3_islands","title":"3️⃣ Islas Privadas 🏝️"},
                        {"id":"menu_4_weddings","title":"4️⃣ Bodas & Eventos 💍🎉"},
                        {"id":"menu_5_concierge","title":"5️⃣ Concierge ✨"},
                        {"id":"menu_6_sales","title":"6️⃣ Hablar con ventas 👤"},
                    ]
                    wa_send_list(user, brand_header(state["lang"]),
                                 ("Elige una opción" if is_es(state["lang"]) else "Choose an option"),
                                 "Two Travel", rows)
                    continue

                # ===== VILLAS / ISLANDS =====
                if state["step"] == "villas_city":
                    # sel por lista
                    city_map = {
                        "villas_city_cartagena":"cartagena",
                        "villas_city_medellin":"medellin",
                        "villas_city_tulum":"tulum",
                        "villas_city_cdmx":"cdmx",
                    }
                    if sel_id in city_map:
                        state["city"] = city_map[sel_id]
                    else:
                        # Si eligió “Otra” o texto libre
                        state["city"] = (text or "").strip()
                    state["step"] = "villas_dates"
                    wa_send_text(user, "Fechas de check-in y check-out (YYYY-MM-DD):" if is_es(state["lang"])
                                 else "Check-in and check-out dates (YYYY-MM-DD):")
                    continue

                if state["step"] == "villas_dates":
                    state["dates"] = (text or "").strip()
                    state["step"] = "villas_pax"
                    wa_send_text(user, "¿Para cuántas personas?" if is_es(state["lang"]) else "How many guests?")
                    continue

                if state["step"] == "villas_pax":
                    try:
                        state["pax"] = int(re.sub(r"[^\d]", "", text or "") or "0")
                    except:
                        state["pax"] = 0
                    state["step"] = "villas_prefs"
                    # preferencias → LIST
                    rows = [
                        {"id":"villas_pref_ocean","title":"1️⃣ Frente al mar"},
                        {"id":"villas_pref_historic","title":"2️⃣ Centro histórico"},
                        {"id":"villas_pref_excl","title":"3️⃣ Zona exclusiva"},
                        {"id":"villas_pref_any","title":"4️⃣ Cualquiera / No preference"},
                    ]
                    wa_send_list(
                        user, brand_header(state["lang"]),
                        ("¿Alguna preferencia?" if is_es(state["lang"]) else "Any preference?"),
                        "Two Travel", rows, section_title=("Preferencias" if is_es(state["lang"]) else "Preferences")
                    )
                    continue

                if state["step"] == "villas_prefs":
                    pref_map = {
                        "villas_pref_ocean":"oceanfront",
                        "villas_pref_historic":"historic center",
                        "villas_pref_excl":"exclusive area",
                        "villas_pref_any":"any",
                    }
                    if sel_id in pref_map:
                        state["prefs"] = pref_map[sel_id]
                    else:
                        state["prefs"] = (text or "").strip()

                    # No catálogo -> fallback
                    if not GOOGLE_SHEET_CSV_URL:
                        wa_send_text(user, "⚠️ Aún no tengo el catálogo conectado. ¿Te conecto con *Ventas* para cotización?")
                        state["step"] = "post_results"
                        continue

                    svc = "villas" if state.get("service_type") in ("villas","islands","islas","islands") else state.get("service_type")
                    top = find_top(
                        service=svc or "villas",
                        city=(state.get("city") or ""),
                        pax=int(state.get("pax") or 0),
                        prefs=(state.get("prefs") or ""),
                        top_k=TOP_K
                    )
                    unit = "noche" if is_es(state["lang"]) else "night"
                    wa_send_text(user, reply_topN(state["lang"], top, unit))
                    state["step"] = "post_results"
                    continue

                # ===== BOATS =====
                if state["step"] == "boats_city":
                    # Solo Cartagena
                    state["city"] = "cartagena"
                    state["step"] = "boats_date"
                    wa_send_text(user, "¿Fecha del paseo? (YYYY-MM-DD; ¿día o noche?)" if is_es(state["lang"])
                                 else "Trip date? (YYYY-MM-DD; day or night?)")
                    continue

                if state["step"] == "boats_date":
                    state["date"] = (text or "").strip()
                    state["step"] = "boats_pax"
                    wa_send_text(user, "¿Número de pasajeros?" if is_es(state["lang"]) else "Number of passengers?")
                    continue

                if state["step"] == "boats_pax":
                    try:
                        state["pax"] = int(re.sub(r"[^\d]", "", text or "") or "0")
                    except:
                        state["pax"] = 0
                    state["step"] = "boats_type"
                    # Tipo embarcación → LIST
                    rows = [
                        {"id":"boat_type_speed","title":"1️⃣ Lancha / Speedboat"},
                        {"id":"boat_type_yacht","title":"2️⃣ Yate / Yacht"},
                        {"id":"boat_type_cat","title":"3️⃣ Catamarán / Catamaran"},
                    ]
                    wa_send_list(
                        user, brand_header(state["lang"]),
                        ("Tipo de embarcación" if is_es(state["lang"]) else "Vessel type"),
                        "Two Travel", rows, section_title=("Tipos" if is_es(state["lang"]) else "Types")
                    )
                    continue

                if state["step"] == "boats_type":
                    type_map = {
                        "boat_type_speed":"speedboat",
                        "boat_type_yacht":"yacht",
                        "boat_type_cat":"catamaran",
                    }
                    if sel_id in type_map:
                        state["boat_type"] = type_map[sel_id]
                    else:
                        state["boat_type"] = (text or "").strip()

                    if not GOOGLE_SHEET_CSV_URL:
                        wa_send_text(user, "⚠️ Aún no tengo el catálogo conectado. ¿Te conecto con *Ventas* para cotización?")
                        state["step"] = "post_results"
                        continue

                    top = find_top(
                        service="boats",
                        city=(state.get("city") or "cartagena"),
                        pax=int(state.get("pax") or 0),
                        prefs=(state.get("boat_type") or ""),
                        top_k=TOP_K
                    )
                    unit = "día" if is_es(state["lang"]) else "day"
                    wa_send_text(user, reply_topN(state["lang"], top, unit))
                    state["step"] = "post_results"
                    continue

                # ===== WEDDINGS =====
                if state["step"] == "weddings_form":
                    state["weddings_info"] = (text or "")
                    wa_send_text(user,
                                 "Con esa información preparo un *estimado*. ¿Te conecto con *Weddings* para afinar propuesta y visitas?"
                                 if is_es(state["lang"]) else
                                 "We’ll prepare an *estimate*. Connect with *Weddings* to refine the proposal and schedule site visits?")
                    state["step"] = "post_results"
                    continue

                # ===== CONCIERGE =====
                if state["step"] == "concierge_form":
                    state["concierge_info"] = (text or "")
                    wa_send_text(user,
                                 "Servicio 100% personalizado. *Desde* un estimado por persona. Ventas confirma el valor final. ¿Te conecto con Ventas?"
                                 if is_es(state["lang"]) else
                                 "100% personalized. *From* an estimated per-person rate. Sales confirms final pricing. Connect with Sales?")
                    state["step"] = "post_results"
                    continue

                # ===== POST-RESULTS =====
                if state["step"] == "post_results":
                    # Botones (2): Añadir otro / Ventas
                    wa_send_buttons(
                        user,
                        add_another_or_sales(state["lang"]),
                        [
                            {"id":"post_add","title":"1️⃣ " + ("Añadir otro" if is_es(state["lang"]) else "Add another")},
                            {"id":"post_sales","title":"2️⃣ " + ("Conectar con Ventas" if is_es(state["lang"]) else "Connect with Sales")},
                        ]
                    )
                    state["step"] = "post_results_wait"
                    continue

                if state["step"] == "post_results_wait":
                    if sel_id == "post_add":
                        state["step"] = "menu"
                        # Re-mostrar menú LIST
                        rows = [
                            {"id":"menu_1_villas","title":"1️⃣ Villas & Casas 🏠"},
                            {"id":"menu_2_boats","title":"2️⃣ Botes & Yates 🚤"},
                            {"id":"menu_3_islands","title":"3️⃣ Islas Privadas 🏝️"},
                            {"id":"menu_4_weddings","title":"4️⃣ Bodas & Eventos 💍🎉"},
                            {"id":"menu_5_concierge","title":"5️⃣ Concierge ✨"},
                            {"id":"menu_6_sales","title":"6️⃣ Hablar con ventas 👤"},
                        ]
                        wa_send_list(user, brand_header(state["lang"]),
                                     ("Elige un servicio" if is_es(state["lang"]) else "Choose a service"),
                                     "Two Travel", rows)
                        continue
                    if sel_id == "post_sales":
                        state["step"] = "handoff"
                        owner_name, team = "Two Travel Advisor", "Two Travel"
                        wa_send_text(user, handoff_client(state["lang"], owner_name, team))
                        continue
                    # si escribe texto, vuelvo a preguntar con botones
                    wa_send_buttons(
                        user,
                        add_another_or_sales(state["lang"]),
                        [
                            {"id":"post_add","title":"1️⃣ " + ("Añadir otro" if is_es(state["lang"]) else "Add another")},
                            {"id":"post_sales","title":"2️⃣ " + ("Conectar con Ventas" if is_es(state["lang"]) else "Connect with Sales")},
                        ]
                    )
                    continue

    return {"ok": True}
