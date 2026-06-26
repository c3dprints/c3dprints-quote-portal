import json
import math
import os
import re
import secrets
import struct
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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ai_triage import ai_triage_summary
from auth import check_admin_credentials, create_admin_token, verify_admin
from email_service import send_quote_notification
from pricing_engine import PricingSettings, calculate_quote

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "C3D Prints Quote Portal")
DATABASE_URL = os.getenv("DATABASE_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "quote-files")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://c3dprints-quote-portal.onrender.com").rstrip("/")
APPROVAL_NOTIFY_EMAIL = os.getenv("APPROVAL_NOTIFY_EMAIL", os.getenv("QUOTE_NOTIFY_EMAIL", "hi@c3dprints.com"))

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




MATERIAL_DENSITY_G_CM3 = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.20}

def guess_material_density(material_preference: Optional[str]) -> float:
    material = (material_preference or "PLA").upper()
    if "PETG" in material: return MATERIAL_DENSITY_G_CM3["PETG"]
    if "ABS" in material: return MATERIAL_DENSITY_G_CM3["ABS"]
    if "ASA" in material: return MATERIAL_DENSITY_G_CM3["ASA"]
    if "TPU" in material or "FLEX" in material: return MATERIAL_DENSITY_G_CM3["TPU"]
    return MATERIAL_DENSITY_G_CM3["PLA"]

def triangle_signed_volume(a, b, c) -> float:
    return (a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0]))/6.0

def triangle_area(a, b, c) -> float:
    ab=(b[0]-a[0],b[1]-a[1],b[2]-a[2]); ac=(c[0]-a[0],c[1]-a[1],c[2]-a[2])
    cross=(ab[1]*ac[2]-ab[2]*ac[1],ab[2]*ac[0]-ab[0]*ac[2],ab[0]*ac[1]-ab[1]*ac[0])
    return 0.5*math.sqrt(cross[0]**2+cross[1]**2+cross[2]**2)

def analyze_binary_stl(raw: bytes) -> Optional[dict]:
    if len(raw) < 84: return None
    tri_count = struct.unpack("<I", raw[80:84])[0]
    if 84 + tri_count * 50 > len(raw): return None
    min_x=min_y=min_z=float("inf"); max_x=max_y=max_z=float("-inf")
    signed_volume=0.0; surface_area=0.0; offset=84
    for _ in range(tri_count):
        offset += 12
        verts=[]
        for _ in range(3):
            x,y,z=struct.unpack("<fff", raw[offset:offset+12]); offset += 12
            verts.append((x,y,z))
            min_x,min_y,min_z=min(min_x,x),min(min_y,y),min(min_z,z)
            max_x,max_y,max_z=max(max_x,x),max(max_y,y),max(max_z,z)
        offset += 2
        signed_volume += triangle_signed_volume(verts[0],verts[1],verts[2])
        surface_area += triangle_area(verts[0],verts[1],verts[2])
    if tri_count <= 0 or min_x == float("inf"): return None
    return {"triangle_count":tri_count,"bbox":{"x":max_x-min_x,"y":max_y-min_y,"z":max_z-min_z},"volume_units3":abs(signed_volume),"surface_area_units2":surface_area}

def analyze_ascii_stl(raw: bytes) -> Optional[dict]:
    text=raw.decode("utf-8", errors="ignore")
    if "vertex" not in text.lower(): return None
    verts=[]
    for line in text.splitlines():
        p=line.strip().split()
        if len(p)==4 and p[0].lower()=="vertex":
            try: verts.append((float(p[1]),float(p[2]),float(p[3])))
            except ValueError: pass
    if len(verts)<3: return None
    min_x=min(v[0] for v in verts); min_y=min(v[1] for v in verts); min_z=min(v[2] for v in verts)
    max_x=max(v[0] for v in verts); max_y=max(v[1] for v in verts); max_z=max(v[2] for v in verts)
    signed_volume=0.0; surface_area=0.0; tri_count=len(verts)//3
    for i in range(0, tri_count*3, 3):
        a,b,c=verts[i],verts[i+1],verts[i+2]
        signed_volume += triangle_signed_volume(a,b,c)
        surface_area += triangle_area(a,b,c)
    return {"triangle_count":tri_count,"bbox":{"x":max_x-min_x,"y":max_y-min_y,"z":max_z-min_z},"volume_units3":abs(signed_volume),"surface_area_units2":surface_area}

def estimate_print_hours_from_stl(volume_cm3: float, height_mm: float, complexity: str) -> float:
    rate = 8.0 if complexity=="Low" else 6.5 if complexity=="Medium" else 5.0
    return round(max((volume_cm3/rate) + max(0,height_mm-30)/60 + 0.5, 0.5), 2)

