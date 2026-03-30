from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
import time

# ─── Deduplication cache ──────────────────────────────────────
# מונע יצירת טיקט כפול לאותו לקוח תוך 60 שניות
_escalation_cache: dict = {}
COOLDOWN_SECONDS = 60

app = FastAPI(title="Customer Data API - Fireberry", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Fireberry config ────────────────────────────────────────
FIREBERRY_TOKEN = os.getenv("FIREBERRY_TOKEN")
FIREBERRY_BASE  = "https://api.fireberry.com/api"

def fb_headers():
    return {
        "tokenid": FIREBERRY_TOKEN,
        "Content-Type": "application/json",
    }

def fb_get(path: str, params: dict = {}):
    """Generic GET to Fireberry API"""
    url = f"{FIREBERRY_BASE}/{path}"
    res = requests.get(url, headers=fb_headers(), params=params, timeout=10)
    if res.status_code == 429:
        raise HTTPException(status_code=429, detail="Fireberry rate limit - try again in 60s")
    data = res.json()
    if not data.get("success"):
        raise HTTPException(status_code=502, detail=f"Fireberry error: {data.get('message')}")
    return data

def fb_post(path: str, body: dict):
    """Generic POST to Fireberry API"""
    url = f"{FIREBERRY_BASE}/{path}"
    res = requests.post(url, headers=fb_headers(), json=body, timeout=10)
    data = res.json()
    print(f"FB POST {path} → status={res.status_code} body={data}")
    if not data.get("success"):
        raise HTTPException(status_code=502, detail=f"Fireberry error: {data.get('message')} | full: {data}")
    return data

def normalize_phone(phone: str) -> str:
    """054-1234567 → 0541234567 (Fireberry stores without dashes)"""
    return phone.replace("-", "").replace(" ", "").strip()

def map_customer(raw: dict) -> dict:
    """Fireberry account → unified customer format"""
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
    """Fireberry opportunity → unified order format"""
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

# ─── Health ──────────────────────────────────────────────────

@app.get("/")
def root():
    return {"message": "Customer Data API v2 - Fireberry", "version": "2.0.0"}

@app.get("/health")
def health():
    if not FIREBERRY_TOKEN:
        return {"status": "error", "detail": "FIREBERRY_TOKEN not set"}
    try:
        fb_get("record/account", {"page_size": "1"})
        return {"status": "ok", "crm": "fireberry"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# ─── Customer lookup ─────────────────────────────────────────

@app.get("/customer/{identifier}")
def get_customer(identifier: str):
    if not FIREBERRY_TOKEN:
        raise HTTPException(status_code=500, detail="FIREBERRY_TOKEN not configured")

    identifier = identifier.strip()

    # 1. Try as phone number
    normalized = normalize_phone(identifier)
    if normalized.startswith("0") or normalized.startswith("+972"):
        try:
            data = fb_get("record/account", {
                "fields": "accountid,accountname,emailaddress1,telephone1,telephone2,statuscode,ownerid,description,status",
                "page_size": "50",
            })
            records = data.get("data", {}).get("Records", [])
            # סינון בצד שלנו לפי טלפון
            for r in records:
                t1 = normalize_phone(r.get("telephone1") or "")
                t2 = normalize_phone(r.get("telephone2") or "")
                if t1 == normalized or t2 == normalized:
                    return map_customer(r)
        except Exception:
            pass

    # 2. Try as order number (opportunity)
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

    # 3. Try as email
    if "@" in identifier:
        try:
            data = fb_get("record/account", {
                "fields": "accountid,accountname,emailaddress1,telephone1,statuscode,ownerid,description,status",
                "page_size": "50",
            })
            records = data.get("data", {}).get("Records", [])
            # סינון בצד שלנו לפי אימייל
            identifier_lower = identifier.lower()
            for r in records:
                e1 = (r.get("emailaddress1") or "").lower()
                e2 = (r.get("emailaddress2") or "").lower()
                if e1 == identifier_lower or e2 == identifier_lower:
                    return map_customer(r)
        except Exception:
            pass

    return {"found": False, "message": f"No record found for: {identifier}"}


# ─── Escalate via GET ─────────────────────────────────────────

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

    # ─── Deduplication check ──────────────────────────────────
    cache_key = f"{customer_id}:{intent}"
    last_time = _escalation_cache.get(cache_key, 0)
    now = time.time()
    if now - last_time < COOLDOWN_SECONDS:
        return {
            "success": True,
            "ticket_id": "duplicate_skipped",
            "message": f"Ticket already created for this customer in the last {COOLDOWN_SECONDS}s"
        }
    _escalation_cache[cache_key] = now
    # ─────────────────────────────────────────────────────────

    priority_map = {"low": 1, "medium": 2, "high": 3, "urgent": 4}

    try:
        intent_hebrew = {
            "Refund+Request":       "בקשת החזר כספי",
            "Complaint":            "תלונה",
            "Human+Agent+Request":  "בקשה לנציג אנושי",
            "Unknown+Query":        "פנייה לא מזוהה",
        }.get(intent, intent)

        # סוג לפי intent: 1=בעיה, 2=שאלה, 3=בקשה, 4=תקלה
        type_map = {
            "Refund+Request":      3,  # בקשה
            "Complaint":           1,  # בעיה
            "Human+Agent+Request": 2,  # שאלה
            "Unknown+Query":       2,  # שאלה
        }

        # עדיפות לפי Fireberry: 1=רגילה, 2=גבוהה, 3=נמוכה
        intent_priority = {
            "Refund+Request":      2,  # גבוהה
            "Complaint":           2,  # גבוהה
            "Human+Agent+Request": 1,  # רגילה
            "Unknown+Query":       3,  # נמוכה
        }

        payload = {
            "title":        f"פנייה - {intent_hebrew}" if intent else "פנייה חדשה",
            "description":  description,
            "accountid":    customer_id,
            "casetypecode": type_map.get(intent, 1),
            "prioritycode": intent_priority.get(intent, 1),
            "statuscode":   1,
        }
        # ✅ תוקן: Cases במקום case
        result = fb_post("record/Cases", payload)
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
                pass  # Note failure shouldn't fail the whole escalation

        return {
            "success": True,
            "ticket_id": ticket_id,
            "message": f"Ticket created for customer {customer_id}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── Ticket creation via POST ─────────────────────────────────

@app.post("/ticket")
def create_ticket(body: dict):
    if not FIREBERRY_TOKEN:
        raise HTTPException(status_code=500, detail="FIREBERRY_TOKEN not configured")

    priority_map = {"low": 1, "medium": 2, "high": 3, "urgent": 4}

    payload = {
        "title":        body.get("subject", "Customer inquiry"),
        "description":  body.get("description", ""),
        "accountid":    body.get("customer_id", ""),
        "prioritycode": priority_map.get(body.get("priority", "medium"), 2),
        "statuscode":   1,
    }

    # ✅ תוקן: Cases במקום case
    result = fb_post("record/Cases", payload)
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


# ─── List customers (for testing) ────────────────────────────

@app.get("/customers")
def list_customers(limit: int = 10):
    data = fb_get("record/account", {
        "fields": "accountid,accountname,emailaddress1,telephone1",
        "page_size": str(limit),
    })
    records = data.get("data", {}).get("Records", [])
    return {
        "total":   data.get("data", {}).get("Total_Records", 0),
        "source":  "fireberry",
        "sample":  records,
    }

@app.get("/columns")
def get_columns():
    return {
        "customer_fields": ["customer_name", "phone", "email", "customer_id", "status", "assigned_agent", "notes"],
        "order_fields":    ["order_number", "customer_name", "product", "status", "price", "order_date", "expected_delivery", "notes"],
    }

@app.get("/debug-case/{case_id}")
def debug_case(case_id: str):
    """זמני — מחזיר raw data של קריאה כדי לראות prioritycode"""
    data = fb_get(f"record/Cases/{case_id}")
    return data
