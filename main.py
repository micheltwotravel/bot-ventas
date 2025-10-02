# main.py
import os, re, csv, io, requests, datetime
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse

app = FastAPI()

# ====== ENV ======
VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN") or "").strip()
WA_TOKEN     = (os.getenv("WA_ACCESS_TOKEN") or "").strip()
WA_PHONE_ID  = (os.getenv("WA_PHONE_NUMBER_ID") or "").strip()

TOP_K = int(os.getenv("TOP_K", "3"))

HUBSPOT_TOKEN   = (os.getenv("HUBSPOT_TOKEN") or "").strip()
HS_PIPELINE     = (os.getenv("HUBSPOT_DEAL_PIPELINE") or "default").strip()
HS_STAGE        = (os.getenv("HUBSPOT_DEAL_STAGE") or "appointmentscheduled").strip()

GOOGLE_SHEET_CSV_URL = (os.getenv("GOOGLE_SHEET_CSV_URL") or "").strip()

# ====== STATE (MVP) ======
SESSIONS = {}  # { phone: {...} }
OWNERS_CACHE = {}  # { email: hubspot_owner_id }

# ====== CONSTANTES UX ======
CITIES = [
    {"id":"ctg","title":"1) Cartagena"},
    {"id":"mde","title":"2) Medellín"},
    {"id":"mex","title":"3) México"},
]

CITY_OWNER_EMAIL = {
    "ctg": "sofia@two.travel",
    "mde": "ross@two.travel",
    "mex": "ray@two.travel",
}

PAX_LIST = [{"id":str(i), "title": f"{i}"} for i in range(1,11)] + [{"id":"11plus","title":"11+"}]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # YYYY-MM-DD

# ====== WHATSAPP HELPERS ======
def _wa_post(payload: dict) -> requests.Response:
    url = f"https://graph.facebook.com/v23.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print("WA >", r.status_code, r.text[:200])
    return r

def wa_text(to: str, body: str):
    _wa_post({"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}})

def wa_buttons_lang(to: str):
    _wa_post({
        "messaging_product":"whatsapp","to":to,"type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":"TWO TRAVEL\n\nChoose your language / Elige tu idioma"},
            "action":{"buttons":[
                {"type":"reply","reply":{"id":"lang_es","title":"1) ES"}},
                {"type":"reply","reply":{"id":"lang_en","title":"2) EN"}}
            ]}
        }
    })

def wa_list_menu(to: str, lang: str):
    es = lang.upper().startswith("ES")
    title = "¿Qué necesitas hoy?" if es else "What do you need today?"
    rows = [
        {"id":"svc_villas",   "title":("1) Villas & Casas" if es else "1) Villas & Homes")},
        {"id":"svc_boats",    "title":("2) Botes & Yates" if es else "2) Boats & Yachts")},
        {"id":"svc_islands",  "title":("3) Islas Privadas" if es else "3) Private Islands")},
        {"id":"svc_wedding",  "title":("4) Bodas & Eventos" if es else "4) Weddings & Events")},
        {"id":"svc_concierge","title":("5) Concierge" if es else "5) Concierge")},
        {"id":"svc_sales",    "title":("6) Hablar con ventas" if es else "6) Talk to sales")},
    ]
    _wa_post({
        "messaging_product":"whatsapp","to":to,"type":"interactive",
        "interactive":{
            "type":"list",
            "header":{"type":"text","text":"TWO TRAVEL"},
            "body":{"text":title},
            "action":{
                "button":"Ver opciones" if es else "See options",
                "sections":[{"title":"Servicios","rows":rows}]
            }
        }
    })

def wa_list_cities(to: str, lang: str):
    es = lang.upper().startswith("ES")
    _wa_post({
        "messaging_product":"whatsapp","to":to,"type":"interactive",
        "interactive":{
            "type":"list",
            "header":{"type":"text","text":"Ciudad"},
            "body":{"text":"Elige ciudad" if es else "Choose city"},
            "action":{"button":"Seleccionar" if es else "Select","sections":[{"title":"Ciudades","rows":[
                {"id":"city_ctg","title":CITIES[0]["title"]},
                {"id":"city_mde","title":CITIES[1]["title"]},
                {"id":"city_mex","title":CITIES[2]["title"]},
            ]}]}
        }
    })

def wa_list_pax(to: str, lang: str):
    es = lang.upper().startswith("ES")
    _wa_post({
        "messaging_product":"whatsapp","to":to,"type":"interactive",
        "interactive":{
            "type":"list",
            "header":{"type":"text","text":"Personas / Guests"},
            "body":{"text":"¿Para cuántas personas?" if es else "How many guests?"},
            "action":{"button":"Elegir" if es else "Choose","sections":[{"title":"PAX","rows":[
                {"id":f"pax_{r['id']}", "title":f"{i+1}) {r['title']}"} for i,r in enumerate(PAX_LIST)
            ]}]}
        }
    })

