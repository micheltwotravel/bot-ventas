# main.py
import os, re, csv, io, requests
from fastapi import FastAPI, Request

app = FastAPI()

# ====== ENV ======
VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")
WA_TOKEN     = os.getenv("WA_ACCESS_TOKEN")
WA_PHONE_ID  = os.getenv("WA_PHONE_NUMBER_ID")

HUBSPOT_TOKEN         = os.getenv("HUBSPOT_TOKEN")  # Private App
GOOGLE_SHEET_CSV_URL  = os.getenv("GOOGLE_SHEET_CSV_URL")  # CSV público

# ====== Estado simple en memoria (MVP) ======
SESSIONS = {}  # { phone: {"lang": "ES/EN", "step": "...", "name": "", "email": "", ...} }

# ====== Helpers WhatsApp ======
def wa_send_text(to: str, body: str):
    url = f"https://graph.facebook.com/v22.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print("WA send:", r.status_code, r.text)
    return r.status_code

# ====== Helpers HubSpot ======
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def hubspot_upsert_contact(name: str, email: str, phone: str, lang: str):
    """
    Crea o actualiza un contacto por email usando la API v3.
    Si existe -> lo busca y hace PATCH; si no -> lo crea.
    """
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

    # 1) intenta crear
    r = requests.post(base, headers=headers, json={"properties": props}, timeout=20)
    if r.status_code == 201:
        print("HubSpot contact created", r.json().get("id"))
        return True

    # 2) si ya existe (409), busca y actualiza
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
            print("HubSpot update:", up.status_code, up.text)
            return up.ok

    print("HubSpot upsert error:", r.status_code, r.text)
    return False

# ====== Helpers Google Sheet ======
def load_catalog():
    """
    Descarga el CSV del catálogo y lo devuelve como lista de dicts.
    Columnas esperadas:
      service_type, name, city, capacity_max, price_from_usd, price_unit,
      preference_tags, url_web, url_brochure (al menos una URL)
    """
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
        rows.append({k.strip(): (v or "").strip() for k,v in row.items()})
    print("Catalog rows:", len(rows))
    return rows

def find_top5(service: str, city: str, pax: int, prefs: str):
    service = (service or "").strip().lower()
    city    = (city or "").strip().lower()
    prefs   = [p.strip().lower() for p in (prefs or "").split(",") if p.strip()]

    rows = load_catalog()
    if not rows: 
        return []

    def row_ok(r):
        if (r.get("service_type","").lower() != service):
            return False
        if city and r.get("city","").lower() != city:
            return False
        cap = 0
        try:
            cap = int(float(r.get("capacity_max","0") or "0"))
        except:
            cap = 0
        if pax and cap < pax: 
            return False
        # preferencias: si el usuario mandó alguna, que al menos una matchee
        if prefs:
            tags = [t.strip().lower() for t in (r.get("preference_tags","") or "").split(",") if t.strip()]
            if not any(p in tags for p in prefs):
                return False
        return True

    filtered = [r for r in rows if row_ok(r)]

    # Orden simple por precio_from_usd asc
    def price_val(r):
        try:
            return float(r.get("price_from_usd","999999") or "999999")
        except:
            return 999999.0

    filtered.sort(key=price_val)
    return filtered[:5]

# ====== Mensajes ======
OPENER_ES = ("¡Hola! Soy tu concierge virtual de TWOTRAVEL 🛎️✨.\n"
             "Puedo ayudarte con **villas, botes, islas, bodas/eventos y concierge**.\n"
             "¿En qué idioma prefieres continuar? Escribe: *ES* o *EN*.")
OPENER_EN = ("Hi! I’m your TWOTRAVEL virtual concierge 🛎️✨.\n"
             "I can help with **villas, boats, islands, weddings/events and concierge**.\n"
             "Which language would you prefer? Type: *ES* or *EN*.")

MENU_ES = ("Genial. ¿Qué necesitas hoy?\n"
           "- *Villas* 🏠\n- *Botes* 🚤\n- *Islas* 🏝️\n- *Bodas* 💍🎉\n- *Concierge* ✨\n- *Ventas* 👤 (hablar con humano)")
