from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import os

app = FastAPI(title="Customer Data API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def load_data():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")
    try:
        df = pd.read_csv(database_url)
        df.columns = df.columns.str.strip()
        return df
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load data: {str(e)}")

@app.get("/")
def root():
    return {"message": "Customer Data API is running", "version": "1.0.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/customer/{identifier}")
def get_customer(identifier: str):
    df = load_data()
    mask = df.apply(
        lambda col: col.astype(str).str.strip() == identifier.strip()
    ).any(axis=1)
    result = df[mask]
    if result.empty:
        return {"found": False, "message": f"No customer found for: {identifier}"}
    record = result.iloc[0].fillna("").to_dict()
    return {"found": True, "data": record}

@app.get("/customers")
def list_customers(limit: int = 10):
    df = load_data()
    return {
        "total": len(df),
        "columns": list(df.columns),
        "sample": df.head(limit).fillna("").to_dict(orient="records")
    }

@app.get("/columns")
def get_columns():
    df = load_data()
    return {"columns": list(df.columns)}
