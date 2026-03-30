from fastapi import FastAPI, HTTPException, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from twilio.rest import Client
import requests
import os
import time

# --- Deduplication cache ---
_escalation_cache: dict = {}
COOLDOWN_SECONDS = 60

app = FastAPI(title="Customer Data API - Fireberry & Twilio", version="3.7.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Environment Variables ---
FIREBERRY_TOKEN = os.getenv("FIREBERRY_TOKEN")
FIREBERRY_BASE  = "https://api.fireberry.com/api"

TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUM = os.getenv("TWILIO_WHATSAPP_NUM", "whatsapp:+14155238886")

# משתני ה-CrewAI
CREWAI_API_KEY = os.getenv("CREWAI_API_KEY")
CREWAI_KICKOFF_URL = os.getenv("CREWAI_KICKOFF_URL")

# --- HELPERS ---

def fb_headers():
    return {"tokenid": FIREBERRY_TOKEN, "Content-Type": "application/json"}

def fb_get(path: str, params: dict = {}):
    url = f"{FIREBERRY_BASE}/{path}"
    res = requests.get(url, headers=fb_headers(), params=params, timeout=10)
    return res.json()

def fb_post(path: str, body: dict):
    url = f"{FIREBERRY_BASE}/{path}"
    res = requests.post(url, headers=fb_headers(), json=body, timeout=10)
    return res.json()

def normalize_phone(phone: str) -> str:
    if not phone: return ""
    return "".join(filter(str.isdigit, phone)).replace("972", "0")

# --- TWILIO SEND FUNCTION ---

def send_whatsapp(to_number: str, message_body: str):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    clean_number = to_number.replace("whatsapp:", "")
    formatted_number = f"whatsapp:{clean_number}"
    
    msg = client.messages.create(
        from_=TWILIO_WHATSAPP_NUM,
        body=message_body,
        to=formatted_number
    )
    return msg.sid

# ════════════════════════════════════════════════════════════
#  ENDPOINTS
# ════════════════════════════════════════════════════════════

@app.get("/customer/{identifier}", response_class=PlainTextResponse)
def get_customer(identifier: str):
    norm = normalize_phone(identifier)
    data = fb_get("record/account", {"fields": "accountid,accountname,telephone1,status", "page_size": "100"})
    records = data.get("data", {}).get("Records", [])
    
    customer = next((r for r in records if normalize_phone(r.get("telephone1")) == norm), None)
    
    if not customer:
        return f"Result: No customer found for {identifier}"

    return f"customer_id: {customer.get('accountid')}\nname: {customer.get('accountname')}\nstatus: {customer.get('status')}\ndata_found: true"

@app.get("/escalate", response_class=PlainTextResponse)
def escalate(customer_id: str, intent: str = "Unknown", description: str = ""):
    cache_key = f"{customer_id}:{intent}"
    if time.time() - _escalation_cache.get(cache_key, 0) < COOLDOWN_SECONDS:
        return "Duplicate skipped."
    
    _escalation_cache[cache_key] = time.time()
    body = {"title": f"WhatsApp: {intent}", "description": description, "accountid": customer_id, "casetypecode": 1}
    res = fb_post("record/Cases", body)
    
    if res.get("success"):
        return f"Success: Ticket ID {res.get('data', {}).get('casesid')}"
    return f"Error: {res.get('message')}"

@app.get("/send_response", response_class=PlainTextResponse)
def api_send_response(phone: str, message: str):
    try:
        sid = send_whatsapp(phone, message)
        return f"Message Sent. SID: {sid}"
    except Exception as e:
        return f"Failed: {str(e)}"

# ════════════════════════════════════════════════════════════
#  WEBHOOK - המשודרג עם שמות משתנים תואמים ל-Studio
# ════════════════════════════════════════════════════════════

@app.post("/webhook/whatsapp")
async def webhook(background_tasks: BackgroundTasks, Body: str = Form(...), From: str = Form(...)):
    print(f"📨 NEW MESSAGE | From: {From} | Body: {Body}")
    
    if CREWAI_KICKOFF_URL and CREWAI_API_KEY:
        def start_crew():
            # עדכון ה-Payload שיתאים בדיוק לדרישות ה-Inputs ב-Crew Studio
            payload = {
                "inputs": {
                    # שינוי מ-raw_message ל-customer_input כדי להתאים ל-Studio
                    "customer_input": Body,
                    "order_number_or_phone": From.replace("whatsapp:", ""),
                    # הוספת formatted_message כדי למנוע שגיאת 422 Missing inputs
                    "formatted_message": "New WhatsApp Inquiry"
                }
            }
            
            # וידוא פורמט Bearer Token
            token = CREWAI_API_KEY if CREWAI_API_KEY.startswith("Bearer ") else f"Bearer {CREWAI_API_KEY}"
            headers = {"Authorization": token, "Content-Type": "application/json"}
            
            try:
                print(f"🚀 SENDING TO CREWAI: {CREWAI_KICKOFF_URL}")
                response = requests.post(CREWAI_KICKOFF_URL, json=payload, headers=headers, timeout=20)
                # הדפסת התשובה כדי לוודא שקיבלנו 200 OK
                print(f"✅ CREWAI RESPONSE: {response.status_code} - {response.text}")
            except Exception as e:
                print(f"❌ CREWAI CONNECTION ERROR: {str(e)}")
        
        background_tasks.add_task(start_crew)
    else:
        print("⚠️ SKIPPING CREWAI: Missing URL or API Key!")
    
    return PlainTextResponse('<?xml version="1.0" encoding="UTF-8"?><Response></Response>')

@app.get("/")
def root(): return {"status": "online", "version": "3.7.0"}
