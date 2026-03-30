from fastapi import FastAPI, HTTPException, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from twilio.rest import Client
import requests
import os
import time

# ─── Deduplication cache ──────────────────────────────────────
_escalation_cache: dict = {}
COOLDOWN_SECONDS = 60

app = FastAPI(title="Customer Data API - Fireberry & Twilio & CrewAI", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Environment Variables ────────────────────────────────────
FIREBERRY_TOKEN     = os.getenv("FIREBERRY_TOKEN")
FIREBERRY_BASE      = "https://api.fireberry.com/api"

TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUM = os.getenv("TWILIO_WHATSAPP_NUM", "whatsapp:+14155238886")

CREWAI_API_KEY      = os.getenv("CREWAI_API_KEY")
CREWAI_KICKOFF_URL  = os.getenv("CREWAI_KICKOFF_URL")

# ─── Fireberry Helpers ────────────────────────────────────────

def fb_headers():
    return {"tokenid": FIREBERRY_TOKEN, "Content-Type": "application/json"}

def fb_get(path: str, params: dict = {}):
    url = f"{FIREBERRY_BASE}/{path}"
    res = requests.get(url, headers=fb_headers(), params=params, timeout=10)
    if res.status_code == 429:
        raise HTTPException(status_code=429, detail="Fireberry rate limit - try again in 60s")
    data = res.json()
    if not data.get("success"):
        raise HTTPException(status_code=502, detail=f"Fireberry error: {data.get('message')}")
    return data

def fb_post(path: str, body: dict):
    url = f"{FIREBERRY_BASE}/{path}"
    res = requests.post(url, headers=fb_headers(), json=body, timeout=10)
    data = res.json()
    print(f"FB POST {path} → status={res.status_code} body={data}")
    if not data.get("success"):
        raise HTTPException(status_code=502, detail=f"Fireberry error: {data.get('message')} | full: {data}")
    return data

def fb_patch(path: str, body: dict):
    url = f"{FIREBERRY_BASE}/{path}"
    res = requests.patch(url, headers=fb_headers(), json=body, timeout=10)
    data = res.json()
    print(f"FB PATCH {path} → status={res.status_code} body={data}")
    return data

def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = "".join(filter(str.isdigit, phone))
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    return digits

# ─── Twilio Helper ────────────────────────────────────────────

def send_whatsapp(to_number: str, message_body: str):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    clean = to_number.replace("whatsapp:", "").strip()
    formatted = f"whatsapp:{clean}"
    msg = client.messages.create(
        from_=TWILIO_WHATSAPP_NUM,
        body=message_body,
        to=formatted,
    )
    return msg.sid

# ─── Intent → Fireberry mappings ─────────────────────────────

INTENT_HEBREW = {
    "Refund_Request":       "בקשת החזר כספי",
    "Refund+Request":       "בקשת החזר כספי",
    "Complaint":            "תלונה",
    "Human_Agent_Request":  "בקשה לנציג אנושי",
    "Human+Agent+Request":  "בקשה לנציג אנושי",
    "Status_Check":         "בדיקת סטטוס הזמנה",
    "Unknown_Query":        "פנייה לא מזוהה",
    "Unknown+Query":        "פנייה לא מזוהה",
}

# casetypecode: 1=בעיה, 2=שאלה, 3=בקשה, 4=תקלה
INTENT_TYPE = {
    "Refund_Request":       3,   # בקשה
    "Refund+Request":       3,
    "Complaint":            1,   # בעיה
    "Human_Agent_Request":  2,   # שאלה
    "Human+Agent+Request":  2,
    "Status_Check":         2,   # שאלה
    "Unknown_Query":        2,
    "Unknown+Query":        2,
}

# prioritycode: 1=רגילה, 2=גבוהה, 3=נמוכה
INTENT_PRIORITY = {
    "Refund_Request":       2,   # גבוהה
    "Refund+Request":       2,
    "Complaint":            2,   # גבוהה
    "Human_Agent_Request":  1,   # רגילה
    "Human+Agent+Request":  1,
    "Status_Check":         3,   # נמוכה
    "Unknown_Query":        3,
    "Unknown+Query":        3,
}

# ─── Ticket creator (shared logic) ───────────────────────────

def _create_fireberry_ticket(
    customer_id: str,
    intent: str,
    description: str,
    channel: str = "whatsapp",
    auto_close: bool = False,   # True = הפנייה נסגרה אוטומטית
) -> dict:
    """
    יוצר טיקט ב-Fireberry.
    auto_close=True  → statuscode=2 (סגור)
    auto_close=False → statuscode=1 (פתוח)
    """
    intent_heb  = INTENT_HEBREW.get(intent, intent)
    case_type   = INTENT_TYPE.get(intent, 1)
    priority    = INTENT_PRIORITY.get(intent, 1)
    status_code = 2 if auto_close else 1

    title = f"פנייה - {intent_heb}" if intent else "פנייה חדשה"

    payload = {
        "title":        title,
        "description":  description,
        "accountid":    customer_id,
        "casetypecode": case_type,
        "prioritycode": priority,
        "statuscode":   status_code,
    }
    result    = fb_post("record/Cases", payload)
    ticket_id = (
        result.get("data", {}).get("casesid")
        or result.get("data", {}).get("caseid")
        or result.get("data", {}).get("id")
        or ""
    )

    # ─── Add note with channel / intent context ───────────────
    if ticket_id:
        try:
            note_text = (
                f"Channel: {channel}\n"
                f"Intent: {intent}\n"
                f"Auto-closed: {auto_close}\n\n"
                f"Message / Summary:\n{description}"
            )
            fb_post("record/note", {
                "regardingobjectid":       ticket_id,
                "regardingobjecttypecode": "Cases",
                "notetext": note_text,
                "subject":  "Agent escalation",
            })
        except Exception as e:
            print(f"Note creation failed (non-fatal): {e}")

    return {
        "ticket_id":  ticket_id,
        "status":     "closed" if auto_close else "open",
        "priority":   priority,
        "case_type":  case_type,
    }

# ════════════════════════════════════════════════════════════
#  ENDPOINTS
# ════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {"status": "online", "version": "4.0.0"}

@app.get("/health")
def health():
    if not FIREBERRY_TOKEN:
        return {"status": "error", "detail": "FIREBERRY_TOKEN not set"}
    try:
        fb_get("record/account", {"page_size": "1"})
        return {"status": "ok", "crm": "fireberry"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# ─── Customer lookup ──────────────────────────────────────────

@app.get("/customer/{identifier}", response_class=PlainTextResponse)
def get_customer(identifier: str):
    """
    שולף נתוני לקוח מ-Fireberry לפי טלפון / מייל / מספר הזמנה.
    מחזיר plain-text שמתאים לקריאה מסוכן CrewAI.
    """
    if not FIREBERRY_TOKEN:
        return "Result: FIREBERRY_TOKEN not configured"

    identifier = identifier.strip()
    norm       = normalize_phone(identifier)

    fields = "accountid,accountname,telephone1,telephone2,emailaddress1,emailaddress2,status"

    # 1. חיפוש לפי טלפון
    if norm.startswith("0") or norm.startswith("972"):
        try:
            data    = fb_get("record/account", {"fields": fields, "page_size": "100"})
            records = data.get("data", {}).get("Records", [])
            print(f"DEBUG phone search: norm={norm}, records={len(records)}")
            for r in records:
                t1 = normalize_phone(r.get("telephone1") or "")
                t2 = normalize_phone(r.get("telephone2") or "")
                if t1 == norm or t2 == norm:
                    email = r.get("emailaddress1") or r.get("emailaddress2") or "Not provided"
                    return (
                        f"customer_id: {r.get('accountid')}\n"
                        f"customer_name: {r.get('accountname')}\n"
                        f"phone: {r.get('telephone1')}\n"
                        f"email: {email}\n"
                        f"status: {r.get('status')}\n"
                        f"data_found: true"
                    )
        except Exception as e:
            print(f"Phone search error: {e}")

    # 2. חיפוש לפי מייל
    if "@" in identifier:
        try:
            data    = fb_get("record/account", {"fields": fields, "page_size": "100"})
            records = data.get("data", {}).get("Records", [])
            id_low  = identifier.lower()
            for r in records:
                e1 = (r.get("emailaddress1") or "").lower()
                e2 = (r.get("emailaddress2") or "").lower()
                if e1 == id_low or e2 == id_low:
                    return (
                        f"customer_id: {r.get('accountid')}\n"
                        f"customer_name: {r.get('accountname')}\n"
                        f"phone: {r.get('telephone1')}\n"
                        f"email: {r.get('emailaddress1')}\n"
                        f"status: {r.get('status')}\n"
                        f"data_found: true"
                    )
        except Exception as e:
            print(f"Email search error: {e}")

    # 3. חיפוש לפי מספר הזמנה
    try:
        data    = fb_get("record/opportunity", {
            "fields": "opportunityid,name,accountname,statuscode,totalamount,createdon,estimatedclosedate,description",
            "page_size": "50",
        })
        records = data.get("data", {}).get("Records", [])
        for r in records:
            if r.get("name", "") == identifier:
                return (
                    f"order_number: {r.get('opportunityid')}\n"
                    f"customer_name: {r.get('accountname')}\n"
                    f"product: {r.get('name')}\n"
                    f"status: {r.get('statuscode')}\n"
                    f"price: {r.get('totalamount')}\n"
                    f"order_date: {r.get('createdon')}\n"
                    f"expected_delivery: {r.get('estimatedclosedate')}\n"
                    f"data_found: true"
                )
    except Exception as e:
        print(f"Order search error: {e}")

    return f"Result: No customer found for {identifier}"

# ─── Escalate (open ticket, stays open for human) ────────────

@app.get("/escalate", response_class=PlainTextResponse)
def escalate(
    customer_id: str = "",
    intent: str      = "Unknown",
    description: str = "",
    channel: str     = "whatsapp",
):
    """
    פותח טיקט פתוח ב-Fireberry — נדרש מענה אנושי.
    """
    if not FIREBERRY_TOKEN:
        return "Error: FIREBERRY_TOKEN not configured"
    if not customer_id:
        return "Error: customer_id is required"

    cache_key = f"{customer_id}:{intent}"
    if time.time() - _escalation_cache.get(cache_key, 0) < COOLDOWN_SECONDS:
        return "Duplicate skipped."
    _escalation_cache[cache_key] = time.time()

    try:
        result = _create_fireberry_ticket(
            customer_id=customer_id,
            intent=intent,
            description=description,
            channel=channel,
            auto_close=False,
        )
        return f"Success: Ticket ID {result['ticket_id']} | Status: open | Priority: {result['priority']}"
    except Exception as e:
        return f"Error: {str(e)}"

# ─── Auto-close ticket (resolved by agents) ──────────────────

@app.get("/close_ticket", response_class=PlainTextResponse)
def close_ticket(
    customer_id: str = "",
    intent: str      = "Unknown",
    description: str = "",
    channel: str     = "whatsapp",
):
    """
    פותח טיקט סגור ב-Fireberry — הפנייה נפתרה אוטומטית ע"י הסוכנים.
    description אמור להכיל סיכום השיחה.
    """
    if not FIREBERRY_TOKEN:
        return "Error: FIREBERRY_TOKEN not configured"
    if not customer_id:
        return "Error: customer_id is required"

    try:
        result = _create_fireberry_ticket(
            customer_id=customer_id,
            intent=intent,
            description=description,
            channel=channel,
            auto_close=True,
        )
        return f"Success: Ticket ID {result['ticket_id']} | Status: closed"
    except Exception as e:
        return f"Error: {str(e)}"

# ─── Send WhatsApp via Twilio ─────────────────────────────────

@app.get("/send_response", response_class=PlainTextResponse)
def api_send_response(phone: str, message: str):
    """
    שולח הודעת WhatsApp ללקוח דרך Twilio.
    קרוי ע"י סוכן CrewAI לאחר שגיבש תשובה.
    """
    try:
        sid = send_whatsapp(phone, message)
        return f"Message Sent. SID: {sid}"
    except Exception as e:
        return f"Failed: {str(e)}"

# ─── Webhook (incoming WhatsApp → CrewAI) ────────────────────

@app.post("/webhook/whatsapp")
async def webhook(
    background_tasks: BackgroundTasks,
    Body: str = Form(...),
    From: str = Form(...),
):
    """
    מקבל הודעת WhatsApp נכנסת מ-Twilio.
    מפעיל CrewAI crew ברקע עם מספר הטלפון ותוכן ההודעה.
    """
    print(f"📨 NEW MESSAGE | From: {From} | Body: {Body}")

    if CREWAI_KICKOFF_URL and CREWAI_API_KEY:
        def start_crew():
            clean_phone = From.replace("whatsapp:", "")
            payload = {
                "inputs": {
                    "customer_input":          Body,
                    "order_number_or_phone":   clean_phone,
                    "customer_phone":          clean_phone,   # extra alias
                }
            }
            token   = CREWAI_API_KEY if CREWAI_API_KEY.startswith("Bearer ") else f"Bearer {CREWAI_API_KEY}"
            headers = {"Authorization": token, "Content-Type": "application/json"}
            try:
                print(f"🚀 SENDING TO CREWAI: {CREWAI_KICKOFF_URL}")
                response = requests.post(CREWAI_KICKOFF_URL, json=payload, headers=headers, timeout=30)
                print(f"✅ CREWAI RESPONSE: {response.status_code} - {response.text}")
            except Exception as e:
                print(f"❌ CREWAI ERROR: {str(e)}")

        background_tasks.add_task(start_crew)

    # חייב להחזיר 200 ריק לטוויליו
    from fastapi.responses import PlainTextResponse as PR
    return PR("")

# ─── POST ticket (legacy / external) ─────────────────────────

@app.post("/ticket")
def create_ticket(body: dict):
    """
    יצירת טיקט דרך POST מגורם חיצוני.
    """
    if not FIREBERRY_TOKEN:
        raise HTTPException(status_code=500, detail="FIREBERRY_TOKEN not configured")

    auto_close = body.get("auto_close", False)
    result = _create_fireberry_ticket(
        customer_id=body.get("customer_id", ""),
        intent=body.get("intent", "Unknown"),
        description=body.get("description", body.get("subject", "")),
        channel=body.get("channel", "unknown"),
        auto_close=auto_close,
    )
    return {"success": True, **result}

# ─── Debug / Utility ──────────────────────────────────────────

@app.get("/customers")
def list_customers(limit: int = 10):
    data    = fb_get("record/account", {
        "fields":    "accountid,accountname,emailaddress1,telephone1",
        "page_size": str(limit),
    })
    records = data.get("data", {}).get("Records", [])
    return {
        "total":  data.get("data", {}).get("Total_Records", 0),
        "source": "fireberry",
        "sample": records,
    }

@app.get("/columns")
def get_columns():
    return {
        "customer_fields": ["customer_name", "phone", "email", "customer_id", "status", "assigned_agent", "notes"],
        "order_fields":    ["order_number", "customer_name", "product", "status", "price", "order_date", "expected_delivery", "notes"],
        "ticket_statuses": {"1": "open", "2": "closed"},
        "priority_codes":  {"1": "רגילה", "2": "גבוהה", "3": "נמוכה"},
        "case_types":      {"1": "בעיה", "2": "שאלה", "3": "בקשה", "4": "תקלה"},
    }

@app.get("/debug-case/{case_id}")
def debug_case(case_id: str):
    data = fb_get(f"record/Cases/{case_id}")
    return data
