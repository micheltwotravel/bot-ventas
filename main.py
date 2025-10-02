# main.py
import os, re, csv, io, json, requests
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

# =============== ENV (strip para evitar espacios/saltos) ======================
VERIFY_TOKEN        = (os.getenv("WA_VERIFY_TOKEN") or "").strip()
WA_TOKEN            = (os.getenv("WA_ACCESS_TOKEN") or "").strip()
WA_PHONE_ID         = (os.getenv("WA_PHONE_NUMBER_ID") or "").strip()
TOP_K               = int(os.getenv("TOP_K", "3").strip() or 3)

HUBSPOT_TOKEN       = (os.getenv("HUBSPOT_TOKEN") or "").strip()              # Private App
HUBSPOT_PIPELINE    = (os.getenv("HUBSPOT_PIPELINE") or "default").strip()    # ej: "default" o id de pipeline
HUBSPOT_STAGE       = (os.getenv("HUBSPOT_STAGE") or "appointmentscheduled").strip()
HUBSPOT_OWNER_ID    = (os.getenv("HUBSPOT_OWNER_ID") or "").strip()           # opcional: asignar propietario
GOOGLE_SHEET_CSV_URL= (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()

# =============== Estado en memoria (MVP) ======================================
# Por número de WhatsApp
SESSIONS = {}  # { phone: {step, lang, name, email, service_type, ...} }

# ==============================================================================
#                               WhatsApp helpers
# ==============================================================================
def _wa_headers():
    return {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    }

def wa_send_text(to: str, body: str) -> int:
    """Envío de texto simple. Maneja logs útiles."""
    url = f"https://graph.facebook.com/v23.0/{WA_PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    try:
        r = requests.post(url, headers=_wa_headers(), json=payload, timeout=20)
    except Exception as e:
        print("WA send exception:", e)
        return 0
    print(f"WA send -> {r.status_code} to={to} resp={r.text[:180]}")
    if r.status_code == 401:
        print("⚠️ WA TOKEN INVALID/EXPIRED. Revisa WA_ACCESS_TOKEN en Render.")
    if r.status_code == 400:
        print(f"⚠️ BAD REQUEST. phone_id={repr(WA_PHONE_ID)}")
    return r.status_code

def extract_text(m: dict) -> str:
    """Extrae texto del mensaje (text/button/interactive) y normaliza."""
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
    return ""  # stickers/medios no tienen texto útil

# ==============================================================================
#                                HubSpot helpers
# ==============================================================================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _hs_headers():
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }

def hs_upsert_contact(name: str, email: str, phone: str, lang: str):
    """Crea o actualiza un Contact en HubSpot."""
    if not HUBSPOT_TOKEN:
        print("WARN: HUBSPOT_TOKEN missing")
        return None

    base = "https://api.hubapi.com/crm/v3/objects/contacts"
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

    try:
        r = requests.post(base, headers=_hs_headers(), json={"properties": props}, timeout=20)
    except Exception as e:
        print("HubSpot create error:", e); return None

    if r.status_code == 201:
        cid = r.json().get("id")
        print("HubSpot contact created:", cid)
        return cid

    if r.status_code == 409:
        # Buscar por email y actualizar
        s = requests.post(f"{base}/search", headers=_hs_headers(), json={
            "filterGroups": [{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}],
            "properties": ["email"]
        }, timeout=20)
        if s.ok and s.json().get("results"):
            cid = s.json()["results"][0]["id"]
            up = requests.patch(f"{base}/{cid}", headers=_hs_headers(), json={"properties": props}, timeout=20)
            print("HubSpot contact update:", up.status_code)
            return cid
        print("HubSpot 409 but no results:", s.status_code, s.text[:150])
        return None

    print("HubSpot contact error:", r.status_code, r.text[:180])
    return None

def hs_create_deal(contact_id: str, service_type: str, city: str, date_range: str, pax: int, lang: str):
    """Crea un Deal (oportunidad) simple y lo asocia al Contact."""
    if not HUBSPOT_TOKEN or not contact_id:
        return None

    base = "https://api.hubapi.com/crm/v3/objects/deals"
    title = f"[WhatsApp] {service_type.title()} - {city or '—'} ({pax or 0} pax)"
    props = {
        "dealname": title,
        "pipeline": HUBSPOT_PIPELINE or "default",
        "dealstage": HUBSPOT_STAGE or "appointmentscheduled",
        "description": f"Origen: WhatsApp Bot\nServicio: {service_type}\nCiudad: {city}\nFechas: {date_range}\nPax: {pax}\nIdioma: {lang}",
        "source": "WhatsApp Bot",
    }
    if HUBSPOT_OWNER_ID:
        props["hubspot_owner_id"] = HUBSPOT_OWNER_ID

    try:
        r = requests.post(base, headers=_hs_headers(),
                          json={"properties": props,
                                "associations": [{
                                     "to": {"id": contact_id},
                                     "types": [{"associationCategory":"HUBSPOT_DEFINED","associationTypeId":3}]  # 3: deal-to-contact
                                }]},
                          timeout=20)
    except Exception as e:
        print("HubSpot deal create exception:", e); return None

    if r.ok:
        did = r.json().get("id")
        print("HubSpot deal created:", did)
        return did

    print("HubSpot deal error:", r.status_code, r.text[:180])
    return None

