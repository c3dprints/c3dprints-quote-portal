import json
import os
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import psycopg
import requests
from psycopg.rows import dict_row
from psycopg.types.json import Json

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

from ai_triage import ai_triage_summary
from auth import check_admin_credentials, create_admin_token, verify_admin
from email_service import send_quote_notification
from pricing_engine import PricingSettings, calculate_quote

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "C3D Prints Quote Portal")
AI_QUOTE_MODEL = os.getenv("AI_QUOTE_MODEL", "gpt-4o-mini")
DATABASE_URL = os.getenv("DATABASE_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "quote-files")

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



def safe_storage_filename(filename: str) -> str:
    name = os.path.basename(filename or "uploaded-file")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "uploaded-file"


def storage_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPABASE_STORAGE_BUCKET)


def upload_file_to_supabase_storage(request_id: int, file: UploadFile) -> Optional[dict]:
    if not storage_enabled():
        return None
    filename = safe_storage_filename(file.filename)
    storage_path = f"quote-requests/{request_id}/{filename}"
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{storage_path}"
    contents = file.file.read()
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "x-upsert": "true",
        "Content-Type": file.content_type or "application/octet-stream",
    }
    response = requests.post(upload_url, headers=headers, data=contents, timeout=60)
    if response.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"File upload failed for {filename}: {response.text}")
    return {
        "filename": filename,
        "original_filename": file.filename,
        "storage_path": storage_path,
        "bucket": SUPABASE_STORAGE_BUCKET,
        "content_type": file.content_type,
        "size_bytes": len(contents),
    }


def create_signed_file_url(storage_path: str, expires_in: int = 3600) -> str:
    if not storage_enabled():
        raise HTTPException(status_code=500, detail="Supabase Storage is not configured")
    sign_url = f"{SUPABASE_URL}/storage/v1/object/sign/{SUPABASE_STORAGE_BUCKET}/{storage_path}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
    }
    response = requests.post(sign_url, headers=headers, json={"expiresIn": expires_in}, timeout=30)
    if response.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Could not create signed file URL: {response.text}")
    data = response.json()
    signed_url = data.get("signedURL") or data.get("signedUrl") or data.get("signed_url")
    if not signed_url:
        raise HTTPException(status_code=500, detail="Supabase did not return a signed URL")
    return signed_url if signed_url.startswith("http") else f"{SUPABASE_URL}/storage/v1{signed_url}"


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
                    actual_cost NUMERIC,
                    profit_notes TEXT,
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
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS actual_cost NUMERIC;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS profit_notes TEXT;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS quoted_price NUMERIC;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS quote_message TEXT;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS quote_sent_at TIMESTAMPTZ;")



def save_request(data: dict, ai_summary: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quote_requests (
                    created_at, name, email, phone, project_description, quantity,
                    approx_size, deadline, material_preference, color_preference,
                    use_case, requirements, delivery_method, shipping_location,
                    additional_notes, uploaded_files, ai_summary, status
                )
                VALUES (
                    %(created_at)s, %(name)s, %(email)s, %(phone)s,
                    %(project_description)s, %(quantity)s, %(approx_size)s,
                    %(deadline)s, %(material_preference)s, %(color_preference)s,
                    %(use_case)s, %(requirements)s, %(delivery_method)s,
                    %(shipping_location)s, %(additional_notes)s, %(uploaded_files)s,
                    %(ai_summary)s, %(status)s
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
            return cur.fetchone()["id"]


def update_request_files(request_id: int, uploaded_files: list) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE quote_requests SET uploaded_files = %(uploaded_files)s WHERE id = %(request_id)s;",
                {"uploaded_files": Json(uploaded_files), "request_id": request_id},
            )




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
            "storage_configured": storage_enabled(),
            "supabase_url_configured": bool(SUPABASE_URL),
            "supabase_service_key_configured": bool(SUPABASE_SERVICE_ROLE_KEY),
            "supabase_bucket": SUPABASE_STORAGE_BUCKET,
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

    stored_files = []
    if files:
        for file in files:
            if storage_enabled():
                stored = upload_file_to_supabase_storage(request_id, file)
                if stored:
                    stored_files.append(stored)
            else:
                stored_files.append({"filename": file.filename, "original_filename": file.filename, "storage_path": None})
    if stored_files:
        data["uploaded_files"] = stored_files
        update_request_files(request_id, stored_files)

    notify_email = os.getenv("QUOTE_NOTIFY_EMAIL", "hi@c3dprints.com")
    file_list = "<br>".join([(f.get("original_filename") or f.get("filename")) if isinstance(f, dict) else str(f) for f in data["uploaded_files"]]) if data["uploaded_files"] else "No files uploaded"

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
                    actual_cost,
                    profit_notes,
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
                    actual_cost,
                    profit_notes,
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
    actual_cost: Optional[float] = None
    profit_notes: Optional[str] = None
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
                        actual_cost = %(actual_cost)s,
                        profit_notes = %(profit_notes)s,
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
                        actual_cost,
                        profit_notes,
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
                        "actual_cost": request.actual_cost,
                        "profit_notes": request.profit_notes,
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
                        print_notes = %(print_notes)s,
                        actual_cost = %(actual_cost)s,
                        profit_notes = %(profit_notes)s
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
                        actual_cost,
                        profit_notes,
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
                        "actual_cost": request.actual_cost,
                        "profit_notes": request.profit_notes,
                    },
                )

            updated = cur.fetchone()

    return {"success": True, "request": updated}


