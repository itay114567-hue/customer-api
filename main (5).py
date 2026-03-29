from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
import time

_escalation_cache: dict = {}
COOLDOWN_SECONDS = 60

app = FastAPI(title="Customer Data API - Fireberry", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FIREBERRY_TOKEN = os.getenv("FIREBERRY_TOKEN")
FIREBERRY_BASE  = "https://api.fireberry.com/api"

def fb_headers():
    return {
        "tokenid": FIREBERRY_TOKEN,
        "Content-Type": "application/json",
    }

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
    print(f"FB POST {path} → status={res.status_code} payload={body} response={data}")
    if not data.get("success"):
        raise HTTPException(status_code=502, detail=f"Fireberry error: {data.get('message')} | full: {data}")
    return data

def normalize_phone(phone: str) -> str:
    return phone.replace("-", "").replace(" ", "").strip()

def normalize_intent(intent: str) -> str:
    """'Refund+Request' or 'Refund Request' → 'Refund Request'"""
    return intent.replace("+", " ").strip()

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

# ─── intent → Fireberry codes ────────────────────────────────
# סטטוס:   1=פתוח, 2=בביצוע, 3=הושלם, 4=מבוטל
# עדיפות:  1=נמוכה, 2=רגילה, 3=גבוהה, 4=דחופה
# סוג:     1=בעיה, 2=שאלה, 3=בקשה, 4=תקלה

INTENT_CONFIG = {
    "Refund Request":      {"priority": 2, "type": 3, "hebrew": "בקשת החזר כספי"},
    "Complaint":           {"priority": 3, "type": 1, "hebrew": "תלונה"},
    "Human Agent Request": {"priority": 2, "type": 2, "hebrew": "בקשה לנציג אנושי"},
    "Unknown Query":       {"priority": 1, "type": 2, "hebrew": "פנייה לא מזוהה"},
    "Status Check":        {"priority": 1, "type": 2, "hebrew": "בדיקת סטטוס"},
    "Cancellation":        {"priority": 2, "type": 3, "hebrew": "בקשת ביטול"},
}

PRIORITY_MAP = {"low": 1, "medium": 2, "high": 3, "urgent": 4}

# ─── Endpoints ───────────────────────────────────────────────

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

@app.get("/customer/{identifier}")
def get_customer(identifier: str):
    if not FIREBERRY_TOKEN:
        raise HTTPException(status_code=500, detail="FIREBERRY_TOKEN not configured")

    identifier = identifier.strip()

    # 1. Phone
    normalized = normalize_phone(identifier)
    if normalized.startswith("0") or normalized.startswith("+972"):
        try:
            data = fb_get("record/account", {
                "query": f'telephone1 = "{normalized}"',
                "fields": "accountid,accountname,emailaddress1,telephone1,statuscode,ownerid,description,status,accounttype,originatinglead,ownername,needs",
                "page_size": "1",
            })
            records = data.get("data", {}).get("Records", [])
            if records:
                return map_customer(records[0])
        except Exception:
            pass

    # 2. Order number
    if identifier.upper().startswith("ORD-") or identifier.isdigit():
        try:
            data = fb_get("record/opportunity", {
                "query": f'name = "{identifier}"',
                "fields": "opportunityid,name,accountname,statuscode,totalamount,createdon,estimatedclosedate,description,status",
                "page_size": "1",
            })
            records = data.get("data", {}).get("Records", [])
            if records:
                return map_order(records[0])
        except Exception:
            pass

    # 3. Email
    if "@" in identifier:
        try:
            data = fb_get("record/account", {
                "query": f'emailaddress1 = "{identifier}"',
                "fields": "accountid,accountname,emailaddress1,telephone1,statuscode,ownerid,description,status,accounttype,originatinglead,ownername,needs",
                "page_size": "1",
            })
            records = data.get("data", {}).get("Records", [])
            if records:
                return map_customer(records[0])
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

    # Deduplication
    cache_key = f"{customer_id}:{intent}"
    now = time.time()
    if now - _escalation_cache.get(cache_key, 0) < COOLDOWN_SECONDS:
        return {"success": True, "ticket_id": "duplicate_skipped", "message": "Ticket already created in last 60s"}
    _escalation_cache[cache_key] = now

    # Normalize intent (handles both "Refund+Request" and "Refund Request")
    intent_clean = normalize_intent(intent)
    config = INTENT_CONFIG.get(intent_clean, {"priority": PRIORITY_MAP.get(priority, 3), "type": 1, "hebrew": intent_clean})

    try:
        payload = {
            "title":        f"פנייה - {config['hebrew']}",
            "description":  description,
            "accountid":    customer_id,
            "prioritycode": config["priority"],   # int: 1=נמוכה, 2=רגילה, 3=גבוהה, 4=דחופה
            "casetypecode": config["type"],        # int: 1=בעיה, 2=שאלה, 3=בקשה, 4=תקלה
            "statuscode":   1,                     # פתוח
        }

        result = fb_post("record/Cases", payload)
        ticket_id = result.get("data", {}).get("Record", result.get("data", {})).get("casesid", "") or result.get("data", {}).get("casesid", "") or result.get("data", {}).get("caseid", "")

        if ticket_id:
            try:
                fb_post("record/note", {
                    "regardingobjectid":       ticket_id,
                    "regardingobjecttypecode": "Cases",
                    "notetext": f"Channel: {channel}\nIntent: {intent_clean}\nMessage: {description}",
                    "subject":  "Agent escalation",
                })
            except Exception:
                pass

        return {
            "success":   True,
            "ticket_id": ticket_id,
            "intent":    intent_clean,
            "priority":  config["priority"],
            "type":      config["type"],
            "message":   f"Ticket created for customer {customer_id}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/ticket")
def create_ticket(body: dict):
    if not FIREBERRY_TOKEN:
        raise HTTPException(status_code=500, detail="FIREBERRY_TOKEN not configured")

    intent_clean = normalize_intent(body.get("intent", ""))
    config = INTENT_CONFIG.get(intent_clean, {"priority": 2, "type": 1, "hebrew": intent_clean})

    payload = {
        "title":        body.get("subject", "Customer inquiry"),
        "description":  body.get("description", ""),
        "accountid":    body.get("customer_id", ""),
        "prioritycode": config["priority"],
        "casetypecode": config["type"],
        "statuscode":   1,
    }

    result = fb_post("record/Cases", payload)
    ticket_id = result.get("data", {}).get("Record", result.get("data", {})).get("casesid", "") or result.get("data", {}).get("casesid", "") or result.get("data", {}).get("caseid", "")

    if ticket_id:
        try:
            fb_post("record/note", {
                "regardingobjectid":       ticket_id,
                "regardingobjecttypecode": "Cases",
                "notetext": f"Channel: {body.get('channel', 'unknown')}\nIntent: {intent_clean}",
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
    return {
        "total":  data.get("data", {}).get("Total_Records", 0),
        "source": "fireberry",
        "sample": records,
    }

@app.get("/columns")
def get_columns():
    return {
        "customer_fields": ["customer_name", "phone", "email", "customer_id", "status", "account_type", "source_channel", "assigned_agent", "notes"],
        "order_fields":    ["order_number", "customer_name", "product", "status", "price", "order_date", "expected_delivery", "notes"],
        "intent_types":    list(INTENT_CONFIG.keys()),
    }

@app.get("/debug-case/{case_id}")
def debug_case(case_id: str):
    return fb_get(f"record/Cases/{case_id}")
