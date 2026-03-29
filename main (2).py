from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import os

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
    if not data.get("success"):
        raise HTTPException(status_code=502, detail=f"Fireberry error: {data.get('message')}")
    return data

def normalize_phone(phone: str) -> str:
    """054-1234567 → 0541234567 (Fireberry stores without dashes)"""
    return phone.replace("-", "").replace(" ", "").strip()

def map_customer(raw: dict) -> dict:
    """Fireberry account → unified customer format (same structure agents expect)"""
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

# ─── Main endpoint — same URL the agents already call ────────
# /customer/{identifier}  ← agents don't change anything

@app.get("/customer/{identifier}")
def get_customer(identifier: str):
    """
    Search by: order number, phone, or email.
    Returns same JSON structure as before — agents don't notice the change.
    """
    if not FIREBERRY_TOKEN:
        raise HTTPException(status_code=500, detail="FIREBERRY_TOKEN not configured")

    identifier = identifier.strip()

    # 1. Try as phone number → search account first (most common case)
    normalized = normalize_phone(identifier)
    if normalized.startswith("0") or normalized.startswith("+972"):
        try:
            data = fb_get("record/account", {
                "query": f'telephone1 = "{normalized}"',
                "fields": "accountid,accountname,emailaddress1,telephone1,statuscode,ownerid,description",
                "page_size": "1",
            })
            records = data.get("data", {}).get("Records", [])
            if records:
                return map_customer(records[0])
        except Exception:
            pass

    # 2. Try as order number (opportunity)
    if identifier.upper().startswith("ORD-") or identifier.isdigit():
        try:
            data = fb_get("record/opportunity", {
                "query": f'name = "{identifier}"',
                "fields": "opportunityid,name,accountname,statuscode,totalamount,createdon,estimatedclosedate,description",
                "page_size": "1",
            })
            records = data.get("data", {}).get("Records", [])
            if records:
                return map_order(records[0])
        except Exception:
            pass

    # 3. Try as email
    if "@" in identifier:
        try:
            data = fb_get("record/account", {
                "query": f'emailaddress1 = "{identifier}"',
                "fields": "accountid,accountname,emailaddress1,telephone1,statuscode,ownerid,description",
                "page_size": "1",
            })
            records = data.get("data", {}).get("Records", [])
            if records:
                return map_customer(records[0])
        except Exception:
            pass

    # 4. Not found
    return {"found": False, "message": f"No record found for: {identifier}"}

# ─── Ticket creation — new endpoint for Decision Maker ───────

@app.post("/ticket")
def create_ticket(body: dict):
    """
    Called by Decision Maker when action_type = Escalate or needs a ticket.
    Body: { customer_id, subject, description, priority, channel, intent }
    """
    if not FIREBERRY_TOKEN:
        raise HTTPException(status_code=500, detail="FIREBERRY_TOKEN not configured")

    priority_map = {"low": 1, "medium": 2, "high": 3, "urgent": 4}

    payload = {
        "title":        body.get("subject", "Customer inquiry"),
        "description":  body.get("description", ""),
        "accountid":    body.get("customer_id", ""),
        "prioritycode": priority_map.get(body.get("priority", "medium"), 2),
        "statuscode":   1,  # open
    }

    result = fb_post("record/case", payload)
    ticket_id = result.get("data", {}).get("caseid", "")

    # Add note with channel + intent context
    if ticket_id:
        fb_post("record/note", {
            "regardingobjectid":       ticket_id,
            "regardingobjecttypecode": "case",
            "notetext": f"Channel: {body.get('channel', 'unknown')}\nIntent: {body.get('intent', 'unknown')}",
            "subject":  "Agent context",
        })

    return {"success": True, "ticket_id": ticket_id}

# ─── Optional: list customers (for testing) ──────────────────

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
    """Returns the unified field names agents should expect"""
    return {
        "customer_fields": ["customer_name", "phone", "email", "customer_id", "status", "assigned_agent", "notes"],
        "order_fields":    ["order_number", "customer_name", "product", "status", "price", "order_date", "expected_delivery", "notes"],
    }