@app.get("/admin/analytics")
def get_admin_analytics(admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)::int AS total_requests,
                    COUNT(*) FILTER (WHERE status = 'New')::int AS new_count,
                    COUNT(*) FILTER (WHERE status = 'Need Info')::int AS need_info_count,
                    COUNT(*) FILTER (WHERE status = 'Quoted')::int AS quoted_count,
                    COUNT(*) FILTER (WHERE status = 'Approved')::int AS approved_count,
                    COUNT(*) FILTER (WHERE status = 'Printing')::int AS printing_count,
                    COUNT(*) FILTER (WHERE status = 'Completed')::int AS completed_count,
                    COUNT(*) FILTER (WHERE status = 'Archived')::int AS archived_count,
                    COALESCE(SUM(quoted_price) FILTER (WHERE status = 'Quoted'), 0)::float AS quoted_open_value,
                    COALESCE(AVG(quoted_price) FILTER (WHERE quoted_price IS NOT NULL), 0)::float AS average_quote,
                    COALESCE(SUM(final_price) FILTER (WHERE status IN ('Approved','Printing')), 0)::float AS active_job_value,
                    COALESCE(SUM(final_price) FILTER (WHERE status IN ('Approved','Printing','Completed')), 0)::float AS revenue_tracked,
                    COALESCE(SUM(actual_cost) FILTER (WHERE status IN ('Approved','Printing','Completed')), 0)::float AS actual_cost_tracked,
                    COALESCE(SUM(final_price - COALESCE(actual_cost,0)) FILTER (WHERE status IN ('Approved','Printing','Completed') AND final_price IS NOT NULL), 0)::float AS estimated_profit
                FROM quote_requests;
            """)
            return cur.fetchone()

@app.get("/admin/requests/{request_id}/files/{file_index}/download")
def get_quote_file_download_url(request_id: int, file_index: int, admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT uploaded_files FROM quote_requests WHERE id = %(request_id)s;", {"request_id": request_id})
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Quote request not found")
    uploaded_files = row.get("uploaded_files") or []
    if file_index < 0 or file_index >= len(uploaded_files):
        raise HTTPException(status_code=404, detail="File not found")
    file_info = uploaded_files[file_index]
    if not isinstance(file_info, dict) or not file_info.get("storage_path"):
        raise HTTPException(status_code=404, detail="This file does not have cloud storage metadata")
    return {
        "success": True,
        "filename": file_info.get("original_filename") or file_info.get("filename"),
        "url": create_signed_file_url(file_info["storage_path"]),
        "expires_in_seconds": 3600,
    }

class SendQuoteRequest(BaseModel):
    subject: str = "Your C3D Prints Custom Quote"
    message: str
    quoted_price: Optional[float] = None


@app.post("/admin/requests/{request_id}/send-quote")
def send_quote_to_customer(request_id: int, request: SendQuoteRequest, admin=Depends(verify_admin)):
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Quote message cannot be empty")
    subject = request.subject.strip() or "Your C3D Prints Custom Quote"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email FROM quote_requests WHERE id = %(request_id)s;", {"request_id": request_id})
            quote_request = cur.fetchone()
            if not quote_request:
                raise HTTPException(status_code=404, detail="Quote request not found")
            html_body = f"""<div style="font-family:Arial,sans-serif;line-height:1.5;color:#111;"><pre style="white-space:pre-wrap;font-family:Arial,sans-serif;">{message}</pre></div>"""
            email_result = send_quote_notification(to_email=quote_request["email"], subject=subject, html_body=html_body, text_body=message)
            if not email_result.get("sent"):
                raise HTTPException(status_code=500, detail=f"Email failed: {email_result.get('reason', 'Unknown error')}")
            cur.execute("""
                UPDATE quote_requests
                SET status = 'Quoted', quoted_price = %(quoted_price)s, quote_message = %(quote_message)s, quote_sent_at = %(quote_sent_at)s
                WHERE id = %(request_id)s
                RETURNING id, created_at, name, email, phone, project_description, quantity, approx_size, deadline,
                    material_preference, color_preference, use_case, requirements, delivery_method, shipping_location,
                    additional_notes, uploaded_files, ai_summary, final_price, deposit_paid, due_date, print_notes,
                    actual_cost, profit_notes, quoted_price, quote_message, quote_sent_at, status;
            """, {"request_id": request_id, "quoted_price": request.quoted_price, "quote_message": message, "quote_sent_at": datetime.now(timezone.utc)})
            updated = cur.fetchone()
    return {"success": True, "email": email_result, "request": updated}