def wa_buttons_next(to: str, lang: str):
    es = lang.upper().startswith("ES")
    _wa_post({
        "messaging_product":"whatsapp","to":to,"type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":"¿Qué quieres hacer?" if es else "What would you like to do?"},
            "action":{"buttons":[
                {"type":"reply","reply":{"id":"post_add","title":("1) Añadir otro servicio" if es else "1) Add another service")}},
                {"type":"reply","reply":{"id":"post_sales","title":("2) Conectar con ventas" if es else "2) Connect with sales")}}
            ]}
        }
    })

# ====== HUBSPOT HELPERS ======
def hs_headers():
    return {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}

def hs_owner_id_by_email(email: str) -> str | None:
    if not HUBSPOT_TOKEN or not email: return None
    if email in OWNERS_CACHE: return OWNERS_CACHE[email]
    r = requests.get("https://api.hubapi.com/crm/v3/owners", headers=hs_headers(), params={"email":email}, timeout=20)
    if r.ok and r.json().get("results"):
        oid = r.json()["results"][0]["id"]
        OWNERS_CACHE[email] = oid
        return oid
    print("HS owners lookup error:", r.status_code, r.text[:200])
    return None

def hs_upsert_contact(name: str, email: str, phone: str, lang: str) -> str | None:
    if not HUBSPOT_TOKEN: return None
    base = "https://api.hubapi.com/crm/v3/objects/contacts"
    props = {
        "email": email,
        "firstname": (name.split()[0] if name else None),
        "lastname": (" ".join(name.split()[1:]) if name and len(name.split())>1 else None),
        "phone": phone,
        "lifecyclestage":"lead",
        "preferred_language": ("es" if lang.upper().startswith("ES") else "en"),
        "source":"WhatsApp Bot",
    }
    r = requests.post(base, headers=hs_headers(), json={"properties":props}, timeout=20)
    if r.status_code==201: return r.json().get("id")
    if r.status_code==409:
        s = requests.post(f"{base}/search", headers=hs_headers(), json={
            "filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}],
            "properties":["email"]
        }, timeout=20)
        if s.ok and s.json().get("results"):
            cid = s.json()["results"][0]["id"]
            requests.patch(f"{base}/{cid}", headers=hs_headers(), json={"properties":props}, timeout=20)
            return cid
    print("HS contact error:", r.status_code, r.text[:200])
    return None

def hs_create_deal(contact_id: str, data: dict) -> str | None:
    if not HUBSPOT_TOKEN: return None
    props = {
        "dealname": data["dealname"],
        "pipeline": HS_PIPELINE,
        "dealstage": HS_STAGE,
        "city__c": data.get("city_label"),        # custom free prop name example
        "service_type__c": data.get("service"),   # custom
        "trip_date__c": data.get("date"),         # custom (YYYY-MM-DD)
        "pax__c": str(data.get("pax") or ""),
    }
    # owner
    owner_email = CITY_OWNER_EMAIL.get(data.get("city"))
    owner_id = hs_owner_id_by_email(owner_email) if owner_email else None
    if owner_id:
        props["hubspot_owner_id"] = owner_id

    payload = {"properties": props}
    if contact_id:
        payload["associations"] = [{
            "to":{"id":contact_id},
            "types":[{"associationCategory":"HUBSPOT_DEFINED","associationTypeId":3}]  # 3 = deal->contact
        }]
    r = requests.post("https://api.hubapi.com/crm/v3/objects/deals", headers=hs_headers(), json=payload, timeout=20)
    if r.ok: return r.json().get("id")
    print("HS deal error:", r.status_code, r.text[:200])
    return None

# ====== CATALOGO ======
def load_catalog():
    if not GOOGLE_SHEET_CSV_URL:
        return []
    r = requests.get(GOOGLE_SHEET_CSV_URL, timeout=30)
    if not r.ok: 
        print("Catalog download error:", r.status_code, r.text[:200])
        return []
    rows=[]; reader = csv.DictReader(io.StringIO(r.content.decode("utf-8",errors="ignore")))
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k,v in row.items()})
    return rows

def find_top(city_code: str, pax: int, top_k: int=TOP_K):
    rows = load_catalog()
    code_to_city = {"ctg":"cartagena","mde":"medellín","mex":"méxico"}
    city = code_to_city.get(city_code,"").lower()
    if not rows: return []
    def ok(r):
        if r.get("service_type","").lower()!="villas": return False
        if city and (r.get("city","").lower()!=city): return False
        try: cap=int(float(r.get("capacity_max","0") or "0"))
        except: cap=0
        return cap>=pax if pax else True
    F=[r for r in rows if ok(r)]
    def price(r):
        try: return float(r.get("price_from_usd","999999") or "999999")
        except: return 999999.0
    F.sort(key=price)
    return F[:max(1,int(top_k or 1))]

