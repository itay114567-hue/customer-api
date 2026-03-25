# 🤖 Customer Data API

API גמיש לחיבור סוכני CrewAI לכל מסד נתונים של לקוח.

---

## 🚀 העלאה ל-Railway (5 דקות)

### שלב 1 — התקנות
```bash
pip install fastapi uvicorn pandas
npm install -g @railway/cli
```

### שלב 2 — התחברות ל-Railway
```bash
railway login
```

### שלב 3 — אתחול הפרויקט (בתיקיית customer-api)
```bash
cd customer-api
railway init
```

### שלב 4 — הוסף Environment Variable
```bash
railway variables set DATABASE_URL="https://docs.google.com/spreadsheets/d/1coGufe1kQwkf4AVOPGHxNLk7bbWyvuZODul_z2APRSU/export?format=csv&gid=1338068448"
```

### שלב 5 — העלה
```bash
railway up
```

Railway ייתן לך URL כזה:
```
https://customer-api-production.up.railway.app
```

---

## 🔗 Endpoints

| Method | URL | תיאור |
|--------|-----|-------|
| GET | `/` | בדיקת חיים |
| GET | `/health` | סטטוס |
| GET | `/customer/{identifier}` | חיפוש לקוח לפי מספר הזמנה או טלפון |
| GET | `/customers?limit=10` | רשימת לקוחות (לבדיקות) |
| GET | `/columns` | עמודות ה-Database |

### דוגמה לשימוש
```
GET https://your-api.up.railway.app/customer/12345
GET https://your-api.up.railway.app/customer/0501234567
```

---

## 🏢 לכל לקוח עסקי חדש

רק תשנה את `DATABASE_URL` ב-Railway:

```bash
# לקוח עם Google Sheets
railway variables set DATABASE_URL="https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv"

# לקוח עם Airtable
railway variables set DATABASE_URL="https://airtable.com/..."

# לקוח עם קובץ CSV
railway variables set DATABASE_URL="https://example.com/customers.csv"
```

**הסוכנים ב-CrewAI לא משתנים לעולם!**

---

## 🤖 חיבור ל-CrewAI

בסוכן Customer Data Retriever, ב-Backstory הוסף:

```
To fetch customer data, use the HTTPTool to call:
GET https://your-api.up.railway.app/customer/{order_number_or_phone}

The response will contain:
- found: true/false
- data: object with all customer fields (Status, Order Date, Customer Name, etc.)

If found is false, report "Customer not found" immediately.
```

---

## 📁 מבנה הפרויקט

```
customer-api/
├── main.py          ← הקוד הראשי
├── requirements.txt ← תלויות Python
├── railway.json     ← הגדרות Railway
└── .env.example     ← דוגמה לקובץ הגדרות
```