# ==============================================================================
#                             Catálogo (Google Sheet)
# ==============================================================================
def load_catalog():
    if not GOOGLE_SHEET_CSV_URL:
        print("WARN: GOOGLE_SHEET_CSV_URL missing")
        return []

    try:
        r = requests.get(GOOGLE_SHEET_CSV_URL, timeout=30)
    except Exception as e:
        print("Catalog download exception:", e); return []

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
    """Filtra catálogo y devuelve TOP N por precio ascendente."""
    rows = load_catalog()
    if not rows:
        return []

    service = (service or "").strip().lower()
    city    = (city or "").strip().lower()
    prefs_l = [p.strip().lower() for p in (prefs or "").split(",") if p.strip()]

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

# ==============================================================================
#                             Copy / Mensajería (ES/EN)
# ==============================================================================
def is_es(lang: str) -> bool:
    return (lang or "ES").upper().startswith("ES")

def opener_bi():
    return (
        "Two Travel ✨\n\n"
        "ES — ¡Hola! Soy tu concierge virtual. Puedo ayudarte con *villas*, *botes*, *islas*, *bodas/eventos* y *concierge*.\n"
        "¿En qué idioma prefieres continuar?\n\n"
        "EN — Hi! I’m your virtual concierge. I can help with *villas*, *boats*, *islands*, *weddings/events* and *concierge*.\n"
        "Which language would you prefer?"
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

def q_boats_city(lang):   return "¿Ciudad/puerto de salida? (*Cartagena*)" if is_es(lang) else "City/port of departure? (*Cartagena*)"
def q_boats_date(lang):   return "¿*Fecha* del paseo? (YYYY-MM-DD; ¿*día o noche*?)" if is_es(lang) else "Trip *date*? (YYYY-MM-DD; *day or night*?)"
def q_boats_pax(lang):    return "¿Número de *pasajeros*?" if is_es(lang) else "Number of *passengers*?"
def q_boats_type(lang):   return "Tipo: *Lancha* / *Yate* / *Catamarán*. Tour: *Cholón*, *Islas del Rosario*, etc." if is_es(lang) else "Type: *Speedboat* / *Yacht* / *Catamaran*. Tour: *Cholón*, *Rosario Islands*, etc."

def q_wed_city(lang):     return "Ciudad • *fecha aproximada* • *# invitados* • tipo de *venue* (playa, histórico, finca, moderno) • ¿*Full planning*?" if is_es(lang) else "City • *approx date* • *guest count* • *venue* type (beach, historic, estate, modern) • *Full planning*?"
def r_wed_estimate(lang): return ("Con esa info preparo un *estimado*. ¿Te conecto con *Weddings* para afinar propuesta y visitas?" if is_es(lang) else "We’ll prepare an *estimate*. Connect with *Weddings* to refine proposal and site visits?")

def q_concierge(lang):    return "Ciudad / Fechas / Servicios (reservas, transporte, chef, seguridad, experiencias privadas)." if is_es(lang) else "City / Dates / Services (reservations, transport, private chef, security, private experiences)."
def r_concierge(lang):    return ("Servicio 100% personalizado. *Desde* USD estimado por persona por viaje. *Ventas* confirma el valor final según agenda y servicios. ¿Te conecto con ventas?" if is_es(lang) else "100% personalized. *From* an estimated USD per person per trip. *Sales* will confirm final pricing. Connect with sales?")

def reply_topN(lang: str, items: list, unit: str = "noche"):
    if not items:
        return ("No veo opciones con esos filtros. ¿Intento con *fechas cercanas (±3 días)* o ajustamos el *tamaño del grupo*?"
                if is_es(lang) else
                "I couldn’t find matches. Try *nearby dates (±3 days)* or adjust the *party size*?")
    es = is_es(lang)
    lines = []
    if es:
        lines.append(f"Estas son nuestras {len(items)} mejores opción(es) (precios *desde*):")
        for r in items:
            lines.append(f"• {r.get('name')} — {r.get('capacity_max','?')} pax — USD {r.get('price_from_usd','?')}/{unit}\n  {r.get('url')}")
        lines.append("La *disponibilidad final* la confirma nuestro equipo de *ventas* antes de reservar. ¿Quieres que te conecte con ventas para confirmación y cotización final?")
    else:
        lines.append(f"Here are the top {len(items)} option(s) (*prices from*):")
        for r in items:
            lines.append(f"• {r.get('name')} — {r.get('capacity_max','?')} guests — USD {r.get('price_from_usd','?')}/{unit}\n  {r.get('url')}")
        lines.append("Final *availability* is confirmed by our *sales* team before booking. Connect with sales?")
    return "\n".join(lines)

def add_another_or_sales(lang: str):
    return ("¿Quieres *cotizar otro servicio* además de este?\n• *Añadir otro servicio*\n• *Conectar con ventas*"
            if is_es(lang) else
            "Would you like to *quote another service* as well?\n• *Add another service*\n• *Connect with sales*")

def handoff_client(lang: str):
    return ("Te conecto con el *Equipo de Ventas TwoTravel* para confirmar *disponibilidad* y cerrar la *reserva*."
            if is_es(lang) else
            "Connecting you with the *TwoTravel Sales Team* to confirm *availability* and finalize the *booking*.")

# Validación de nombre robusta
def valid_name(fullname: str) -> bool:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return len(tokens) >= 2

def normalize_name(fullname: str) -> str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return " ".join(tokens[:3]).title()

# ==============================================================================
#                                    FastAPI
# ==============================================================================
@app.on_event("startup")
async def show_routes():
    print("BOOT> Routes:", [r.path for r in app.router.routes])
    print("BOOT> WA_PHONE_ID:", repr(WA_PHONE_ID))
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

@app.get("/")
def root():
    return {"ok": True, "routes": [r.path for r in app.router.routes]}

# --- Verify webhook (GET)
@app.get("/wa-webhook")
async def verify(req: Request):
    mode      = req.query_params.get("hub.mode")
    token     = req.query_params.get("hub.verify_token")
    challenge = req.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("forbidden", status_code=403)

# --- Incoming (POST)
@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", json.dumps(data, ensure_ascii=False)[:600])

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Ignorar estados de entrega
            if value.get("statuses"):
                continue

            for m in value.get("messages", []):
                user = m.get("from")
                if not user:
                    continue

                # Bienvenida automática ante PRIMER mensaje
                if user not in SESSIONS:
                    SESSIONS[user] = {"step": "lang", "lang": "ES"}
                    wa_send_text(user, opener_bi())
                    continue

                text  = extract_text(m)
                state = SESSIONS[user]

                # 0) Selección de idioma
                if state["step"] == "lang":
                    low = (text or "").strip().lower()
                    if low in ("es","español"):
                        state["lang"] = "ES"
                    elif low in ("en","english"):
                        state["lang"] = "EN"
                    else:
                        wa_send_text(user, opener_bi())
                        continue
                    state["step"] = "contact_name"
                    wa_send_text(user, ("Para enviarte opciones y una cotización personalizada, necesito tus datos:\n"
                                        "📛 *Nombre completo:*\n"
                                        " _(Luego te pido el correo)_" )
                                 if is_es(state["lang"]) else
                                 ("To share options and a personalized quote, I’ll need your details:\n"
                                  "📛 *Full name:*\n"
                                  " _(I’ll ask your email next)_"))
                    continue

                # 1) Captura de nombre
                if state["step"] == "contact_name":
                    if not valid_name(text):
                        wa_send_text(user, ask_name_again(state["lang"]))
                        continue
                    state["name"] = normalize_name(text)
                    state["step"] = "contact_email"
                    wa_send_text(user, "📧 *Correo electrónico:*" if is_es(state["lang"]) else "📧 *Email address:*")
                    continue

                # 2) Captura de email + HubSpot Contact
                if state["step"] == "contact_email":
                    if not EMAIL_RE.match(text or ""):
                        wa_send_text(user, ask_email_again(state["lang"]))
                        continue
                    state["email"] = (text or "").strip()
                    # Upsert Contact (no bloquea el flujo si falla)
                    try:
                        state["hs_contact_id"] = hs_upsert_contact(state.get("name"), state.get("email"), user, state.get("lang"))
                    except Exception as e:
                        print("HubSpot upsert exception:", e)
                    state["step"] = "menu"
                    wa_send_text(user, main_menu(state["lang"]))
                    continue

                # 3) Menú principal
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
                        state["step"] = "villas_city"
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
                    if any(k in t for k in ("venta","ventas","sales","talk to sales","hablar con ventas","conectar con ventas")):
                        state["step"] = "handoff"
                        wa_send_text(user, handoff_client(state["lang"]))
                        # Creación de Deal básico aunque sea solo handoff
                        try:
                            if state.get("hs_contact_id"):
                                hs_create_deal(state["hs_contact_id"], state.get("service_type") or "general",
                                               state.get("city") or "", state.get("dates") or "", int(state.get("pax") or 0),
                                               state.get("lang"))
                        except Exception as e:
                            print("HubSpot handoff deal exception:", e)
                        continue
                    wa_send_text(user, main_menu(state["lang"]))
                    continue

                # ----------------- VILLAS / ISLAS -----------------
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

                    if not GOOGLE_SHEET_CSV_URL:
                        wa_send_text(user, "⚠️ Aún no tengo el catálogo conectado. Te conecto con *ventas* para una cotización personalizada.")
                        state["step"] = "post_results"
                        continue

                    svc = "villas" if state.get("service_type") in ("villas","islands","islas") else state.get("service_type")
                    top = find_top(svc or "villas", (state.get("city") or ""), int(state.get("pax") or 0), (state.get("prefs") or ""), TOP_K)
                    unit = "noche" if is_es(state["lang"]) else "night"
                    wa_send_text(user, reply_topN(state["lang"], top, unit=unit))

                    # Crear Deal en HubSpot con el contexto recogido
                    try:
                        if state.get("hs_contact_id"):
                            hs_create_deal(state["hs_contact_id"], svc or "villas",
                                           state.get("city") or "", state.get("dates") or "",
                                           int(state.get("pax") or 0), state.get("lang"))
                    except Exception as e:
                        print("HubSpot villas deal exception:", e)

                    state["step"] = "post_results"
                    continue

                # ----------------- BOATS -----------------
                if state["step"] == "boats_city":
                    state["city"] = (text or "Cartagena")
                    state["step"] = "boats_date"
                    wa_send_text(user, q_boats_date(state["lang"]))
                    continue

                if state["step"] == "boats_date":
                    state["dates"] = (text or "")
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
                    state["prefs"] = (text or "")

                    if not GOOGLE_SHEET_CSV_URL:
                        wa_send_text(user, "⚠️ Aún no tengo el catálogo conectado. Te conecto con *ventas* para una cotización personalizada.")
                        state["step"] = "post_results"
                        continue

                    top = find_top("boats", (state.get("city") or "cartagena"),
                                   int(state.get("pax") or 0), (state.get("prefs") or ""), TOP_K)
                    unit = "día" if is_es(state["lang"]) else "day"
                    wa_send_text(user, reply_topN(state["lang"], top, unit=unit))

                    try:
                        if state.get("hs_contact_id"):
                            hs_create_deal(state["hs_contact_id"], "boats",
                                           state.get("city") or "", state.get("dates") or "",
                                           int(state.get("pax") or 0), state.get("lang"))
                    except Exception as e:
                        print("HubSpot boats deal exception:", e)

                    state["step"] = "post_results"
                    continue

                # ----------------- WEDDINGS -----------------
                if state["step"] == "weddings_form":
                    state["weddings_info"] = (text or "")
                    wa_send_text(user, r_wed_estimate(state["lang"]))

                    try:
                        if state.get("hs_contact_id"):
                            hs_create_deal(state["hs_contact_id"], "weddings",
                                           state.get("city") or "", state.get("dates") or "",
                                           int(state.get("pax") or 0), state.get("lang"))
                    except Exception as e:
                        print("HubSpot weddings deal exception:", e)

                    state["step"] = "post_results"
                    continue

                # ----------------- CONCIERGE -----------------
                if state["step"] == "concierge_form":
                    state["concierge_info"] = (text or "")
                    wa_send_text(user, r_concierge(state["lang"]))

                    try:
                        if state.get("hs_contact_id"):
                            hs_create_deal(state["hs_contact_id"], "concierge",
                                           state.get("city") or "", state.get("dates") or "",
                                           int(state.get("pax") or 0), state.get("lang"))
                    except Exception as e:
                        print("HubSpot concierge deal exception:", e)

                    state["step"] = "post_results"
                    continue

                # ----------------- POST-RESULTS -----------------
                if state["step"] == "post_results":
                    t = (text or "").lower()
                    if ("otro" in t) or ("add" in t) or ("another" in t):
                        state["step"] = "menu"
                        wa_send_text(user, main_menu(state["lang"]))
                        continue
                    if ("venta" in t) or ("sales" in t) or ("conectar" in t) or ("connect" in t):
                        state["step"] = "handoff"
                        wa_send_text(user, handoff_client(state["lang"]))
                        continue
                    wa_send_text(user, add_another_or_sales(state["lang"]))
                    continue

    return {"ok": True}