MENU_EN = ("Great. What do you need today?\n"
           "- *Villas* 🏠\n- *Boats* 🚤\n- *Islands* 🏝️\n- *Weddings* 💍🎉\n- *Concierge* ✨\n- *Sales* 👤 (talk to a human)")

def ask_contact(lang):
    if (lang or "ES").upper().startswith("ES"):
        return "Para enviarte opciones y una cotización, comparte:\n📛 *Nombre completo*:"
    else:
        return "To share options and a quote, please send:\n📛 *Full name*:"

def ask_email(lang):
    return ("📧 *Correo electrónico:*" if (lang or "ES").upper().startswith("ES")
            else "📧 *Email address:*")

def ask_villas_questions(lang):
    if (lang or "ES").upper().startswith("ES"):
        return ("Perfecto. Para *Villas* necesito:\n"
                "• *Ciudad* (Cartagena / Medellín / Tulum / CDMX)\n"
                "• *Fechas* check-in y check-out (YYYY-MM-DD a YYYY-MM-DD)\n"
                "• *Número de personas*\n"
                "• *Preferencias* (frente al mar, centro histórico, exclusiva, cualquiera)")
    else:
        return ("Great. For *Villas* I need:\n"
                "• *City* (Cartagena / Medellín / Tulum / CDMX)\n"
                "• *Dates* check-in & check-out (YYYY-MM-DD to YYYY-MM-DD)\n"
                "• *Guests*\n"
                "• *Preference* (oceanfront, historic, exclusive, no preference)")

def reply_top5(lang, items):
    if not items:
        return ("No veo opciones con esos filtros. ¿Probamos fechas cercanas (±3 días) "
                "o ajustamos el tamaño del grupo?") if (lang or "ES").upper().startswith("ES") \
               else ("I couldn’t find matches. Try nearby dates (±3 days) or adjust party size?")
    lines = []
    for r in items:
        name = r.get("name","")
        cap = r.get("capacity_max","")
        price = r.get("price_from_usd","")
        url = r.get("url_web") or r.get("url_brochure") or ""
        unit = r.get("price_unit","night")
        if (lang or "ES").upper().startswith("ES"):
            lines.append(f"• {name} ({cap} pax) — USD {price}/{unit} → {url}")
        else:
            lines.append(f"• {name} ({cap} guests) — USD {price}/{unit} → {url}")
    if (lang or "ES").upper().startswith("ES"):
        lines.append("\nLa disponibilidad final la confirma ventas antes de reservar. ¿Te conecto con ventas para confirmar?")
    else:
        lines.append("\nFinal availability is confirmed by sales before booking. Connect with sales?")
    return "\n".join(lines)

# ====== Startup logs ======
@app.on_event("startup")
async def show_routes():
    print("BOOT> Routes:", [r.path for r in app.router.routes])
    print("BOOT> WA_PHONE_ID:", WA_PHONE_ID)
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
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return str(challenge or "")
    return {"error": "verification failed"}

# ====== Webhook Incoming (POST) ======
@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", data)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Evitar loops con statuses delivery/read
            if value.get("statuses"):
                continue

            for m in value.get("messages", []):
                user = m.get("from")
                if not user:
                    continue
                text = (m.get("text", {}) or {}).get("body", "").strip()
                state = SESSIONS.setdefault(user, {"step": "lang"})

                # 1) Idioma
                if state["step"] == "lang":
                    if text.lower() in ("es","español","1"):
                        state["lang"] = "ES"
                    elif text.lower() in ("en","english","2"):
                        state["lang"] = "EN"
                    else:
                        # mostrar opener bilingüe si no eligió
                        wa_send_text(user, OPENER_ES + "\n\n" + OPENER_EN)
                        continue
                    # pedir nombre
                    state["step"] = "name"
                    wa_send_text(user, ask_contact(state["lang"]))
                    continue

                # 2) Nombre
                if state["step"] == "name":
                    if len(text.split()) < 2:
                        wa_send_text(user, "¿Me confirmas tu *nombre y apellido*?" if state["lang"]=="ES"
                                     else "Could you share *name and last name*?")
                        continue
                    state["name"] = text
                    state["step"] = "email"
                    wa_send_text(user, ask_email(state["lang"]))
                    continue

                # 3) Email
                if state["step"] == "email":
                    if not EMAIL_RE.match(text):
                        wa_send_tex
