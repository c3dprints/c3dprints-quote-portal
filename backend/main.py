import json
import os
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

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
DATABASE_URL = os.getenv("DATABASE_URL")

VALID_STATUSES = {
    "New",
    "Need Info",
    "Quoted",
    "Approved",
    "Printing",
    "Completed",
    "Archived",
}

app = FastAPI(title=APP_NAME)

allowed_origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def normalized_database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    parsed = urlparse(DATABASE_URL)
    query = dict(parse_qsl(parsed.query))

    if "sslmode" not in query:
        query["sslmode"] = "require"

    return urlunparse(parsed._replace(query=urlencode(query)))


def get_conn():
    return psycopg.connect(
        normalized_database_url(),
        row_factory=dict_row,
        prepare_threshold=None,
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
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quote_requests (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    phone TEXT,
                    project_description TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    approx_size TEXT,
                    deadline TEXT,
                    material_preference TEXT,
                    color_preference TEXT,
                    use_case TEXT,
                    requirements JSONB DEFAULT '[]'::jsonb,
                    delivery_method TEXT,
                    shipping_location TEXT,
                    additional_notes TEXT,
                    uploaded_files JSONB DEFAULT '[]'::jsonb,
                    ai_summary TEXT,
                    status TEXT NOT NULL DEFAULT 'New',
                    final_price NUMERIC,
                    deposit_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    due_date TEXT,
                    print_notes TEXT,
                    quoted_price NUMERIC,
                    quote_message TEXT,
                    quote_sent_at TIMESTAMPTZ
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_quote_requests_created_at
                ON quote_requests (created_at DESC);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_quote_requests_status
                ON quote_requests (status);
                """
            )
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS final_price NUMERIC;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS deposit_paid BOOLEAN NOT NULL DEFAULT FALSE;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS due_date TEXT;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS print_notes TEXT;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS quoted_price NUMERIC;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS quote_message TEXT;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS quote_sent_at TIMESTAMPTZ;")



def save_request(data: dict, ai_summary: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quote_requests (
                    created_at,
                    name,
                    email,
                    phone,
                    project_description,
                    quantity,
                    approx_size,
                    deadline,
                    material_preference,
                    color_preference,
                    use_case,
                    requirements,
                    delivery_method,
                    shipping_location,
                    additional_notes,
                    uploaded_files,
                    ai_summary,
                    final_price,
                    deposit_paid,
                    due_date,
                    print_notes,
                    quoted_price,
                    quote_message,
                    quote_sent_at,
                    status
                )
                VALUES (
                    %(created_at)s,
                    %(name)s,
                    %(email)s,
                    %(phone)s,
                    %(project_description)s,
                    %(quantity)s,
                    %(approx_size)s,
                    %(deadline)s,
                    %(material_preference)s,
                    %(color_preference)s,
                    %(use_case)s,
                    %(requirements)s,
                    %(delivery_method)s,
                    %(shipping_location)s,
                    %(additional_notes)s,
                    %(uploaded_files)s,
                    %(ai_summary)s,
                    %(status)s
                )
                RETURNING id;
                """,
                {
                    "created_at": datetime.now(timezone.utc),
                    "name": data["name"],
                    "email": data["email"],
                    "phone": data.get("phone"),
                    "project_description": data["project_description"],
                    "quantity": data["quantity"],
                    "approx_size": data.get("approx_size"),
                    "deadline": data.get("deadline"),
                    "material_preference": data.get("material_preference"),
                    "color_preference": data.get("color_preference"),
                    "use_case": data.get("use_case"),
                    "requirements": Json(data.get("requirements", [])),
                    "delivery_method": data.get("delivery_method"),
                    "shipping_location": data.get("shipping_location"),
                    "additional_notes": data.get("additional_notes"),
                    "uploaded_files": Json(data.get("uploaded_files", [])),
                    "ai_summary": ai_summary,
                    "status": "New",
                },
            )

            row = cur.fetchone()
            return row["id"]


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def root():
    return {
        "ok": True,
        "app": APP_NAME,
        "storage": "supabase_postgres",
        "routes": [
            "/health",
            "/quote-request",
            "/calculate",
            "/admin/login",
            "/admin/requests",
            "/admin/requests/{request_id}/status",
        ],
    }


@app.get("/health")
def health():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS count FROM quote_requests;")
                row = cur.fetchone()

        return {
            "ok": True,
            "app": APP_NAME,
            "storage": "supabase_postgres",
            "database_configured": bool(DATABASE_URL),
            "quote_count": row["count"],
        }

    except Exception as exc:
        return {
            "ok": False,
            "app": APP_NAME,
            "storage": "supabase_postgres",
            "database_configured": bool(DATABASE_URL),
            "error": str(exc),
        }


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
        "name": name,
        "email": email,
        "phone": phone,
        "project_description": project_description,
        "quantity": quantity,
        "approx_size": approx_size,
        "deadline": deadline,
        "material_preference": material_preference,
        "color_preference": color_preference,
        "use_case": use_case,
        "requirements": requirements or [],
        "delivery_method": delivery_method,
        "shipping_location": shipping_location,
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

    <h3>Project Description</h3>
    <p>{project_description}</p>

    <h3>Requirements</h3>
    <p>{", ".join(requirements or []) or "None selected"}</p>

    <h3>Uploaded Files</h3>
    <p>{file_list}</p>

    <h3>AI Triage Summary</h3>
    <pre style="white-space:pre-wrap;font-family:Arial,sans-serif;">{ai_summary}</pre>
    """

    email_result = send_quote_notification(
        to_email=notify_email,
        subject=f"New C3D Quote Request #{request_id} — {name}",
        html_body=html_body,
        text_body=ai_summary,
    )

    return {
        "success": True,
        "request_id": request_id,
        "message": "Quote request submitted successfully.",
        "email": email_result,
    }


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
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    created_at,
                    name,
                    email,
                    phone,
                    project_description,
                    quantity,
                    approx_size,
                    deadline,
                    material_preference,
                    color_preference,
                    use_case,
                    requirements,
                    delivery_method,
                    shipping_location,
                    additional_notes,
                    uploaded_files,
                    ai_summary,
                    final_price,
                    deposit_paid,
                    due_date,
                    print_notes,
                    quoted_price,
                    quote_message,
                    quote_sent_at,
                    status
                FROM quote_requests
                ORDER BY created_at DESC
                LIMIT 100;
                """
            )
            return cur.fetchall()