# ====== COPY ======
def t(es, en, lang): return es if lang.upper().startswith("ES") else en

def welcome_text(lang):
    return t(
        "¡Hola! Soy tu concierge virtual de TWO TRAVEL 🛎️✨.\nTe ayudaré con villas, botes, islas, bodas/eventos y concierge.",
        "Hi! I’m your TWO TRAVEL virtual concierge 🛎️✨.\nI can help with villas, boats, islands, weddings/events and concierge.",
        lang,
    )

def ask_name(lang):  return t("📛 *Nombre completo:*","📛 *Full name:*",lang)
def ask_email(lang): return t("📧 *Correo electrónico:*","📧 *Email address:*",lang)
def ask_date(lang):  return t("📅 *Fecha del viaje/evento* (formato YYYY-MM-DD):","📅 *Trip/Event date* (format YYYY-MM-DD):",lang)

def reply_top(lang, items, unit):
    if not items:
        return t("No veo opciones con esos filtros. ¿Intento con otro número de personas?",
                 "I couldn’t find matches. Try different party size?",
                 lang)
    lines=[t("Top opciones (precios *desde*):","Top picks (*prices from*):",lang)]
    for r in items:
        lines.append(f"• {r.get('name')} ({r.get('capacity_max','?')} pax) — USD {r.get('price_from_usd','?')}/{unit} → {r.get('url')}")
    lines.append(t("La disponibilidad final la confirma *ventas*.","Final availability confirmed by *sales*.",lang))
    return "\n".join(lines)

def normalize_name(fullname:str)->str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']{2,}", (fullname or ""))
    return " ".join(tokens[:3]).title()

# ====== STARTUP ======
@app.on_event("startup")
async def boot():
    print("BOOT> Routes:", [r.path for r in app.router.routes])
    print("BOOT> WA_PHONE_ID:", WA_PHONE_ID)
    print("BOOT> WA_TOKEN len:", len(WA_TOKEN or ""))

# ====== HEALTH ======
@app.get("/")
def root():
    return {"ok": True}

# ====== VERIFY (GET) ======
@app.get("/wa-webhook")
async def verify(req: Request):
    mode = req.query_params.get("hub.mode")
    token = req.query_params.get("hub.verify_token")
    challenge = req.query_params.get("hub.challenge")
    if mode=="subscribe" and token==VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("forbidden", status_code=403)

