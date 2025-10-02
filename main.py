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

# Calendarios (si no hay ENV, usamos los que pasaste)
CAL_RAY   = (os.getenv("CAL_RAY")   or "https://meetings.hubspot.com/ray-kanevsky?uuid=280bb17d-4006-4bd1-9560-9cefa9752d5d").strip()
CAL_SOFIA = (os.getenv("CAL_SOFIA") or "https://marketing.two.travel/meetings/sofia217").strip()
CAL_ROSS  = (os.getenv("CAL_ROSS")  or "https://meetings.hubspot.com/ross334?uuid=68031520-950b-4493-b5ad-9cde268edbc8").strip()

# Catálogo
GOOGLE_SHEET_CSV_URL = (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()
TOP_K = int(os.getenv("TOP_K", "3"))  # 2 o 3

# Estado
SESSIONS = {}  # { phone: {...} }

# ==================== WhatsApp helpers ====================
def _post_graph(path: str, payload: dict):
    url = f"https://graph.facebook.com/v23.0/{path}"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print(f"WA -> {r.status_code} {r.text[:220]}")
    return r

def wa_send_text(to: str, body: str):
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}}
    return _post_graph(f"{WA_PHONE_ID}/messages", payload)

def wa_send_buttons(to: str, body_text: str, buttons: list):
    """
    buttons: [{"id":"BTN_ID","title":"Title"}, ...]  (máx 3)
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
    rows: [{"id":"CITY_CARTAGENA","title":"Cartagena"}, ...]
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

# ==================== Copy / UI ====================
def opener_buttons():
    return "Welcome to Two Travel ✨\nChoose your language / Elige tu idioma"

def menu_main_es(): return "Genial. ¿Qué necesitas hoy?"
def menu_main_en(): return "Great. What do you need today?"

def menu_more_es(): return "Más opciones"
def menu_more_en(): return "More options"

def ask_name_es(): return "📛 *Nombre y apellido:*"
def ask_name_en(): return "📛 *Full name:*"

def ask_email_es(): return "📧 *Correo electrónico:*"
def ask_email_en(): return "📧 *Email address:*"

def ask_city_es(): return "Elige tu *ciudad*:"
def ask_city_en(): return "Choose your *city*:"

def ask_date_es():
    return "📅 *Fecha del viaje* (formato `YYYY-MM-DD`, ej. 2025-04-12)\nSi aún no sabes, toca el botón."
def ask_date_en():
    return "📅 *Trip date* (format `YYYY-MM-DD`, e.g. 2025-04-12)\nIf you’re not sure yet, use the button."

def ask_pax_es(): return "👥 ¿Para cuántas *personas*? (puedes escribir: “somos 5 personas”)"
def ask_pax_en(): return "👥 How many *guests*? (you can write: “we are 5 people”)"

def after_results_es(): return "¿Quieres *añadir otro servicio* o *hablar con el equipo*?"
def after_results_en(): return "Would you like to *add another service* or *talk to the team*?"

def no_catalog_es(): return "⚠️ Aún no tengo el *catálogo* conectado. Puedo conectarte con *el equipo* para una cotización personalizada."
def no_catalog_en(): return "⚠️ The *catalog* isn’t connected yet. I can connect you with *the team* for a personalized quote."

def handoff_msg(city_norm: str, lang: str, cal_url: str, agent_label: str):
    if lang == "ES":
        return (f"Te conecto con el *equipo de {city_norm.title()}*. "
                f"Para hablar pronto, aquí tienes su *calendario*: {cal_url}\n"
                f"— {agent_label}")
    return (f"I’m connecting you with the *{city_norm.title()} team*. "
            f"To speak soon, here’s their *calendar*: {cal_url}\n"
            f"— {agent_label}")

def result_header_es(n): return f"Top {n} recomendadas (precios *desde*):"
def result_header_en(n): return f"Top {n} picks (*prices from*):"
def result_footer_es(): return "La *disponibilidad final* la confirma nuestro equipo antes de reservar."
def result_footer_en(): return "Final *availability* is confirmed by our team before booking."

# Botones
BTN_LANG_ES = {"id":"LANG_ES","title":"ES 🇪🇸"}
BTN_LANG_EN = {"id":"LANG_EN","title":"EN 🇺🇸"}

BTN_VILLAS = {"id":"SVC_VILLAS","title":"Villas & Homes 🏠"}
BTN_BOATS  = {"id":"SVC_BOATS","title":"Boats & Yachts 🚤"}
BTN_MORE   = {"id":"SVC_MORE","title":"More… ➕"}

BTN_WEDDINGS = {"id":"SVC_WEDDINGS","title":"Weddings & Events 💍🎉"}
BTN_CONCIERGE= {"id":"SVC_CONCIERGE","title":"Concierge ✨"}
BTN_TALK     = {"id":"SVC_TALK","title":"Talk to the Team 👤"}

BTN_DATE_UNKNOWN_ES = {"id":"DATE_UNKNOWN","title":"Aún no sé"}
BTN_DATE_UNKNOWN_EN = {"id":"DATE_UNKNOWN","title":"I don’t know"}

BTN_ADD_SERVICE_ES = {"id":"ADD_SERVICE","title":"Añadir otro servicio"}
BTN_TALK_TEAM_ES   = {"id":"TALK_TEAM","title":"Hablar con el equipo"}
BTN_ADD_SERVICE_EN = {"id":"ADD_SERVICE","title":"Add another service"}
BTN_TALK_TEAM_EN   = {"id":"TALK_TEAM","title":"Talk to the team"}

# Ciudades
CITY_ROWS = [
    {"id":"CITY_CARTAGENA","title":"Cartagena"},
    {"id":"CITY_MEDELLIN","title":"Medellín"},
    {"id":"CITY_TULUM","title":"Tulum"},
    {"id":"CITY_MEXICO","title":"CDMX / México"},
]

# ==================== Utilidades ====================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def normalize_city(s: str) -> str:
    s = (s or "").strip().lower()
    if "cart" in s: return "cartagena"
    if "medell" in s: return "medellín"
    if "tulum" in s: return "tulum"
    if "mex" in s or "cdmx" in s: return "méxico"
    return s or "cartagena"

def calendar_for_city(city_norm: str) -> tuple[str,str]:
    # devuelve (url, label firma)
    if city_norm in ("cartagena","tulum"):
        return CAL_SOFIA, "Sofía – Two Travel"
    if city_norm.startswith("medell"):
        return CAL_ROSS, "Ross – Two Travel"
    # México/CDMX por defecto Ray
    return CAL_RAY, "Ray – Two Travel"

def owner_for_city(city_norm: str) -> str | None:
    if city_norm in ("cartagena","tulum"): return HUBSPOT_OWNER_SOFIA or None
    if city_norm.startswith("medell"):     return HUBSPOT_OWNER_ROSS  or None
    return HUBSPOT_OWNER_RAY or None  # México/CDMX default

def is_valid_date(s: str) -> bool:
    if not DATE_RE.match(s or ""): return False
    try:
        datetime.date.fromisoformat(s)
        return True
    except Exception:
        return False

def parse_pax(text: str) -> int | None:
    nums = re.findall(r"\d+", text or "")
    if not nums: return None
    try:
        n = int(nums[0])
        return n if n > 0 else None
    except:
        return None

def valid_name(fullname: str) -> bool:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return len(tokens) >= 2

def normalize_name(fullname: str) -> str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return " ".join(tokens[:3]).title()

# ==================== Catálogo ====================
def load_catalog():
    if not GOOGLE_SHEET_CSV_URL:
        print("WARN: GOOGLE_SHEET_CSV_URL missing")
        return []
    r = requests.get(GOOGLE_SHEET_CSV_URL, timeout=30)
    if not r.ok:
        print("Catalog error:", r.status_code, r.text[:160])
        return []
    rows = []
    content = r.content.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    print("Catalog rows:", len(rows))
    return rows

def _match_rows(rows, service, city=None, pax=None, prefs=None):
    service = (service or "").lower().strip()
    city    = (city or "").lower().strip()
    prefs_l = [p.strip().lower() for p in (prefs or "").split(",") if p.strip()]
    def ok(r):
        if (r.get("service_type","").lower() != service): return False
        if city and (r.get("city","").lower() != city):    return False
        if pax:
            try:
                cap = int(float(r.get("capacity_max","0") or "0"))
                if cap < pax: return False
            except: return False
        if prefs_l:
            tags = [t.strip().lower() for t in (r.get("preference_tags","") or "").split(",") if t.strip()]
            if not any(p in tags for p in prefs_l): return False
        return True
    out = [r for r in rows if ok(r)]
    out.sort(key=lambda r: float((r.get("price_from_usd") or "999999") or "999999"))
    return out

def find_top_relaxed(service, city, pax, prefs, top_k=TOP_K):
    rows = load_catalog()
    if not rows: return []
    # 1) service+city+pax
    res = _match_rows(rows, service, city, pax, prefs)
    if res: return res[:max(1, int(top_k or 1))]
    # 2) service+city
    res = _match_rows(rows, service, city, None, None)
    if res: return res[:max(1, int(top_k or 1))]
    # 3) service only
    res = _match_rows(rows, service, None, None, None)
    return res[:max(1, int(top_k or 1))]

# ==================== HubSpot ====================
def hubspot_upsert_contact(name: str, email: str, phone: str, lang: str) -> str | None:
    if not HUBSPOT_TOKEN: return None
    base = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    props = {
        "email": email,
        "phone": phone,
        "firstname": (name.split()[0] if name else None),
        "lastname": (" ".join(name.split()[1:]) if name and len(name.split()) > 1 else None),
        "preferred_language": ("es" if (lang or "ES")=="ES" else "en"),
        "source": "Two Travel WhatsApp Bot",
        "hs_lead_status": "NEW",
        "lifecyclestage": "lead",
    }
    r = requests.post(base, headers=headers, json={"properties": props}, timeout=20)
    if r.status_code == 201:
        return (r.json() or {}).get("id")

    if r.status_code == 409:
        s = requests.post(f"{base}/search", headers=headers, json={
            "filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value": email}]}],
            "properties":["email"]
        }, timeout=20)
        if s.ok and (s.json().get("results") or []):
            cid = s.json()["results"][0]["id"]
            up = requests.patch(f"{base}/{cid}", headers=headers, json={"properties": props}, timeout=20)
            return cid if up.ok else cid

    print("HubSpot contact upsert error:", r.status_code, r.text[:200])
    return None

def hubspot_create_deal(contact_id: str, owner_id: str | None, service_type: str, city: str, trip_date: str | None, pax: int | None) -> str | None:
    if not HUBSPOT_TOKEN or not contact_id: return None
    base = "https://api.hubapi.com/crm/v3/objects/deals"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    props = {
        "dealname": f"{service_type.title()} – {city.title()} ({pax or 'N/A'} pax)",
        "pipeline": "default",
        "dealstage": "appointmentscheduled",
        "source": "Two Travel WhatsApp Bot",
        "city": city,  # crea props personalizadas en HubSpot si no existen
        "service_type": service_type,
        "party_size": str(pax or ""),
    }
    if trip_date:
        props["trip_date"] = trip_date
    if owner_id:
        props["hubspot_owner_id"] = owner_id

    # Intentamos asociar en el mismo POST (HUBSPOT_DEFINED: 3 suele ser deal<->contact)
    payload = {
        "properties": props,
        "associations":[
            {"to":{"id": contact_id},"types":[{"associationCategory":"HUBSPOT_DEFINED","associationTypeId":3}]}
        ]
    }
    r = requests.post(base, headers=headers, json=payload, timeout=20)
    if r.status_code in (201,200):
        return (r.json() or {}).get("id")

    print("Deal create error:", r.status_code, r.text[:200])
    return None

# ==================== FastAPI ====================
@app.on_event("startup")
async def show_routes():
    print("BOOT> Routes:", [r.path for r in app.router.routes])
    print("BOOT> WA_PHONE_ID:", repr(WA_PHONE_ID))
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

@app.get("/")
def root():
    return {"ok": True, "routes": [r.path for r in app.router.routes]}

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

            # Ignore delivery statuses
            if value.get("statuses"):
                continue

            for m in value.get("messages", []):
                user = m.get("from")
                if not user:
                    continue

                # Primer mensaje: saludo + idioma por botones
                if user not in SESSIONS:
                    SESSIONS[user] = {"step":"lang","lang":"ES"}
                    wa_send_buttons(user, opener_buttons(), [BTN_LANG_ES, BTN_LANG_EN])
                    continue

                text, rid = extract_text_or_reply(m)
                state = SESSIONS[user]

                # ========== 0) Idioma ==========
                if state["step"] == "lang":
                    if rid == "LANG_ES" or (text.lower() in ("es","español")):
                        state["lang"] = "ES"
                    elif rid == "LANG_EN" or (text.lower() in ("en","english")):
                        state["lang"] = "EN"
                    else:
                        wa_send_buttons(user, opener_buttons(), [BTN_LANG_ES, BTN_LANG_EN])
                        continue
                    # pedir nombre
                    state["step"] = "contact_name"
                    wa_send_text(user, ask_name_es() if state["lang"]=="ES" else ask_name_en())
                    continue

                # ========== 1) Nombre ==========
                if state["step"] == "contact_name":
                    if not valid_name(text):
                        # no cortamos el flujo: avisamos y seguimos con lo recibido
                        name_used = (text or "Guest").strip()
                        wa_send_text(user, ("(Tip) Usa nombre y apellido, lo registré como: " + name_used) if state["lang"]=="ES"
                                           else "(Tip) Use first & last name; I saved: " + name_used)
                        state["name"] = name_used
                    else:
                        state["name"] = normalize_name(text)
                    state["step"] = "contact_email"
                    wa_send_text(user, ask_email_es() if state["lang"]=="ES" else ask_email_en())
                    continue

                # ========== 2) Email ==========
                if state["step"] == "contact_email":
                    if not EMAIL_RE.match(text or ""):
                        wa_send_text(user, ("Ese correo no parece válido; lo podemos corregir luego.") if state["lang"]=="ES"
                                           else "That email looks invalid; we can fix it later.")
                        state["email"] = (text or "").strip()
                    else:
                        state["email"] = (text or "").strip()
                    # Upsert contacto (best effort)
                    try:
                        cid = hubspot_upsert_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                        state["contact_id"] = cid
                    except Exception as e:
                        print("HubSpot contact error:", e)

                    # Menú principal
                    if state["lang"]=="ES":
                        wa_send_buttons(user, menu_main_es(), [BTN_VILLAS, BTN_BOATS, BTN_MORE])
                    else:
                        wa_send_buttons(user, menu_main_en(), [BTN_VILLAS, BTN_BOATS, BTN_MORE])
                    state["step"] = "menu_main"
                    continue

                # ========== 3) Menú principal ==========
                if state["step"] == "menu_main":
                    if rid == "SVC_VILLAS":
                        state["service_type"] = "villas"
                        state["step"] = "city"
                        if state["lang"]=="ES":
                            wa_send_list(user, "Two Travel", ask_city_es(), "Elegir", CITY_ROWS)
                        else:
                            wa_send_list(user, "Two Travel", ask_city_en(), "Choose", CITY_ROWS)
                        continue
                    if rid == "SVC_BOATS":
                        state["service_type"] = "boats"
                        state["step"] = "city"
                        if state["lang"]=="ES":
                            wa_send_list(user, "Two Travel", ask_city_es(), "Elegir", CITY_ROWS)
                        else:
                            wa_send_list(user, "Two Travel", ask_city_en(), "Choose", CITY_ROWS)
                        continue
                    if rid == "SVC_MORE":
                        if state["lang"]=="ES":
                            wa_send_buttons(user, menu_more_es(), [BTN_WEDDINGS, BTN_CONCIERGE, BTN_TALK])
                        else:
                            wa_send_buttons(user, menu_more_en(), [BTN_WEDDINGS, BTN_CONCIERGE, BTN_TALK])
                        state["step"] = "menu_more"
                        continue
                    # fallback
                    if state["lang"]=="ES":
                        wa_send_buttons(user, menu_main_es(), [BTN_VILLAS, BTN_BOATS, BTN_MORE])
                    else:
                        wa_send_buttons(user, menu_main_en(), [BTN_VILLAS, BTN_BOATS, BTN_MORE])
                    continue

                # ========== 3b) Menú more ==========
                if state["step"] == "menu_more":
                    if rid == "SVC_WEDDINGS":
                        state["service_type"] = "weddings"
                        state["step"] = "city"
                        if state["lang"]=="ES":
                            wa_send_list(user, "Two Travel", ask_city_es(), "Elegir", CITY_ROWS)
                        else:
                            wa_send_list(user, "Two Travel", ask_city_en(), "Choose", CITY_ROWS)
                        continue
                    if rid == "SVC_CONCIERGE":
                        state["service_type"] = "concierge"
                        state["step"] = "city"
                        if state["lang"]=="ES":
                            wa_send_list(user, "Two Travel", ask_city_es(), "Elegir", CITY_ROWS)
                        else:
                            wa_send_list(user, "Two Travel", ask_city_en(), "Choose", CITY_ROWS)
                        continue
                    if rid == "SVC_TALK":
                        # Sin ciudad elegida: por defecto Ray/CDMX
                        city_norm = "méxico"
                        cal, label = calendar_for_city(city_norm)
                        wa_send_text(user, handoff_msg(city_norm, state["lang"], cal, label))
                        state["step"] = "handoff"
                        # Deal rápido (sin ciudad definida por el user)
                        try:
                            cid = state.get("contact_id")
                            owner = owner_for_city(city_norm)
                            hubspot_create_deal(cid, owner, "consult", city_norm, None, None)
                        except Exception as e:
                            print("Deal error:", e)
                        continue
                    # fallback
                    if state["lang"]=="ES":
                        wa_send_buttons(user, menu_more_es(), [BTN_WEDDINGS, BTN_CONCIERGE, BTN_TALK])
                    else:
                        wa_send_buttons(user, menu_more_en(), [BTN_WEDDINGS, BTN_CONCIERGE, BTN_TALK])
                    continue

                # ========== 4) Ciudad ==========
                if state["step"] == "city":
                    # acepta list id o texto
                    cid = (rid or "").upper()
                    t = (text or "")
                    if cid.startswith("CITY_"):
                        if "CARTAGENA" in cid: city_norm = "cartagena"
                        elif "MEDELLIN" in cid: city_norm = "medellín"
                        elif "TULUM" in cid: city_norm = "tulum"
                        else: city_norm = "méxico"
                    else:
                        city_norm = normalize_city(t)

                    state["city"] = city_norm
                    state["step"] = "date"
                    # fecha estricta + botón "Aún no sé"
                    if state["lang"]=="ES":
                        wa_send_buttons(user, ask_date_es(), [BTN_DATE_UNKNOWN_ES])
                    else:
                        wa_send_buttons(user, ask_date_en(), [BTN_DATE_UNKNOWN_EN])
                    continue

                # ========== 5) Fecha ==========
                if state["step"] == "date":
                    unknown = (rid == "DATE_UNKNOWN")
                    if unknown:
                        state["trip_date"] = None
                    else:
                        if is_valid_date(text):
                            state["trip_date"] = text
                        else:
                            # No rompemos el flujo: dejamos "por definir" y avisamos
                            msg = "Formato de fecha no válido; lo dejo *por definir*." if state["lang"]=="ES" \
                                  else "Invalid date format; I’ll leave it as *TBD*."
                            wa_send_text(user, msg)
                            state["trip_date"] = None
                    state["step"] = "pax"
                    wa_send_text(user, ask_pax_es() if state["lang"]=="ES" else ask_pax_en())
                    continue

                # ========== 6) Pax ==========
                if state["step"] == "pax":
                    pax = parse_pax(text)
                    state["pax"] = pax
                    svc = state.get("service_type")
                    city_norm = state.get("city")

                    # Si no hay catálogo, handoff
                    if not GOOGLE_SHEET_CSV_URL:
                        wa_send_text(user, no_catalog_es() if state["lang"]=="ES" else no_catalog_en())
                        state["step"] = "after_results"
                        # Crear deal igualmente
                        try:
                            cid = state.get("contact_id")
                            owner = owner_for_city(city_norm or "méxico")
                            hubspot_create_deal(cid, owner, (svc or "consult"), (city_norm or "méxico"), state.get("trip_date"), pax)
                        except Exception as e:
                            print("Deal error:", e)
                        continue

                    # Buscamos top (relajando filtros si hace falta)
                    top = find_top_relaxed(svc or "villas", city_norm, pax, None, TOP_K)

                    # Render respuesta
                    if state["lang"]=="ES":
                        header = result_header_es(len(top))
                        lines = [header]
                        unit = "noche" if (svc=="villas") else ("día" if svc=="boats" else "noche")
                        for r in top:
                            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} pax) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
                        lines.append(result_footer_es())
                        wa_send_text(user, "\n".join(lines))
                        wa_send_buttons(user, after_results_es(), [BTN_ADD_SERVICE_ES, BTN_TALK_TEAM_ES])
                    else:
                        header = result_header_en(len(top))
                        lines = [header]
                        unit = "night" if (svc=="villas") else ("day" if svc=="boats" else "night")
                        for r in top:
                            lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} guests) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
                        lines.append(result_footer_en())
                        wa_send_text(user, "\n".join(lines))
                        wa_send_buttons(user, after_results_en(), [BTN_ADD_SERVICE_EN, BTN_TALK_TEAM_EN])

                    # Crear deal en HubSpot (best effort)
                    try:
                        cid = state.get("contact_id")
                        owner = owner_for_city(city_norm or "méxico")
                        hubspot_create_deal(cid, owner, (svc or "villas"), (city_norm or ""), state.get("trip_date"), pax)
                    except Exception as e:
                        print("Deal error:", e)

                    state["step"] = "after_results"
                    continue

                # ========== 7) After results ==========
                if state["step"] == "after_results":
                    if rid == "ADD_SERVICE":
                        # Volver al menú principal
                        if state["lang"]=="ES":
                            wa_send_buttons(user, menu_main_es(), [BTN_VILLAS, BTN_BOATS, BTN_MORE])
                        else:
                            wa_send_buttons(user, menu_main_en(), [BTN_VILLAS, BTN_BOATS, BTN_MORE])
                        state["step"] = "menu_main"
                        continue
                    if rid == "TALK_TEAM":
                        city_norm = state.get("city") or "méxico"
                        cal, label = calendar_for_city(city_norm)
                        wa_send_text(user, handoff_msg(city_norm, state["lang"], cal, label))
                        state["step"] = "handoff"
                        continue
                    # fallback → ofrecer de nuevo opciones
                    if state["lang"]=="ES":
                        wa_send_buttons(user, after_results_es(), [BTN_ADD_SERVICE_ES, BTN_TALK_TEAM_ES])
                    else:
                        wa_send_buttons(user, after_results_en(), [BTN_ADD_SERVICE_EN, BTN_TALK_TEAM_EN])
                    continue

                # ========== 8) Handoff (bot se silencia) ==========
                if state["step"] == "handoff":
                    # no respondemos más (deja la conversación al equipo)
                    print("Handoff active; ignoring bot replies for", user)
                    continue

    return {"ok": True}