def analyze_stl_bytes(raw: bytes, filename: str, material_preference: Optional[str]) -> Optional[dict]:
    if not filename.lower().endswith(".stl"): return None
    parsed = analyze_binary_stl(raw) or analyze_ascii_stl(raw)
    if not parsed: return {"filename":filename,"error":"Could not parse STL file."}
    bbox=parsed["bbox"]; x,y,z=bbox["x"],bbox["y"],bbox["z"]
    volume_cm3=parsed["volume_units3"]/1000.0; surface_area_cm2=parsed["surface_area_units2"]/100.0
    density=guess_material_density(material_preference)
    grams=volume_cm3*density
    largest=max(x,y,z)
    complexity="High" if parsed["triangle_count"]>100000 or z>120 or largest>220 else "Medium" if parsed["triangle_count"]>25000 or z>60 or largest>120 else "Low"
    return {
        "filename":filename,
        "type":"stl_analysis_v1",
        "units_assumed":"mm",
        "triangle_count":parsed["triangle_count"],
        "dimensions_mm":{"x":round(x,2),"y":round(y,2),"z":round(z,2)},
        "volume_cm3":round(volume_cm3,2),
        "surface_area_cm2":round(surface_area_cm2,2),
        "material_density_g_cm3":density,
        "estimated_grams_solid":round(grams,1),
        "estimated_hours_rough":estimate_print_hours_from_stl(volume_cm3,z,complexity),
        "complexity":complexity,
        "complexity_multiplier":{"Low":1.0,"Medium":1.25,"High":1.5}.get(complexity,1.25),
        "fail_rate":{"Low":20,"Medium":30,"High":45}.get(complexity,30),
        "warning":"Rough estimate only. True print time and grams require slicer settings, infill, layer height, supports, and orientation."
    }


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
    try:
        file.file.seek(0)
    except Exception:
        pass
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
                    quote_sent_at TIMESTAMPTZ,
                    approval_token TEXT UNIQUE,
                    approved_at TIMESTAMPTZ,
                    approval_notes TEXT
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
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS approval_token TEXT UNIQUE;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS approval_notes TEXT;")



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





def create_approval_token() -> str:
    return secrets.token_urlsafe(32)


