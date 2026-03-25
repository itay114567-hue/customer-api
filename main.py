from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import os
from typing import Optional

app = FastAPI(title="Customer Data API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def load_data():
    """Load customer data from DATABASE_URL environment variable."""
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
    """
    Fetch customer data by order number or phone number.
    Searches all columns for a match.
    """
    df = load_data()

    # Search across all columns
    mask = df.apply(
        lambda col: col.astype(str).str.strip() == identifier.strip()
    ).any(axis=1)

    result = df[mask]

    if result.empty:
        return {"found": False, "message": f"No customer found for: {identifier}"}

    record = result.iloc[0].to_dict()
    return {"found": True, "data": record}


@app.get("/customers")
def list_customers(limit: int = 10):
    """Return first N customers (for testing)."""
    df = load_data()
    return {
        "total": len(df),
        "columns": list(df.columns),
        "sample": df.head(limit).to_dict(orient="records")
    }


@app.get("/columns")
def get_columns():
    """Return column names of the database (for debugging)."""
    df = load_data()
    return {"columns": list(df.columns)}