# ====== INCOMING (POST) ======
@app.post("/wa-webhook")
async def incoming(req: Request):
    data = await req.json()
    print("Incoming:", data)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Ignore statuses
            if value.get("statuses"): continue

            for m in value.get("messages", []):
                user = m.get("from")
                if not user: continue

                # Start session on 1st msg
                if user not in SESSIONS:
                    SESSIONS[user] = {"step":"lang","lang":"ES"}
                    wa_text(user, welcome_text("ES"))
                    wa_buttons_lang(user)
                    continue

                # Extract text/title from interactive
                txt = ""
                ttype = (m.get("type") or "").lower()
                if ttype=="text":
                    txt = (m.get("text") or {}).get("body","").strip()
                elif ttype=="interactive":
                    inter = m.get("interactive") or {}
                    if inter.get("type")=="button_reply":
                        txt = (inter.get("button_reply") or {}).get("id","")
                    elif inter.get("type")=="list_reply":
                        txt = (inter.get("list_reply") or {}).get("id","")

                st = SESSIONS[user]

                # ---- step: lang
                if st["step"]=="lang":
                    if txt in ("lang_es","lang_en"):
                        st["lang"] = "ES" if txt=="lang_es" else "EN"
                        wa_text(user, t("Perfecto. Empecemos con tus datos.","Great. Let’s start with your details.", st["lang"]))
                        st["step"]="name"
                        wa_text(user, ask_name(st["lang"]))
                    else:
                        wa_buttons_lang(user)
                    continue

                # ---- step: name
                if st["step"]=="name":
                    if not txt:
                        wa_text(user, ask_name(st["lang"])); continue
                    nm = normalize_name(txt)
                    if len(nm.split())<2:
                        wa_text(user, t("¿Me confirmas *nombre y apellido*?","Please share *name and last name*.", st["lang"]))
                        continue
                    st["name"]=nm
                    st["step"]="email"
                    wa_text(user, ask_email(st["lang"]))
                    continue

                # ---- step: email
                if st["step"]=="email":
                    if not EMAIL_RE.match(txt or ""):
                        wa_text(user, t("Ese correo no parece válido. Inténtalo así: nombre@dominio.com",
                                        "That email looks invalid. Try like: name@example.com", st["lang"]))
                        continue
                    st["email"]=txt.strip()
                    # upsert contact (non-blocking)
                    try:
                        st["contact_id"] = hs_upsert_contact(st["name"], st["email"], user, st["lang"])
                    except Exception as e:
                        print("HS upsert contact err:", e)
                    st["step"]="menu"
                    wa_list_menu(user, st["lang"])
                    continue

                # ---- step: menu
                if st["step"]=="menu":
                    if txt in ("svc_villas","svc_islands"):   # islas usa mismo flujo de villas
                        st["service"] = "villas"
                        st["step"] = "city"
                        wa_list_cities(user, st["lang"])
                        continue
                    if txt=="svc_boats":
                        st["service"]="boats"
                        st["step"]="city"
                        wa_list_cities(user, st["lang"])
                        continue
                    if txt=="svc_wedding":
                        st["service"]="weddings"
                        st["step"]="city"
                        wa_list_cities(user, st["lang"])
                        continue
                    if txt=="svc_concierge":
                        st["service"]="concierge"
                        st["step"]="city"
                        wa_list_cities(user, st["lang"])
                        continue
                    if txt=="svc_sales":
                        st["step"]="handoff"
                        # we still need city to assign owner; ask city
                        wa_list_cities(user, st["lang"])
                        continue
                    # re-show
                    wa_list_menu(user, st["lang"])
                    continue

                # ---- step: city
                if st["step"]=="city":
                    if txt in ("city_ctg","city_mde","city_mex"):
                        st["city"] = txt.split("_")[1]  # ctg/mde/mex
                        st["city_label"] = next((c["title"][3:] for c in CITIES if c["id"]==st["city"]), st["city"])
                        st["step"]="date"
                        wa_text(user, ask_date(st["lang"]))
                    else:
                        wa_list_cities(user, st["lang"])
                    continue

                # ---- step: date (single)
                if st["step"]=="date":
                    if not DATE_RE.match(txt or ""):
                        wa_text(user, t("Formato inválido. Usa *YYYY-MM-DD* (ej: 2025-02-15).",
                                        "Invalid format. Use *YYYY-MM-DD* (e.g. 2025-02-15).", st["lang"]))
                        continue
                    st["date"]=txt
                    st["step"]="pax"
                    wa_list_pax(user, st["lang"])
                    continue

                # ---- step: pax
                if st["step"]=="pax":
                    if (txt or "").startswith("pax_"):
                        pid = txt.split("_",1)[1]
                        st["pax"] = 11 if pid=="11plus" else int(pid)
                        # Results (for villas/boats only)
                        if st.get("service") in ("villas","boats"):
                            if not GOOGLE_SHEET_CSV_URL:
                                wa_text(user, t("⚠️ Aún no tengo el catálogo conectado. Te conecto con ventas.",
                                                "⚠️ Catalog not connected yet. I’ll connect you with sales.", st["lang"]))
                            else:
                                items = find_top(st["city"], st["pax"], TOP_K)
                                unit = t("noche","night", st["lang"]) if st["service"]=="villas" else t("día","day",st["lang"])
                                wa_text(user, reply_top(st["lang"], items, unit))
                        # Offer next actions
                        st["step"]="post"
                        wa_buttons_next(user, st["lang"])
                    else:
                        wa_list_pax(user, st["lang"])
                    continue

                # ---- step: post (add or sales)
                if st["step"]=="post":
                    if txt=="post_add":
                        st["step"]="menu"
                        wa_list_menu(user, st["lang"])
                        continue
                    if txt=="post_sales":
                        st["step"]="handoff"
                        # si no hay ciudad aún, pedirla
                        if not st.get("city"):
                            wa_list_cities(user, st["lang"])
                        else:
                            # crear deal
                            try:
                                deal_id = hs_create_deal(st.get("contact_id"), {
                                    "dealname": f"{st.get('name','')} - {st.get('city_label','')} - {st.get('service','')}",
                                    "service": st.get("service"),
                                    "city": st.get("city"),
                                    "city_label": st.get("city_label"),
                                    "date": st.get("date"),
                                    "pax": st.get("pax"),
                                })
                                owner_email = CITY_OWNER_EMAIL.get(st.get("city"))
                                msg = t(
                                    f"Te conecto con *ventas* ({owner_email}) para confirmar disponibilidad y cerrar la reserva.",
                                    f"Connecting you with *sales* ({owner_email}) to confirm availability and finalize.",
                                    st["lang"]
                                )
                                wa_text(user, msg)
                            except Exception as e:
                                print("HS deal create err:", e)
                                wa_text(user, t("No pude crear el deal ahora; igual te conecto con ventas.",
                                                "Couldn't create the deal now; connecting you with sales anyway.", st["lang"]))
                        continue
                    # re-show
                    wa_buttons_next(user, st["lang"])
                    continue

                # ---- fallback
                wa_list_menu(user, st["lang"])

    return JSONResponse({"ok": True})
