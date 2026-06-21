import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ai_triage import ai_triage_summary
from auth import check_admin_credentials, create_admin_token, verify_admin
from email_service import send_quote_notification
from pricing_engine import PricingSettings, calculate_quote

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "C3D Prints Quote Portal")
DB_PATH = Path(__file__).parent / "quote_requests.db"

app = FastAPI(title=APP_NAME)

allowed_origins = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_pricing_settings() -> PricingSettings:
    return PricingSettings(
        kwh=float(os.getenv("DEFAULT_KWH", 0.33)),
        watts=float(os.getenv("DEFAULT_WATTS", 180)),
        spool_usd=float(os.getenv("DEFAULT_SPOOL_USD", 20)),
        spool_g=float(os.getenv("DEFAULT_SPOOL_G", 1000)),
        nozzle_cost=float(os.getenv("DEFAULT_NOZZLE_COST", 8)),
        nozzle_hours=float(os.getenv("DEFAULT_NOZZLE_HOURS", 400)),
        sheet_cost=float(os.getenv("DEFAULT_SHEET_COST", 25)),
        sheet_prints=float(os.getenv("DEFAULT_SHEET_PRINTS", 500)),
        shipping=float(os.getenv("DEFAULT_SHIPPING", 5)),
        boxing=float(os.getenv("DEFAULT_BOXING", 1.50)),
        tax=float(os.getenv("DEFAULT_TAX", 6.25)),
        markup=float(os.getenv("DEFAULT_MARKUP", 50)),
        labor_rate=float(os.getenv("DEFAULT_LABOR_RATE", 35)),
    )


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quote_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            project_description TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            approx_size TEXT,
            deadline TEXT,
            material_preference TEXT,
            color_preference TEXT,
            use_case TEXT,
            requirements TEXT,
            delivery_method TEXT,
            shipping_location TEXT,
            additional_notes TEXT,
            ai_summary TEXT,
            status TEXT DEFAULT 'New'
        )
    """)
    conn.commit()
    conn.close()


init_db()


def save_request(data: dict, ai_summary: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO quote_requests (
            created_at, name, email, phone, project_description, quantity,
            approx_size, deadline, material_preference, color_preference,
            use_case, requirements, delivery_method, shipping_location,
            additional_notes, ai_summary, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        data["name"], data["email"], data.get("phone"),
        data["project_description"], data["quantity"], data.get("approx_size"),
        data.get("deadline"), data.get("material_preference"),
        data.get("color_preference"), data.get("use_case"),
        json.dumps(data.get("requirements", [])), data.get("delivery_method"),
        data.get("shipping_location"), data.get("additional_notes"),
        ai_summary, "New",
    ))
    request_id = cur.lastrowid
    conn.commit()
    conn.close()
    return request_id


@app.get("/")
def root():
    return {
        "ok": True,
        "app": APP_NAME,
        "routes": ["/health", "/quote-request", "/calculate", "/admin/login", "/admin/requests"],
    }


@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME}


class AdminLoginRequest(BaseModel):
    username: str
    password: str


@app.post("/admin/login")
def admin_login(request: AdminLoginRequest):
    try:
        valid = check_admin_credentials(request.username, request.password)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not valid:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    return {
        "token": create_admin_token(request.username),
        "expires_in_seconds": 60 * 60 * 12,
    }


@app.post("/quote-request")
async def quote_request(
    name: str = Form(...),
    email: str = Form(...),
    phone: Optional[str] = Form(None),
    project_description: str = Form(...),
    quantity: int = Form(1),
    approx_size: Optional[str] = Form(None),
    deadline: Optional[str] = Form(None),
    material_preference: Optional[str] = Form("Not sure"),
    color_preference: Optional[str] = Form(None),
    use_case: Optional[str] = Form(None),
    requirements: Optional[List[str]] = Form(None),
    delivery_method: Optional[str] = Form(None),
    shipping_location: Optional[str] = Form(None),
    additional_notes: Optional[str] = Form(None),
    files: Optional[List[UploadFile]] = File(None),
):
    data = {
        "name": name, "email": email, "phone": phone,
        "project_description": project_description, "quantity": quantity,
        "approx_size": approx_size, "deadline": deadline,
        "material_preference": material_preference, "color_preference": color_preference,
        "use_case": use_case, "requirements": requirements or [],
        "delivery_method": delivery_method, "shipping_location": shipping_location,
        "additional_notes": additional_notes,
        "uploaded_files": [f.filename for f in files] if files else [],
    }

    ai_summary = ai_triage_summary(data)
    request_id = save_request(data, ai_summary)

    notify_email = os.getenv("QUOTE_NOTIFY_EMAIL", "hi@c3dprints.com")
    file_list = "<br>".join(data["uploaded_files"]) if data["uploaded_files"] else "No files uploaded"

    html_body = f"""
    <h2>New C3D Prints Quote Request #{request_id}</h2>
    <p><strong>Customer:</strong> {name} &lt;{email}&gt;</p>
    <p><strong>Phone:</strong> {phone or "Not provided"}</p>
    <p><strong>Quantity:</strong> {quantity}</p>
    <p><strong>Material:</strong> {material_preference or "Not sure"}</p>
    <p><strong>Color:</strong> {color_preference or "Not provided"}</p>
    <p><strong>Use Case:</strong> {use_case or "Not provided"}</p>
    <p><strong>Deadline:</strong> {deadline or "Not provided"}</p>
    <p><strong>Delivery:</strong> {delivery_method or "Not provided"}</p>
    <p><strong>Shipping Location:</strong> {shipping_location or "Not provided"}</p>
    <h3>Project Description</h3><p>{project_description}</p>
    <h3>Requirements</h3><p>{", ".join(requirements or []) or "None selected"}</p>
    <h3>Uploaded Files</h3><p>{file_list}</p>
    <h3>AI Triage Summary</h3>
    <pre style="white-space:pre-wrap;font-family:Arial,sans-serif;">{ai_summary}</pre>
    """

    email_result = send_quote_notification(
        to_email=notify_email,
        subject=f"New C3D Quote Request #{request_id} — {name}",
        html_body=html_body,
        text_body=ai_summary,
    )

    return {"success": True, "request_id": request_id, "message": "Quote request submitted successfully.", "email": email_result}


class PricingRequest(BaseModel):
    grams: float
    hours: float
    quantity: int = 1
    fail_rate: float = 0
    labor_minutes: float = 0
    cad_fee: float = 0
    rush_fee: float = 0
    complexity_multiplier: float = 1.0
    include_shipping: bool = True


@app.post("/calculate")
def calculate(request: PricingRequest):
    try:
        return calculate_quote(
            grams=request.grams,
            hours=request.hours,
            quantity=request.quantity,
            fail_rate=request.fail_rate,
            labor_minutes=request.labor_minutes,
            cad_fee=request.cad_fee,
            rush_fee=request.rush_fee,
            complexity_multiplier=request.complexity_multiplier,
            include_shipping=request.include_shipping,
            settings=get_pricing_settings(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/admin/requests")
def list_requests(admin=Depends(verify_admin)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM quote_requests ORDER BY id DESC LIMIT 100")
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows
