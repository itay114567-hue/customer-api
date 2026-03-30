from fastapi import FastAPI, HTTPException, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
import requests
import os
import time
import base64
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── Deduplication cache ──────────────────────────────────────
_escalation_cache: dict = {}
COOLDOWN_SECONDS = 60

app = FastAPI(title="Customer Data API - Fireberry", version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Environment Variables ───────────────────────────────────
FIREBERRY_TOKEN = os.getenv("FIREBERRY_TOKEN")
FIREBERRY_BASE  = "https://api.fireberry.com/api"

TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUM = os.getenv("TWILIO_WHATSAPP_NUM", "whatsapp:+14155238886")

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
    return data

def fb_post(path: str, body: dict):
    url = f"{FIREBERRY_BASE}/{path}"
    res = requests.post(url, headers=fb_headers(), json=body, timeout=10)
    return res.json()

def normalize_phone(phone: str) -> str:
    if not phone: return ""
    return "".join(filter(str.isdigit, phone)).replace("972", "0")

# ════════════════════════════════════════════════════════════
#  ENDPOINTS — CUSTOMER LOOKUP (FOR AGENT 2)
# ════════════════════════════════════════════════════════════

@app.get("/customer/{identifier}", response_class=PlainTextResponse)
def get_customer_for_agent(identifier: str):
    """
    Endpoint מותאם במיוחד ל-ScrapeWebsiteTool. 
    מחזיר טקסט נקי שהסוכן יכול לקרוא בקלות.
    """
    if not FIREBERRY_TOKEN:
        return "Error: System not configured."

    norm = normalize_phone(identifier)
    
    # חיפוש לקוח ב-Fireberry
    data = fb_get("record/account", {"fields": "accountid,accountname,telephone1,status", "page_size": "100"})
    records = data.get("data", {}).get("Records", [])
    
    customer = None
    for r in records:
        if normalize_phone(r.get("telephone1")) == norm:
            customer = r
            break
    
    if not customer:
        return f"Result: No customer found for identifier {identifier}"

    # החזרת פורמט טקסטואלי שהסוכן "מגרד" בקלות
    return f"""
    --- CUSTOMER DATA FOUND ---
    customer_id: {customer.get('accountid')}
    customer_name: {customer.get('accountname')}
    status: {customer.get('status', 'Active')}
    phone: {customer.get('telephone1')}
    data_found: true
    ---------------------------
    """

# ════════════════════════════════════════════════════════════
#  ENDPOINT — ESCALATE (FOR AGENT 3)
# ════════════════════════════════════════════════════════════

@app.get("/escalate", response_class=PlainTextResponse)
def escalate_via_get(customer_id: str, intent: str = "Unknown", description: str = ""):
    """
    Endpoint זה מאפשר לסוכן "לגרד" URL כדי לבצע פעולת כתיבה (Escalation).
    """
    if not customer_id:
        return "Error: Missing customer_id"

    # מניעת כפילויות
    cache_key = f"{customer_id}:{intent}"
    now = time.time()
    if now - _escalation_cache.get(cache_key, 0) < COOLDOWN_SECONDS:
        return "Success: Duplicate escalation skipped, ticket already exists."
    
    _escalation_cache[cache_key] = now

    # בניית הפנייה ל-Fireberry
    try:
        body = {
            "title": f"WhatsApp Escalation: {intent}",
            "description": description,
            "accountid": customer_id,
            "casetypecode": 1, # פנייה
            "statuscode": 1    # חדש
        }
        res = fb_post("record/Cases", body)
        
        if res.get("success"):
            ticket_id = res.get("data", {}).get("casesid", "Created")
            return f"Success: Ticket created in Fireberry. TicketID: {ticket_id}"
        else:
            return f"Error: Fireberry failed to create ticket. {res.get('message')}"
            
    except Exception as e:
        return f"Error: {str(e)}"

# ════════════════════════════════════════════════════════════
#  WHATSAPP WEBHOOK (TWILIO)
# ════════════════════════════════════════════════════════════

@app.post("/webhook/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(
    background_tasks: BackgroundTasks,
    Body: str = Form(default=""),
    From: str = Form(default=""),
):
    """
    כאן Twilio מדווחת על הודעה חדשה.
    כרגע זה רק מדפיס ללוג ב-Railway. 
    בעתיד כאן יקרה ה-Kickoff של CrewAI.
    """
    print(f"📨 NEW MESSAGE FROM {From}: {Body}")
    
    # כאן אפשר להוסיף לוגיקה שתשלח את ההודעה ל-CrewAI Enterprise API
    
    return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

# ════════════════════════════════════════════════════════════
#  HEALTH & ROOT
# ════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {"status": "online", "service": "Fireberry-CrewAI-Bridge"}

@app.get("/health")
def health():
    return {"fireberry_configured": bool(FIREBERRY_TOKEN), "twilio_configured": bool(TWILIO_ACCOUNT_SID)}