class StatusUpdateRequest(BaseModel):
    status: str


@app.patch("/admin/requests/{request_id}/status")
def update_request_status(
    request_id: int,
    request: StatusUpdateRequest,
    admin=Depends(verify_admin),
):
    status = request.status.strip()

    if status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Valid statuses: {', '.join(sorted(VALID_STATUSES))}",
        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM quote_requests WHERE id = %(request_id)s;",
                {"request_id": request_id},
            )
            existing = cur.fetchone()

            if not existing:
                raise HTTPException(status_code=404, detail="Quote request not found")

            cur.execute(
                """
                UPDATE quote_requests
                SET status = %(status)s
                WHERE id = %(request_id)s
                RETURNING
                    id,
                    created_at,
                    name,
                    email,
                    phone,
                    project_description,
                    quantity,
                    approx_size,
                    deadline,
                    material_preference,
                    color_preference,
                    use_case,
                    requirements,
                    delivery_method,
                    shipping_location,
                    additional_notes,
                    uploaded_files,
                    ai_summary,
                    final_price,
                    deposit_paid,
                    due_date,
                    print_notes,
                    quoted_price,
                    quote_message,
                    quote_sent_at,
                    status;
                """,
                {"status": status, "request_id": request_id},
            )
            updated = cur.fetchone()

    return {"success": True, "request": updated}

class JobDetailsUpdateRequest(BaseModel):
    final_price: Optional[float] = None
    deposit_paid: bool = False
    due_date: Optional[str] = None
    print_notes: Optional[str] = None
    approve: bool = False


@app.patch("/admin/requests/{request_id}/job-details")
def update_job_details(
    request_id: int,
    request: JobDetailsUpdateRequest,
    admin=Depends(verify_admin),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM quote_requests WHERE id = %(request_id)s;",
                {"request_id": request_id},
            )
            existing = cur.fetchone()

            if not existing:
                raise HTTPException(status_code=404, detail="Quote request not found")

            if request.approve:
                cur.execute(
                    """
                    UPDATE quote_requests
                    SET
                        final_price = %(final_price)s,
                        deposit_paid = %(deposit_paid)s,
                        due_date = %(due_date)s,
                        print_notes = %(print_notes)s,
                        status = 'Approved'
                    WHERE id = %(request_id)s
                    RETURNING
                        id,
                        created_at,
                        name,
                        email,
                        phone,
                        project_description,
                        quantity,
                        approx_size,
                        deadline,
                        material_preference,
                        color_preference,
                        use_case,
                        requirements,
                        delivery_method,
                        shipping_location,
                        additional_notes,
                        uploaded_files,
                        ai_summary,
                        final_price,
                        deposit_paid,
                        due_date,
                        print_notes,
                        quoted_price,
                        quote_message,
                        quote_sent_at,
                        status;
                    """,
                    {
                        "request_id": request_id,
                        "final_price": request.final_price,
                        "deposit_paid": request.deposit_paid,
                        "due_date": request.due_date,
                        "print_notes": request.print_notes,
                    },
                )
            else:
                cur.execute(
                    """
                    UPDATE quote_requests
                    SET
                        final_price = %(final_price)s,
                        deposit_paid = %(deposit_paid)s,
                        due_date = %(due_date)s,
                        print_notes = %(print_notes)s
                    WHERE id = %(request_id)s
                    RETURNING
                        id,
                        created_at,
                        name,
                        email,
                        phone,
                        project_description,
                        quantity,
                        approx_size,
                        deadline,
                        material_preference,
                        color_preference,
                        use_case,
                        requirements,
                        delivery_method,
                        shipping_location,
                        additional_notes,
                        uploaded_files,
                        ai_summary,
                        final_price,
                        deposit_paid,
                        due_date,
                        print_notes,
                        quoted_price,
                        quote_message,
                        quote_sent_at,
                        status;
                    """,
                    {
                        "request_id": request_id,
                        "final_price": request.final_price,
                        "deposit_paid": request.deposit_paid,
                        "due_date": request.due_date,
                        "print_notes": request.print_notes,
                    },
                )

            updated = cur.fetchone()

    return {"success": True, "request": updated}

