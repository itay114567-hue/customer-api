from fastapi import FastAPI, HTTPException, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
import requests
import os
import time
import base64
import json
import email as email_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── Deduplication cache ──────────────────────────────────────
_escalation_cache: dict = {}
COOLDOWN_SECONDS = 60

app = FastAPI(title="Customer Data API - Fireberry", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Fireberry config ────────────────────────────────────────
FIREBERRY_TOKEN = os.getenv("FIREBERRY_TOKEN")
FIREBERRY_BASE  = "https://api.fireberry.com/api"

# ─── Twilio config ───────────────────────────────────────────
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUM = os.getenv("TWILIO_WHATSAPP_NUM", "whatsapp:+14155238886")  # Sandbox default

# ─── CrewAI config ───────────────────────────────────────────
CREWAI_API_KEY      = os.getenv("CREWAI_API_KEY", "")
CREWAI_CREW_ID      = os.getenv("CREWAI_CREW_ID", "")        # אם יש deployment
RAILWAY_URL         = os.getenv("RAILWAY_URL", "")           # ה-URL של השרת הנוכחי

# ─── Gmail config ────────────────────────────────────────────
GMAIL_CLIENT_ID     = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN", "")
GMAIL_USER_EMAIL    = os.getenv("GMAIL_USER_EMAIL", "")      # כתובת Gmail שלך

# ════════════════════════════════════════════════════════════
#  FIREBERRY HELPERS
# ════════════════════════════════════════════════════════════

def fb_headers():
    return {"tokenid": FIREBERRY_TOKEN, "Content-Type": "application/json"}

def fb_get(path: str, params: dict = {}):
    url = f"{FIREBERRY_BASE}/{path}"
    res = requests.get(url, headers=fb_headers(), params=params, timeout=10)
    if res.status_code == 429:
        raise HTTPException(status_code=429, detail="Fireberry rate limit")
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

def normalize_phone(phone: str) -> str:
    return phone.replace("-", "").replace(" ", "").strip()

def map_customer(raw: dict) -> dict:
    return {
        "found": True,
        "source": "fireberry",
        "data": {
            "customer_name":  raw.get("accountname", ""),
            "phone":          raw.get("telephone1") or raw.get("telephone2") or "",
            "email":          raw.get("emailaddress1") or raw.get("emailaddress2") or "",
            "customer_id":    raw.get("accountid", ""),
            "status":         raw.get("status", ""),
            "account_type":   raw.get("accounttype", ""),
            "source_channel": raw.get("originatinglead", ""),
            "assigned_agent": raw.get("ownername", ""),
            "notes":          raw.get("description") or raw.get("needs") or "",
        }
    }

def map_order(raw: dict) -> dict:
    return {
        "found": True,
        "source": "fireberry",
        "data": {
            "order_number":      raw.get("opportunityid", ""),
            "customer_name":     raw.get("accountname", ""),
            "product":           raw.get("name", ""),
            "status":            raw.get("status", ""),
            "price":             raw.get("totalamount") or "",
            "order_date":        raw.get("createdon", ""),
            "expected_delivery": raw.get("estimatedclosedate") or "",
            "notes":             raw.get("description") or "",
        }
    }

# ════════════════════════════════════════════════════════════
#  WHATSAPP / TWILIO HELPERS
# ════════════════════════════════════════════════════════════

def send_whatsapp(to: str, message: str) -> bool:
    """שליחת הודעת WhatsApp דרך Twilio"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("ERROR: Twilio credentials not set")
        return False
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        res = requests.post(
            url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={
                "From": TWILIO_WHATSAPP_NUM,
                "To":   to if to.startswith("whatsapp:") else f"whatsapp:{to}",
                "Body": message,
            },
            timeout=15,
        )
        result = res.json()
        print(f"Twilio send → sid={result.get('sid')} status={result.get('status')}")
        return res.status_code in (200, 201)
    except Exception as e:
        print(f"Twilio send error: {e}")
        return False


def handle_whatsapp_message(sender: str, message_body: str):
    """
    לוגיקה מרכזית לטיפול בהודעת WhatsApp נכנסת.
    כאן תוכלי לחבר את CrewAI בעתיד.
    """
    print(f"📱 WhatsApp from {sender}: {message_body}")

    # --- שלב 1: נסי לזהות לקוח לפי מספר טלפון ---
    phone = sender.replace("whatsapp:", "").replace("+972", "0").strip()
    customer = None
    try:
        norm = normalize_phone(phone)
        data = fb_get("record/account", {
            "fields": "accountid,accountname,emailaddress1,telephone1,telephone2,status",
            "page_size": "50",
        })
        records = data.get("data", {}).get("Records", [])
        for r in records:
            t1 = normalize_phone(r.get("telephone1") or "")
            t2 = normalize_phone(r.get("telephone2") or "")
            if t1 == norm or t2 == norm:
                customer = r
                break
    except Exception as e:
        print(f"Customer lookup error: {e}")

    customer_name = customer.get("accountname", "לקוח") if customer else "לקוח"

    # --- שלב 2: זיהוי intent בסיסי (להחליף עם CrewAI) ---
    body_lower = message_body.lower()
    if any(w in body_lower for w in ["ביטול", "החזר", "כסף", "refund", "cancel"]):
        intent = "Refund+Request"
        reply = f"שלום {customer_name} 👋\nקיבלנו את פנייתך בנושא ביטול/החזר. נציג יחזור אליך בהקדם."
    elif any(w in body_lower for w in ["תקלה", "בעיה", "problem", "issue", "לא עובד"]):
        intent = "Complaint"
        reply = f"שלום {customer_name} 👋\nמצטערים לשמוע על הבעיה. פנייתך נרשמה ונטפל בה בהקדם."
    elif any(w in body_lower for w in ["נציג", "agent", "אדם", "human", "עזרה"]):
        intent = "Human+Agent+Request"
        reply = f"שלום {customer_name} 👋\nמעביר אותך לנציג אנושי. זמן המתנה משוער: 5-10 דקות."
    else:
        intent = "Unknown+Query"
        reply = f"שלום {customer_name} 👋\nתודה על פנייתך! קיבלנו את הודעתך ונחזור אליך בהקדם 🙏"

    # --- שלב 3: יצירת טיקט ב-Fireberry אם יש לקוח מזוהה ---
    if customer and customer.get("accountid"):
        try:
            cache_key = f"{customer['accountid']}:{intent}"
            now = time.time()
            if now - _escalation_cache.get(cache_key, 0) > COOLDOWN_SECONDS:
                _escalation_cache[cache_key] = now
                fb_post("record/Cases", {
                    "title":        f"WhatsApp - {intent.replace('+', ' ')}",
                    "description":  message_body,
                    "accountid":    customer["accountid"],
                    "casetypecode": 1,
                    "prioritycode": 2,
                    "statuscode":   1,
                })
                print(f"✅ Ticket created for {customer_name}")
        except Exception as e:
            print(f"Ticket creation error: {e}")

    # --- שלב 4: שליחת תגובה ---
    send_whatsapp(sender, reply)


# ════════════════════════════════════════════════════════════
#  GMAIL HELPERS
# ════════════════════════════════════════════════════════════

def get_gmail_access_token() -> str:
    """קבלת Access Token מ-Refresh Token"""
    if not GMAIL_REFRESH_TOKEN or not GMAIL_CLIENT_ID or not GMAIL_CLIENT_SECRET:
        raise Exception("Gmail credentials not configured")
    res = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": GMAIL_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }, timeout=10)
    data = res.json()
    if "access_token" not in data:
        raise Exception(f"Gmail token error: {data}")
    return data["access_token"]


def send_gmail(to: str, subject: str, body: str) -> bool:
    """שליחת אימייל דרך Gmail API"""
    try:
        token = get_gmail_access_token()
        msg = MIMEMultipart()
        msg["To"]      = to
        msg["From"]    = GMAIL_USER_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        res = requests.post(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"raw": raw},
            timeout=15,
        )
        print(f"Gmail send → status={res.status_code}")
        return res.status_code == 200
    except Exception as e:
        print(f"Gmail send error: {e}")
        return False


def get_unread_emails(max_results: int = 10) -> list:
    """שליפת אימיילים לא נקראים"""
    try:
        token = get_gmail_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        # רשימת מזהי הודעות לא נקראות
        res = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={"q": "is:unread", "maxResults": max_results},
            timeout=10,
        )
        msg_ids = [m["id"] for m in res.json().get("messages", [])]

        emails = []
        for msg_id in msg_ids:
            r = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                timeout=10,
            )
            payload = r.json()
            hdrs = {h["name"]: h["value"] for h in payload.get("payload", {}).get("headers", [])}
            emails.append({
                "id":      msg_id,
                "from":    hdrs.get("From", ""),
                "subject": hdrs.get("Subject", ""),
                "date":    hdrs.get("Date", ""),
                "snippet": payload.get("snippet", ""),
            })
        return emails
    except Exception as e:
        print(f"Gmail fetch error: {e}")
        return []


def mark_email_read(message_id: str) -> bool:
    try:
        token = get_gmail_access_token()
        res = requests.post(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/modify",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"removeLabelIds": ["UNREAD"]},
            timeout=10,
        )
        return res.status_code == 200
    except Exception as e:
        print(f"Mark read error: {e}")
        return False


# ════════════════════════════════════════════════════════════
#  ENDPOINTS — HEALTH
# ════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {"message": "Customer Data API v3 - Fireberry + WhatsApp + Gmail", "version": "3.0.0"}

@app.get("/health")
def health():
    status = {}

    # Fireberry
    if not FIREBERRY_TOKEN:
        status["fireberry"] = "❌ token missing"
    else:
        try:
            fb_get("record/account", {"page_size": "1"})
            status["fireberry"] = "✅ connected"
        except Exception as e:
            status["fireberry"] = f"❌ {e}"

    # Twilio
    status["twilio"] = "✅ configured" if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else "❌ missing credentials"

    # Gmail
    if GMAIL_REFRESH_TOKEN and GMAIL_CLIENT_ID:
        try:
            get_gmail_access_token()
            status["gmail"] = "✅ connected"
        except Exception as e:
            status["gmail"] = f"❌ {e}"
    else:
        status["gmail"] = "❌ missing credentials"

    return {"status": status}


# ════════════════════════════════════════════════════════════
#  ENDPOINTS — WHATSAPP WEBHOOK
# ════════════════════════════════════════════════════════════

@app.post("/webhook/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(
    background_tasks: BackgroundTasks,
    Body: str = Form(default=""),
    From: str = Form(default=""),
    To:   str = Form(default=""),
    MessageSid: str = Form(default=""),
):
    """
    Twilio שולח POST לכאן בכל הודעת WhatsApp נכנסת.
    נגדיר את ה-URL הזה ב-Twilio Sandbox/Console.
    """
    print(f"📨 Webhook received: From={From} Body={Body[:100]}")

    if not Body or not From:
        return ""

    # מטפלים ברקע כדי לא לעכב את Twilio (חייב להגיב תוך 15 שניות)
    background_tasks.add_task(handle_whatsapp_message, From, Body)

    # Twilio מצפה ל-TwiML ריק כתגובה מיידית
    return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@app.get("/webhook/whatsapp")
async def whatsapp_webhook_verify():
    """Twilio לפעמים שולח GET לאימות — מחזירים 200"""
    return {"status": "ok", "service": "whatsapp webhook"}


@app.post("/send/whatsapp")
def send_whatsapp_manual(body: dict):
    """
    שליחה ידנית של הודעת WhatsApp (לטסטים).
    POST /send/whatsapp
    {"to": "+972541234567", "message": "שלום!"}
    """
    to  = body.get("to", "")
    msg = body.get("message", "")
    if not to or not msg:
        raise HTTPException(status_code=400, detail="to and message are required")
    success = send_whatsapp(to, msg)
    return {"success": success, "to": to}


# ════════════════════════════════════════════════════════════
#  ENDPOINTS — GMAIL
# ════════════════════════════════════════════════════════════

@app.get("/gmail/unread")
def gmail_unread(limit: int = 10):
    """מחזיר רשימת אימיילים לא נקראים"""
    emails = get_unread_emails(max_results=limit)
    return {"count": len(emails), "emails": emails}


@app.post("/send/gmail")
def send_gmail_endpoint(body: dict):
    """
    שליחת אימייל דרך Gmail.
    POST /send/gmail
    {"to": "customer@example.com", "subject": "...", "body": "..."}
    """
    to      = body.get("to", "")
    subject = body.get("subject", "")
    text    = body.get("body", "")
    if not to or not subject or not text:
        raise HTTPException(status_code=400, detail="to, subject and body are required")
    success = send_gmail(to, subject, text)
    return {"success": success, "to": to}


@app.post("/gmail/process-unread")
def process_unread_emails(background_tasks: BackgroundTasks):
    """
    עיבוד כל האימיילים הלא נקראים:
    - מזהה לקוח לפי כתובת מייל
    - יוצר טיקט ב-Fireberry
    - שולח תגובה אוטומטית
    """
    emails = get_unread_emails(max_results=20)
    processed = []

    for em in emails:
        sender_email = em["from"]
        # נקה את כתובת המייל מ-"Name <email>" format
        if "<" in sender_email:
            sender_email = sender_email.split("<")[1].rstrip(">").strip()

        # חיפוש לקוח ב-Fireberry לפי מייל
        customer = None
        try:
            data = fb_get("record/account", {
                "fields": "accountid,accountname,emailaddress1,emailaddress2",
                "page_size": "50",
            })
            records = data.get("data", {}).get("Records", [])
            for r in records:
                if (r.get("emailaddress1", "").lower() == sender_email.lower() or
                        r.get("emailaddress2", "").lower() == sender_email.lower()):
                    customer = r
                    break
        except Exception as e:
            print(f"Email customer lookup error: {e}")

        ticket_id = None
        if customer and customer.get("accountid"):
            try:
                result = fb_post("record/Cases", {
                    "title":        f"Gmail - {em['subject'][:80]}",
                    "description":  em["snippet"],
                    "accountid":    customer["accountid"],
                    "casetypecode": 2,
                    "prioritycode": 1,
                    "statuscode":   1,
                })
                ticket_id = result.get("data", {}).get("casesid", "")
            except Exception as e:
                print(f"Gmail ticket error: {e}")

        # סמן כנקרא
        mark_email_read(em["id"])

        processed.append({
            "email_id":     em["id"],
            "from":         em["from"],
            "subject":      em["subject"],
            "customer":     customer.get("accountname") if customer else None,
            "ticket_id":    ticket_id,
        })

    return {"processed": len(processed), "details": processed}


# ════════════════════════════════════════════════════════════
#  ENDPOINTS — FIREBERRY (existing, unchanged)
# ════════════════════════════════════════════════════════════

@app.get("/customer/{identifier}")
def get_customer(identifier: str):
    if not FIREBERRY_TOKEN:
        raise HTTPException(status_code=500, detail="FIREBERRY_TOKEN not configured")

    identifier = identifier.strip()

    normalized = normalize_phone(identifier)
    if normalized.startswith("0") or normalized.startswith("+972"):
        try:
            data = fb_get("record/account", {
                "fields": "accountid,accountname,emailaddress1,telephone1,telephone2,statuscode,ownerid,description,status",
                "page_size": "50",
            })
            records = data.get("data", {}).get("Records", [])
            for r in records:
                t1 = normalize_phone(r.get("telephone1") or "")
                t2 = normalize_phone(r.get("telephone2") or "")
                if t1 == normalized or t2 == normalized:
                    return map_customer(r)
        except Exception as e:
            print(f"Phone search error: {e}")

    if identifier.upper().startswith("ORD-") or identifier.isdigit():
        try:
            data = fb_get("record/opportunity", {
                "fields": "opportunityid,name,accountname,statuscode,totalamount,createdon,estimatedclosedate,description",
                "page_size": "50",
            })
            records = data.get("data", {}).get("Records", [])
            for r in records:
                if r.get("name", "") == identifier:
                    return map_order(r)
        except Exception:
            pass

    if "@" in identifier:
        try:
            data = fb_get("record/account", {
                "fields": "accountid,accountname,emailaddress1,telephone1,statuscode,ownerid,description,status",
                "page_size": "50",
            })
            records = data.get("data", {}).get("Records", [])
            identifier_lower = identifier.lower()
            for r in records:
                e1 = (r.get("emailaddress1") or "").lower()
                e2 = (r.get("emailaddress2") or "").lower()
                if e1 == identifier_lower or e2 == identifier_lower:
                    return map_customer(r)
        except Exception:
            pass

    return {"found": False, "message": f"No record found for: {identifier}"}


@app.get("/escalate")
def escalate(
    customer_id: str = "",
    intent: str = "",
    description: str = "",
    channel: str = "whatsapp",
    priority: str = "high"
):
    if not FIREBERRY_TOKEN:
        return {"success": False, "error": "FIREBERRY_TOKEN not configured"}
    if not customer_id:
        return {"success": False, "error": "customer_id is required"}

    cache_key = f"{customer_id}:{intent}"
    now = time.time()
    if now - _escalation_cache.get(cache_key, 0) < COOLDOWN_SECONDS:
        return {"success": True, "ticket_id": "duplicate_skipped", "message": "Duplicate skipped"}
    _escalation_cache[cache_key] = now

    try:
        intent_hebrew = {
            "Refund+Request":       "בקשת החזר כספי",
            "Complaint":            "תלונה",
            "Human+Agent+Request":  "בקשה לנציג אנושי",
            "Unknown+Query":        "פנייה לא מזוהה",
        }.get(intent, intent)

        type_map = {"Refund+Request": 3, "Complaint": 1, "Human+Agent+Request": 2, "Unknown+Query": 2}
        intent_priority = {"Refund+Request": 2, "Complaint": 2, "Human+Agent+Request": 1, "Unknown+Query": 3}

        result = fb_post("record/Cases", {
            "title":        f"פנייה - {intent_hebrew}" if intent else "פנייה חדשה",
            "description":  description,
            "accountid":    customer_id,
            "casetypecode": type_map.get(intent, 1),
            "prioritycode": intent_priority.get(intent, 1),
            "statuscode":   1,
        })
        ticket_id = result.get("data", {}).get("casesid", "") or result.get("data", {}).get("caseid", "")

        if ticket_id and channel:
            try:
                fb_post("record/note", {
                    "regardingobjectid":       ticket_id,
                    "regardingobjecttypecode": "Cases",
                    "notetext": f"Channel: {channel}\nIntent: {intent}\nMessage: {description}",
                    "subject":  "Agent escalation",
                })
            except Exception:
                pass

        return {"success": True, "ticket_id": ticket_id, "message": f"Ticket created for {customer_id}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/ticket")
def create_ticket(body: dict):
    if not FIREBERRY_TOKEN:
        raise HTTPException(status_code=500, detail="FIREBERRY_TOKEN not configured")

    priority_map = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
    result = fb_post("record/Cases", {
        "title":        body.get("subject", "Customer inquiry"),
        "description":  body.get("description", ""),
        "accountid":    body.get("customer_id", ""),
        "prioritycode": priority_map.get(body.get("priority", "medium"), 2),
        "statuscode":   1,
    })
    ticket_id = result.get("data", {}).get("casesid", "") or result.get("data", {}).get("caseid", "")

    if ticket_id:
        try:
            fb_post("record/note", {
                "regardingobjectid":       ticket_id,
                "regardingobjecttypecode": "Cases",
                "notetext": f"Channel: {body.get('channel', 'unknown')}\nIntent: {body.get('intent', 'unknown')}",
                "subject":  "Agent context",
            })
        except Exception:
            pass

    return {"success": True, "ticket_id": ticket_id}


@app.get("/customers")
def list_customers(limit: int = 10):
    data = fb_get("record/account", {
        "fields": "accountid,accountname,emailaddress1,telephone1",
        "page_size": str(limit),
    })
    records = data.get("data", {}).get("Records", [])
    return {"total": data.get("data", {}).get("Total_Records", 0), "source": "fireberry", "sample": records}

@app.get("/columns")
def get_columns():
    return {
        "customer_fields": ["customer_name", "phone", "email", "customer_id", "status", "assigned_agent", "notes"],
        "order_fields":    ["order_number", "customer_name", "product", "status", "price", "order_date", "expected_delivery", "notes"],
    }

@app.get("/debug-case/{case_id}")
def debug_case(case_id: str):
    return fb_get(f"record/Cases/{case_id}")