def get_or_create_approval_token(request_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_token FROM quote_requests WHERE id = %(request_id)s;", {"request_id": request_id})
            row = cur.fetchone()

            if not row:
                raise HTTPException(status_code=404, detail="Quote request not found")

            if row.get("approval_token"):
                return row["approval_token"]

            token = create_approval_token()
            cur.execute(
                """
                UPDATE quote_requests
                SET approval_token = %(approval_token)s
                WHERE id = %(request_id)s
                RETURNING approval_token;
                """,
                {"approval_token": token, "request_id": request_id},
            )
            return cur.fetchone()["approval_token"]


def html_escape(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#039;")


def render_customer_quote_page(row: dict) -> str:
    price = row.get("quoted_price") or row.get("final_price")
    price_text = f"${float(price):.2f}" if price is not None else "Price not listed"
    approved = bool(row.get("approved_at")) or row.get("status") == "Approved"
    quote_message = row.get("quote_message") or "No quote message was saved for this request."

    approval_status = (
        "<div class='approved'>This quote has already been approved.</div>"
        if approved
        else "<button id='approveBtn' onclick='approveQuote()'>Approve Quote</button>"
    )

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>C3D Prints Quote #{html_escape(row.get("id"))}</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
:root {{--bg:#0b1623;--card:#162236;--input:#0d1928;--border:#1e3550;--blue:#33ccff;--orange:#ff8c3a;--green:#00e890;--text:#ddeeff;--muted:#8aa8c5;--red:#ff3d5a;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Arial,sans-serif;padding:24px}}
.wrap{{max-width:760px;margin:0 auto}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:22px;margin-bottom:16px}}
h1{{color:var(--blue);margin:0 0 8px}}
p{{line-height:1.5;color:var(--muted)}}
.price{{font-size:34px;font-weight:bold;color:var(--green);margin:14px 0}}
.detail-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.detail{{background:var(--input);border:1px solid var(--border);border-radius:12px;padding:12px}}
.detail label{{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}}
.detail strong{{color:var(--text)}}
.quote{{background:var(--input);border:1px solid var(--border);border-radius:12px;padding:16px;white-space:pre-wrap;line-height:1.5}}
button{{width:100%;background:linear-gradient(135deg,#008f5f,#00e890);color:#06140d;border:none;border-radius:14px;padding:16px;font-size:17px;font-weight:bold;cursor:pointer}}
button:disabled{{opacity:.6;cursor:wait}}
.approved,.success{{background:rgba(0,232,144,.12);border:1px solid rgba(0,232,144,.35);color:var(--green);border-radius:12px;padding:14px;text-align:center;font-weight:bold}}
.success{{display:none;margin-top:12px}}
.error{{background:rgba(255,61,90,.12);border:1px solid rgba(255,61,90,.35);color:var(--red);border-radius:12px;padding:14px;margin-top:12px;display:none}}
small{{color:var(--muted)}}
@media(max-width:640px){{.detail-grid{{grid-template-columns:1fr}}body{{padding:14px}}}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>C3D Prints Quote #{html_escape(row.get("id"))}</h1>
    <p>Please review your custom quote below. Approving this quote lets C3D Prints know you’re ready to move forward. Payment will still be handled separately through Shopify or Etsy.</p>
    <div class="price">{html_escape(price_text)}</div>
    <div class="detail-grid">
      <div class="detail"><label>Customer</label><strong>{html_escape(row.get("name"))}</strong></div>
      <div class="detail"><label>Status</label><strong>{html_escape(row.get("status"))}</strong></div>
      <div class="detail"><label>Quantity</label><strong>{html_escape(row.get("quantity"))}</strong></div>
      <div class="detail"><label>Material</label><strong>{html_escape(row.get("material_preference") or "Not specified")}</strong></div>
    </div>
  </div>
  <div class="card">
    <h2 style="color:var(--orange);margin-top:0;">Quote Details</h2>
    <div class="quote">{html_escape(quote_message)}</div>
  </div>
  <div class="card">
    {approval_status}
    <div id="success" class="success">Thank you — your quote has been approved. C3D Prints will follow up with your Shopify/Etsy checkout link.</div>
    <div id="error" class="error">Something went wrong. Please contact C3D Prints directly.</div>
    <p><small>Approving this quote does not process payment. Payment will be completed separately.</small></p>
  </div>
</div>
<script>
async function approveQuote(){{
  const btn=document.getElementById("approveBtn");
  const success=document.getElementById("success");
  const error=document.getElementById("error");
  if(btn){{btn.disabled=true;btn.textContent="Approving...";}}
  success.style.display="none"; error.style.display="none";
  try{{
    const response=await fetch(window.location.pathname+"/approve",{{method:"POST"}});
    if(!response.ok)throw new Error("Approval failed");
    success.style.display="block";
    if(btn){{btn.textContent="Approved";}}
  }}catch(e){{
    error.style.display="block";
    if(btn){{btn.disabled=false;btn.textContent="Approve Quote";}}
  }}
}}
</script>
</body>
</html>
"""


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
            if not file.filename:
                continue

            raw_for_analysis = file.file.read()
            try:
                file.file.seek(0)
            except Exception:
                pass

            stl_analysis = analyze_stl_bytes(raw_for_analysis, file.filename, material_preference)

            if storage_enabled():
                stored = upload_file_to_supabase_storage(request_id, file)
                if stored:
                    if stl_analysis:
                        stored["stl_analysis"] = stl_analysis
                    stored_files.append(stored)
            else:
                metadata = {"filename": file.filename, "original_filename": file.filename, "storage_path": None}
                if stl_analysis:
                    metadata["stl_analysis"] = stl_analysis
                stored_files.append(metadata)

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
                    approval_token,
                    approved_at,
                    approval_notes,
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
                    approval_token,
                    approved_at,
                    approval_notes,
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
                        approval_token,
                        approved_at,
                        approval_notes,
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
                        approval_token,
                        approved_at,
                        approval_notes,
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
            approval_token = get_or_create_approval_token(request_id)
            approval_link = f"{PUBLIC_BASE_URL}/quote/{approval_token}"
            message_with_link = f"{message}\n\nReview and approve your quote here:\n{approval_link}\n\nPayment will be completed separately through Shopify or Etsy."
            html_body = f"""<div style="font-family:Arial,sans-serif;line-height:1.5;color:#111;"><pre style="white-space:pre-wrap;font-family:Arial,sans-serif;">{html_escape(message)}</pre><p><a href="{approval_link}" style="display:inline-block;background:#00aaff;color:white;padding:12px 16px;border-radius:8px;text-decoration:none;font-weight:bold;">Review and Approve Quote</a></p><p>Payment will be completed separately through Shopify or Etsy.</p></div>"""
            email_result = send_quote_notification(to_email=quote_request["email"], subject=subject, html_body=html_body, text_body=message_with_link)
            if not email_result.get("sent"):
                raise HTTPException(status_code=500, detail=f"Email failed: {email_result.get('reason', 'Unknown error')}")
            cur.execute("""
                UPDATE quote_requests
                SET status = 'Quoted', quoted_price = %(quoted_price)s, quote_message = %(quote_message)s, quote_sent_at = %(quote_sent_at)s, approval_token = %(approval_token)s
                WHERE id = %(request_id)s
                RETURNING id, created_at, name, email, phone, project_description, quantity, approx_size, deadline,
                    material_preference, color_preference, use_case, requirements, delivery_method, shipping_location,
                    additional_notes, uploaded_files, ai_summary, final_price, deposit_paid, due_date, print_notes,
                    actual_cost, profit_notes, quoted_price, quote_message, quote_sent_at, approval_token, approved_at, approval_notes, status;
            """, {"request_id": request_id, "quoted_price": request.quoted_price, "quote_message": message, "quote_sent_at": datetime.now(timezone.utc), "approval_token": approval_token})
            updated = cur.fetchone()
    return {"success": True, "email": email_result, "request": updated}


@app.get("/quote/{approval_token}", response_class=HTMLResponse)
def public_quote_review(approval_token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, email, quantity, material_preference, project_description,
                       quoted_price, final_price, quote_message, status, approved_at, approval_notes
                FROM quote_requests
                WHERE approval_token = %(approval_token)s;
                """,
                {"approval_token": approval_token},
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Quote not found")
    return HTMLResponse(render_customer_quote_page(row))


@app.post("/quote/{approval_token}/approve")
def public_quote_approve(approval_token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quote_requests
                SET status = 'Approved',
                    approved_at = COALESCE(approved_at, %(approved_at)s),
                    approval_notes = COALESCE(approval_notes, 'Customer approved quote through approval portal.')
                WHERE approval_token = %(approval_token)s
                RETURNING id, name, email, phone, project_description, quantity,
                          material_preference, quoted_price, final_price,
                          quote_message, status, approved_at;
                """,
                {"approval_token": approval_token, "approved_at": datetime.now(timezone.utc)},
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Quote not found")

    price = row.get("quoted_price") or row.get("final_price")
    price_text = f"${float(price):.2f}" if price is not None else "No price listed"
    admin_url = f"{PUBLIC_BASE_URL}/admin.html"

    html_body = f"""
    <h2>C3D Quote Approved</h2>
    <p><strong>Quote:</strong> #{row.get("id")}</p>
    <p><strong>Customer:</strong> {html_escape(row.get("name"))} &lt;{html_escape(row.get("email"))}&gt;</p>
    <p><strong>Phone:</strong> {html_escape(row.get("phone") or "Not provided")}</p>
    <p><strong>Price:</strong> {html_escape(price_text)}</p>
    <p><strong>Quantity:</strong> {html_escape(row.get("quantity"))}</p>
    <p><strong>Material:</strong> {html_escape(row.get("material_preference") or "Not specified")}</p>
    <p><strong>Status:</strong> Approved</p>
    <p><strong>Approved At:</strong> {html_escape(row.get("approved_at"))}</p>
    <h3>Project Description</h3>
    <p>{html_escape(row.get("project_description"))}</p>
    <h3>Next Step</h3>
    <p>Send the customer their Shopify or Etsy checkout link.</p>
    <p><a href="{admin_url}">Open C3D Admin Dashboard</a></p>
    """

    text_body = f"""C3D Quote Approved

Quote: #{row.get("id")}
Customer: {row.get("name")} <{row.get("email")}>
Phone: {row.get("phone") or "Not provided"}
Price: {price_text}
Quantity: {row.get("quantity")}
Material: {row.get("material_preference") or "Not specified"}
Status: Approved
Approved At: {row.get("approved_at")}

Project:
{row.get("project_description")}

Next step:
Send the customer their Shopify or Etsy checkout link.

Admin:
{admin_url}
"""

    try:
        email_result = send_quote_notification(
            to_email=APPROVAL_NOTIFY_EMAIL,
            subject=f"C3D Quote #{row.get('id')} Approved — {row.get('name')}",
            html_body=html_body,
            text_body=text_body,
        )
    except Exception as exc:
        email_result = {"sent": False, "reason": str(exc)}

    return {
        "success": True,
        "request_id": row["id"],
        "status": row["status"],
        "approved_at": row["approved_at"],
        "notification": email_result,
    }

