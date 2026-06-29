import json
import base64
import hashlib
import hmac
import math
import os
import re
import secrets
from html import escape as html_escape
import struct
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import psycopg
import requests
from psycopg.rows import dict_row
from psycopg.types.json import Json

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ai_triage import ai_triage_summary, ai_quote_assist
from auth import check_admin_credentials, create_admin_token, verify_admin
from email_service import send_quote_notification
from pricing_engine import PricingSettings, calculate_quote

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "C3D Prints Quote Portal")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://c3dprints-quote-portal.onrender.com").rstrip("/")
# Absolute URL so it works in emails and server-rendered pages. Served by GitHub Pages from repo root.
LOGO_URL = os.getenv("LOGO_URL", "https://c3dprints.github.io/c3dprints-quote-portal/logo.png")


def _portal_secret() -> bytes:
    return os.getenv("JWT_SECRET", "c3d-portal-fallback-secret").encode()


def make_portal_token(email: str) -> str:
    """Stateless, stable per-customer token: base64(email).hmac_sig. No DB column needed."""
    e = (email or "").strip().lower()
    sig = hmac.new(_portal_secret(), e.encode(), hashlib.sha256).hexdigest()[:16]
    payload = base64.urlsafe_b64encode(e.encode()).decode().rstrip("=")
    return f"{payload}.{sig}"


def read_portal_token(token: str) -> Optional[str]:
    """Verify a portal token and return its email, or None if tampered/invalid."""
    try:
        payload, sig = token.rsplit(".", 1)
        pad = "=" * (-len(payload) % 4)
        email = base64.urlsafe_b64decode(payload + pad).decode().strip().lower()
        expected = hmac.new(_portal_secret(), email.encode(), hashlib.sha256).hexdigest()[:16]
        return email if hmac.compare_digest(sig, expected) else None
    except Exception:
        return None


def portal_url_for(email: str) -> str:
    return f"{PUBLIC_BASE_URL}/portal/{make_portal_token(email)}"


def portal_footer_html(email: str) -> str:
    if not email:
        return ""
    return (
        f"<p style='text-align:center;margin-top:22px;font-size:13px;'>"
        f"<a href='{portal_url_for(email)}' style='color:#1a73e8;font-weight:bold;text-decoration:none;'>"
        f"View all my quotes &amp; orders</a></p>"
    )
DATABASE_URL = os.getenv("DATABASE_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "quote-files")
ETSY_CHECKOUT_URL = os.getenv("ETSY_CHECKOUT_URL", "https://c3dprintsofficial.etsy.com/listing/1249586363")
SHOPIFY_CHECKOUT_URL = os.getenv("SHOPIFY_CHECKOUT_URL", "https://c3dprints.com/products/custom-3d-printed-cosplay-personalized-character-art-fan-art")

VALID_STATUSES = {
    "New",
    "Need Info",
    "Quoted",
    "Approved",
    "Awaiting Payment",
    "Paid",
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

def analyze_obj_bytes(raw: bytes) -> Optional[dict]:
    text=raw.decode("utf-8", errors="ignore")
    verts=[]; faces=[]
    for line in text.splitlines():
        line=line.strip()
        if not line or line[0]=="#": continue
        parts=line.split()
        tag=parts[0]
        if tag=="v" and len(parts)>=4:
            try: verts.append((float(parts[1]),float(parts[2]),float(parts[3])))
            except ValueError: pass
        elif tag=="f" and len(parts)>=4:
            idxs=[]
            for token in parts[1:]:
                ref=token.split("/")[0]
                if not ref: continue
                try: i=int(ref)
                except ValueError: continue
                if i<0: i=len(verts)+i+1   # OBJ negative indices are relative
                idxs.append(i)
            # Fan-triangulate any n-gon face into triangles.
            for k in range(1, len(idxs)-1):
                faces.append((idxs[0], idxs[k], idxs[k+1]))
    if len(verts)<3 or not faces: return None
    min_x=min(v[0] for v in verts); min_y=min(v[1] for v in verts); min_z=min(v[2] for v in verts)
    max_x=max(v[0] for v in verts); max_y=max(v[1] for v in verts); max_z=max(v[2] for v in verts)
    signed_volume=0.0; surface_area=0.0; tri_count=0
    n=len(verts)
    for a,b,c in faces:
        if not (1<=a<=n and 1<=b<=n and 1<=c<=n): continue
        A,B,C=verts[a-1],verts[b-1],verts[c-1]
        signed_volume += triangle_signed_volume(A,B,C)
        surface_area += triangle_area(A,B,C)
        tri_count += 1
    if tri_count==0: return None
    return {"triangle_count":tri_count,"bbox":{"x":max_x-min_x,"y":max_y-min_y,"z":max_z-min_z},"volume_units3":abs(signed_volume),"surface_area_units2":surface_area}

def estimate_print_hours_from_stl(volume_cm3: float, height_mm: float, complexity: str) -> float:
    rate = 8.0 if complexity=="Low" else 6.5 if complexity=="Medium" else 5.0
    return round(max((volume_cm3/rate) + max(0,height_mm-30)/60 + 0.5, 0.5), 2)

def analyze_model_bytes(raw: bytes, filename: str, material_preference: Optional[str]) -> Optional[dict]:
    name=filename.lower()
    if name.endswith(".stl"):
        source_format="stl"
        parsed = analyze_binary_stl(raw) or analyze_ascii_stl(raw)
    elif name.endswith(".obj"):
        source_format="obj"
        parsed = analyze_obj_bytes(raw)
    else:
        return None
    if not parsed: return {"filename":filename,"error":f"Could not parse {source_format.upper()} file."}
    bbox=parsed["bbox"]; x,y,z=bbox["x"],bbox["y"],bbox["z"]
    volume_cm3=parsed["volume_units3"]/1000.0; surface_area_cm2=parsed["surface_area_units2"]/100.0
    density=guess_material_density(material_preference)
    grams=volume_cm3*density
    # Solid volume hugely overestimates real material use. Apply a rough infill factor
    # (walls + sparse infill) so pricing isn't wildly high. Admin can still adjust.
    infill_factor=float(os.getenv("DEFAULT_INFILL_FACTOR", 0.45))
    grams_infill=grams*infill_factor
    largest=max(x,y,z)
    complexity="High" if parsed["triangle_count"]>100000 or z>120 or largest>220 else "Medium" if parsed["triangle_count"]>25000 or z>60 or largest>120 else "Low"
    return {
        "filename":filename,
        "type":"stl_analysis_v1",
        "source_format":source_format,
        "units_assumed":"mm",
        "triangle_count":parsed["triangle_count"],
        "dimensions_mm":{"x":round(x,2),"y":round(y,2),"z":round(z,2)},
        "volume_cm3":round(volume_cm3,2),
        "surface_area_cm2":round(surface_area_cm2,2),
        "material_density_g_cm3":density,
        "estimated_grams_solid":round(grams,1),
        "infill_factor":infill_factor,
        "estimated_grams":round(grams_infill,1),
        "estimated_hours_rough":estimate_print_hours_from_stl(volume_cm3,z,complexity),
        "complexity":complexity,
        "complexity_multiplier":{"Low":1.0,"Medium":1.25,"High":1.5}.get(complexity,1.25),
        "fail_rate":{"Low":20,"Medium":30,"High":45}.get(complexity,30),
        "warning":"Rough estimate only. True print time and grams require slicer settings, infill, layer height, supports, and orientation."
    }


# Back-compat: older call sites and tests reference analyze_stl_bytes.
analyze_stl_bytes = analyze_model_bytes


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
                    checkout_platform TEXT,
                    checkout_url TEXT,
                    checkout_sent_at TIMESTAMPTZ,
                    paid BOOLEAN NOT NULL DEFAULT FALSE,
                    paid_at TIMESTAMPTZ,
                    tracking_token TEXT UNIQUE,
                    customer_status_note TEXT,
                    ai_quote_assist TEXT,
                    ai_quote_structured JSONB DEFAULT '{}'::jsonb
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
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS ai_quote_assist TEXT;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS ai_quote_structured JSONB DEFAULT '{}'::jsonb;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS assigned_printer_id BIGINT;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS printers (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Available',
                    notes TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("ALTER TABLE printers ADD COLUMN IF NOT EXISTS serial TEXT;")
            cur.execute("ALTER TABLE printers ADD COLUMN IF NOT EXISTS live_state TEXT;")
            cur.execute("ALTER TABLE printers ADD COLUMN IF NOT EXISTS progress INTEGER;")
            cur.execute("ALTER TABLE printers ADD COLUMN IF NOT EXISTS remaining_min INTEGER;")
            cur.execute("ALTER TABLE printers ADD COLUMN IF NOT EXISTS last_report_at TIMESTAMPTZ;")



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
            if not file.filename:
                continue

            raw_for_analysis = file.file.read()
            try:
                file.file.seek(0)
            except Exception:
                pass

            stl_analysis = analyze_model_bytes(raw_for_analysis, file.filename, material_preference)

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
                    ai_quote_assist,
                    ai_quote_structured,
                    final_price,
                    deposit_paid,
                    due_date,
                    print_notes,
                    actual_cost,
                    profit_notes,
                    quoted_price,
                    quote_message,
                    quote_sent_at,
                    checkout_platform,
                    checkout_url,
                    checkout_sent_at,
                    paid,
                    paid_at,
                    tracking_token,
                    customer_status_note,
                    status
                FROM quote_requests
                ORDER BY created_at DESC
                LIMIT 100;
                """
            )
            return cur.fetchall()


@app.post("/admin/requests/{request_id}/ai-quote-assist")
def generate_ai_quote_assist(request_id: int, admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, email, phone, project_description, quantity,
                       approx_size, deadline, material_preference, color_preference,
                       use_case, requirements, delivery_method, shipping_location,
                       additional_notes, uploaded_files
                FROM quote_requests
                WHERE id = %(request_id)s;
                """,
                {"request_id": request_id},
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Quote request not found")

            data = {
                "name": row["name"],
                "email": row["email"],
                "phone": row["phone"],
                "project_description": row["project_description"],
                "quantity": row["quantity"],
                "approx_size": row["approx_size"],
                "deadline": row["deadline"],
                "material_preference": row["material_preference"],
                "color_preference": row["color_preference"],
                "use_case": row["use_case"],
                "requirements": row["requirements"] or [],
                "delivery_method": row["delivery_method"],
                "shipping_location": row["shipping_location"],
                "additional_notes": row["additional_notes"],
                "uploaded_files": row["uploaded_files"] or [],
            }

            result = ai_quote_assist(data)

            cur.execute(
                """
                UPDATE quote_requests
                SET ai_quote_assist = %(ai_quote_assist)s,
                    ai_quote_structured = %(ai_quote_structured)s
                WHERE id = %(request_id)s
                RETURNING
                    id, created_at, name, email, phone, project_description, quantity,
                    approx_size, deadline, material_preference, color_preference,
                    use_case, requirements, delivery_method, shipping_location,
                    additional_notes, uploaded_files, ai_summary, ai_quote_assist,
                    ai_quote_structured, final_price, deposit_paid, due_date,
                    print_notes, actual_cost, profit_notes, quoted_price,
                    quote_message, quote_sent_at, checkout_platform, checkout_url,
                    checkout_sent_at, paid, paid_at, tracking_token,
                    customer_status_note, status;
                """,
                {
                    "ai_quote_assist": result["text"],
                    "ai_quote_structured": Json(result["structured"]),
                    "request_id": request_id,
                },
            )
            updated = cur.fetchone()

    return {
        "success": True,
        "ai_quote_assist": result["text"],
        "request": updated,
    }


def pick_best_stl_analysis(uploaded_files) -> Optional[dict]:
    """Return the first usable STL analysis from a request's uploaded files."""
    for f in (uploaded_files or []):
        if isinstance(f, dict):
            a = f.get("stl_analysis")
            if isinstance(a, dict) and not a.get("error"):
                return a
    return None


@app.post("/admin/requests/{request_id}/auto-price")
def auto_price_from_stl(request_id: int, admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, quantity, uploaded_files FROM quote_requests WHERE id = %(id)s;",
                {"id": request_id},
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Quote request not found")

    analysis = pick_best_stl_analysis(row.get("uploaded_files"))
    if not analysis:
        raise HTTPException(status_code=400, detail="No STL analysis available. Upload an .stl file to auto-price.")

    # Prefer the infill-adjusted grams; fall back to applying infill to solid for
    # older records analyzed before estimated_grams existed.
    infill = float(os.getenv("DEFAULT_INFILL_FACTOR", 0.45))
    grams = analysis.get("estimated_grams")
    if grams is None:
        solid = analysis.get("estimated_grams_solid")
        grams = round(solid * infill, 1) if solid else None
    hours = analysis.get("estimated_hours_rough")
    if not grams or not hours or grams <= 0 or hours <= 0:
        raise HTTPException(status_code=400, detail="STL analysis is missing usable grams/hours to price.")

    quantity = max(1, int(row.get("quantity") or 1))
    fail_rate = analysis.get("fail_rate", 30)
    complexity_multiplier = analysis.get("complexity_multiplier", 1.25)

    try:
        quote = calculate_quote(
            grams=grams,
            hours=hours,
            quantity=quantity,
            fail_rate=fail_rate,
            complexity_multiplier=complexity_multiplier,
            settings=get_pricing_settings(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "success": True,
        "source": "stl_analysis",
        "inputs": {
            "grams": grams,
            "hours": hours,
            "fail_rate": fail_rate,
            "complexity_multiplier": complexity_multiplier,
            "quantity": quantity,
        },
        "stl": {
            "filename": analysis.get("filename"),
            "dimensions_mm": analysis.get("dimensions_mm"),
            "volume_cm3": analysis.get("volume_cm3"),
            "estimated_grams_solid": analysis.get("estimated_grams_solid"),
            "infill_factor": analysis.get("infill_factor", infill),
            "complexity": analysis.get("complexity"),
        },
        "quote": quote,
        "recommended_price": quote["totals"]["suggested_sell_price"],
    }


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
                    COALESCE(SUM(final_price - COALESCE(actual_cost,0)) FILTER (WHERE status IN ('Approved','Printing','Completed') AND final_price IS NOT NULL), 0)::float AS estimated_profit,
                    COUNT(*) FILTER (WHERE status = 'Reviewing')::int AS reviewing_count,
                    COUNT(*) FILTER (WHERE status = 'Awaiting Payment')::int AS awaiting_payment_count,
                    COUNT(*) FILTER (WHERE status = 'Paid')::int AS paid_count,
                    COUNT(*) FILTER (WHERE quote_sent_at IS NOT NULL)::int AS quotes_sent_count,
                    COUNT(*) FILTER (WHERE status IN ('Approved','Awaiting Payment','Paid','Printing','Completed'))::int AS approved_or_beyond_count,
                    COUNT(*) FILTER (WHERE paid = TRUE)::int AS paid_orders_count,
                    COALESCE(SUM(COALESCE(final_price, quoted_price)) FILTER (WHERE paid = TRUE), 0)::float AS collected_revenue
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
            cur.execute("SELECT id, name, email, approval_token FROM quote_requests WHERE id = %(request_id)s;", {"request_id": request_id})
            quote_request = cur.fetchone()
            if not quote_request:
                raise HTTPException(status_code=404, detail="Quote request not found")
            approval_token = quote_request.get("approval_token") or secrets.token_urlsafe(24)
            approval_url = f"{PUBLIC_BASE_URL}/approve/{approval_token}"
            html_body = (
                f"""<div style="font-family:Arial,sans-serif;line-height:1.5;color:#111;max-width:600px;margin:auto;">"""
                f"""<div style="text-align:center;margin-bottom:18px;">"""
                f"""<img src="{LOGO_URL}" alt="C3D Prints" width="120" style="width:120px;height:auto;">"""
                f"""</div>"""
                f"""<pre style="white-space:pre-wrap;font-family:Arial,sans-serif;">{message}</pre>"""
                f"""{approval_button_html(approval_token)}"""
                f"""{portal_footer_html(quote_request["email"])}"""
                f"""</div>"""
            )
            text_body = f"{message}\n\nApprove this quote: {approval_url}"
            email_result = send_quote_notification(to_email=quote_request["email"], subject=subject, html_body=html_body, text_body=text_body)
            if not email_result.get("sent"):
                raise HTTPException(status_code=500, detail=f"Email failed: {email_result.get('reason', 'Unknown error')}")
            cur.execute("""
                UPDATE quote_requests
                SET status = 'Quoted', quoted_price = %(quoted_price)s, quote_message = %(quote_message)s,
                    quote_sent_at = %(quote_sent_at)s, approval_token = %(approval_token)s
                WHERE id = %(request_id)s
                RETURNING id, created_at, name, email, phone, project_description, quantity, approx_size, deadline,
                    material_preference, color_preference, use_case, requirements, delivery_method, shipping_location,
                    additional_notes, uploaded_files, ai_summary, final_price, deposit_paid, due_date, print_notes,
                    actual_cost, profit_notes, quoted_price, quote_message, quote_sent_at, status;
            """, {"request_id": request_id, "quoted_price": request.quoted_price, "quote_message": message, "quote_sent_at": datetime.now(timezone.utc), "approval_token": approval_token})
            updated = cur.fetchone()
    return {"success": True, "email": email_result, "request": updated}


@app.on_event("startup")
def ensure_checkout_schema_and_statuses():
    if not DATABASE_URL:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS checkout_platform TEXT;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS checkout_url TEXT;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS checkout_sent_at TIMESTAMPTZ;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS paid BOOLEAN NOT NULL DEFAULT FALSE;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS tracking_token TEXT UNIQUE;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS customer_status_note TEXT;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS approval_token TEXT UNIQUE;")
            cur.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;")

            cur.execute("""
                ALTER TABLE quote_requests
                DROP CONSTRAINT IF EXISTS quote_requests_status_check;
            """)
            cur.execute("""
                ALTER TABLE quote_requests
                ADD CONSTRAINT quote_requests_status_check
                CHECK (status IN (
                    'New',
                    'Reviewing',
                    'Need Info',
                    'Quoted',
                    'Approved',
                    'Awaiting Payment',
                    'Paid',
                    'Printing',
                    'Completed',
                    'Archived'
                ));
            """)


class CheckoutLinkRequest(BaseModel):
    platform: str = "both"


class PaymentStatusRequest(BaseModel):
    paid: bool = True


@app.post("/admin/requests/{request_id}/send-checkout")
def send_checkout_link(request_id: int, request: CheckoutLinkRequest, admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, email, quoted_price, final_price, project_description, tracking_token
                FROM quote_requests
                WHERE id = %(request_id)s;
                """,
                {"request_id": request_id},
            )
            row = cur.fetchone()

            if not row:
                raise HTTPException(status_code=404, detail="Quote request not found")

            price = row.get("quoted_price") or row.get("final_price")
            price_text = f"${float(price):.2f}" if price is not None else "your approved quote amount"
            first_name = (row.get("name") or "there").split(" ")[0]
            tracking_token = row.get("tracking_token") or create_tracking_token()
            tracking_url = f"{PUBLIC_BASE_URL}/track/{tracking_token}"

            text_body = f"""Hi {first_name},

Thanks for approving your custom C3D Prints quote.

You can complete checkout using either option below:

Shop on C3DPRINTS.COM:
{SHOPIFY_CHECKOUT_URL}

Shop on Etsy:
{ETSY_CHECKOUT_URL}

Please use your approved quote amount: {price_text}

Once checkout is completed, your order will move into the print queue.

Track your order status anytime here:
{tracking_url}

Thank you,
C3D Prints
"""

            html_body = f"""
            <div style="font-family:Arial,sans-serif;line-height:1.5;color:#111;max-width:600px;margin:auto;">
              <div style="text-align:center;margin-bottom:18px;">
                <img src="{LOGO_URL}" alt="C3D Prints" width="120" style="width:120px;height:auto;">
              </div>
              <p>Hi {html_escape(first_name)},</p>
              <p>Thanks for approving your custom C3D Prints quote.</p>
              <p><strong>Approved quote amount:</strong> {html_escape(price_text)}</p>
              <p>You can complete checkout using either option below:</p>

              <p>
                <a href="{SHOPIFY_CHECKOUT_URL}" style="display:inline-block;background:#007bff;color:white;padding:13px 18px;border-radius:8px;text-decoration:none;font-weight:bold;margin:6px 8px 6px 0;">
                  Shop on C3DPRINTS.COM
                </a>
              </p>

              <p>
                <a href="{ETSY_CHECKOUT_URL}" style="display:inline-block;background:#f1641e;color:white;padding:13px 18px;border-radius:8px;text-decoration:none;font-weight:bold;margin:6px 8px 6px 0;">
                  Shop on Etsy
                </a>
              </p>

              <p>Once checkout is completed, your order will move into the print queue.</p>
              <p style="margin-top:18px;">
                <a href="{tracking_url}" style="display:inline-block;background:#1a73e8;color:white;padding:12px 18px;border-radius:8px;text-decoration:none;font-weight:bold;">
                  Track Your Order
                </a>
              </p>
              <p style="color:#666;font-size:12px;">Bookmark this to check your order status anytime:<br>{tracking_url}</p>
              {portal_footer_html(row["email"])}
              <p>Thank you,<br>C3D Prints</p>
            </div>
            """

            email_result = send_quote_notification(
                to_email=row["email"],
                subject=f"C3D Prints Checkout Links — Quote #{row.get('id')}",
                html_body=html_body,
                text_body=text_body,
            )

            if not email_result.get("sent"):
                raise HTTPException(status_code=500, detail=f"Checkout email failed: {email_result.get('reason', 'Unknown error')}")

            cur.execute(
                """
                UPDATE quote_requests
                SET
                    checkout_platform = %(checkout_platform)s,
                    checkout_url = %(checkout_url)s,
                    checkout_sent_at = %(checkout_sent_at)s,
                    tracking_token = %(tracking_token)s,
                    status = 'Awaiting Payment'
                WHERE id = %(request_id)s
                RETURNING *;
                """,
                {
                    "request_id": request_id,
                    "checkout_platform": "Both",
                    "checkout_url": f"Shopify: {SHOPIFY_CHECKOUT_URL} | Etsy: {ETSY_CHECKOUT_URL}",
                    "checkout_sent_at": datetime.now(timezone.utc),
                    "tracking_token": tracking_token,
                },
            )
            updated = cur.fetchone()

    return {"success": True, "email": email_result, "request": updated}


@app.patch("/admin/requests/{request_id}/payment")
def update_payment_status(request_id: int, request: PaymentStatusRequest, admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, quoted_price, final_price, paid, tracking_token "
                "FROM quote_requests WHERE id = %(request_id)s;",
                {"request_id": request_id},
            )
            existing = cur.fetchone()

            if not existing:
                raise HTTPException(status_code=404, detail="Quote request not found")

            if request.paid:
                tracking_token = existing.get("tracking_token") or create_tracking_token()
                cur.execute(
                    """
                    UPDATE quote_requests
                    SET
                        paid = TRUE,
                        paid_at = COALESCE(paid_at, %(paid_at)s),
                        tracking_token = %(tracking_token)s,
                        status = 'Paid'
                    WHERE id = %(request_id)s
                    RETURNING *;
                    """,
                    {"request_id": request_id, "paid_at": datetime.now(timezone.utc), "tracking_token": tracking_token},
                )
            else:
                cur.execute(
                    """
                    UPDATE quote_requests
                    SET
                        paid = FALSE,
                        paid_at = NULL
                    WHERE id = %(request_id)s
                    RETURNING *;
                    """,
                    {"request_id": request_id},
                )

            updated = cur.fetchone()

    # Email the customer a payment confirmation + tracking link, but only on the
    # transition into Paid (not on repeat marks). Best-effort: never fail the
    # payment update because an email didn't send.
    if request.paid and not existing.get("paid"):
        send_payment_confirmation(updated)

    return {"success": True, "request": updated}


def send_payment_confirmation(row: dict) -> None:
    try:
        email = row.get("email")
        if not email:
            return
        first_name = (row.get("name") or "there").split(" ")[0]
        price = row.get("quoted_price") or row.get("final_price")
        price_text = f"${float(price):.2f}" if price is not None else "your order"
        token = row.get("tracking_token")
        tracking_url = f"{PUBLIC_BASE_URL}/track/{token}" if token else None
        track_html = (
            f'<p style="margin-top:18px;"><a href="{tracking_url}" '
            f'style="display:inline-block;background:#1a73e8;color:white;padding:12px 18px;'
            f'border-radius:8px;text-decoration:none;font-weight:bold;">Track Your Order</a></p>'
            f'<p style="color:#666;font-size:12px;">Check your order status anytime:<br>{tracking_url}</p>'
        ) if tracking_url else ""
        track_text = f"\n\nTrack your order status anytime:\n{tracking_url}" if tracking_url else ""
        html_body = (
            f'<div style="font-family:Arial,sans-serif;line-height:1.5;color:#111;max-width:600px;margin:auto;">'
            f'<div style="text-align:center;margin-bottom:18px;">'
            f'<img src="{LOGO_URL}" alt="C3D Prints" width="120" style="width:120px;height:auto;"></div>'
            f'<p>Hi {html_escape(first_name)},</p>'
            f'<p>We have received your payment of <strong>{html_escape(price_text)}</strong>. Thank you!</p>'
            f'<p>Your order is now in our production queue. You can follow its progress, '
            f'from printing through completion, on your tracking page.</p>'
            f'{track_html}'
            f'{portal_footer_html(email)}'
            f'<p>Thank you,<br>C3D Prints</p></div>'
        )
        text_body = (
            f"Hi {first_name},\n\nWe have received your payment of {price_text}. Thank you!\n\n"
            f"Your order is now in our production queue.{track_text}\n\nThank you,\nC3D Prints"
        )
        send_quote_notification(
            to_email=email,
            subject="Payment received - your C3D Prints order is in production",
            html_body=html_body,
            text_body=text_body,
        )
    except Exception as exc:
        print(f"Payment confirmation email failed: {exc}")



def create_tracking_token() -> str:
    return secrets.token_urlsafe(24)

def get_or_create_tracking_token(request_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tracking_token FROM quote_requests WHERE id = %(id)s;", {"id": request_id})
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Quote request not found")
            if row.get("tracking_token"):
                return row["tracking_token"]
            token = create_tracking_token()
            cur.execute("UPDATE quote_requests SET tracking_token = %(token)s WHERE id = %(id)s RETURNING tracking_token;", {"token": token, "id": request_id})
            return cur.fetchone()["tracking_token"]

def render_tracking_page(row: dict) -> str:
    status = row.get("status") or "New"
    paid = bool(row.get("paid"))
    price = row.get("quoted_price") or row.get("final_price")
    price_text = f"${float(price):.2f}" if price is not None else "Not listed yet"
    done = {
        "quoted": status in {"Quoted","Approved","Awaiting Payment","Paid","Printing","Completed"},
        "approved": status in {"Approved","Awaiting Payment","Paid","Printing","Completed"},
        "paid": paid or status in {"Paid","Printing","Completed"},
        "printing": status in {"Printing","Completed"},
        "completed": status == "Completed",
    }
    def step(label, ok):
        return f"<div class='step {'done' if ok else 'pending'}'><span>{'✓' if ok else '○'}</span><strong>{html_escape(label)}</strong></div>"
    steps = step("Quote Sent", done["quoted"]) + step("Quote Approved", done["approved"]) + step("Payment Received", done["paid"]) + step("Printing", done["printing"]) + step("Completed", done["completed"])
    note = html_escape(row.get("customer_status_note") or "Your order status updates as C3D Prints moves your project through the workflow.")
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>C3D Prints Order Status</title><style>
body{{margin:0;background:#0b1623;color:#ddeeff;font-family:Arial,sans-serif;padding:24px}}.wrap{{max-width:760px;margin:auto}}.card{{background:#162236;border:1px solid #1e3550;border-radius:18px;padding:22px;margin-bottom:16px}}h1{{color:#33ccff;margin:0 0 8px}}p{{color:#8aa8c5}}.badge{{display:inline-block;border:1px solid #1e3550;border-radius:999px;padding:6px 10px;color:#33ccff;font-weight:bold}}.price{{font-size:30px;color:#00e890;font-weight:bold;margin:12px 0}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.detail{{background:#0d1928;border:1px solid #1e3550;border-radius:12px;padding:12px}}.detail label{{display:block;color:#8aa8c5;font-size:12px;margin-bottom:6px}}.step{{display:flex;gap:10px;align-items:center;background:#0d1928;border:1px solid #1e3550;border-radius:12px;padding:14px;margin-bottom:10px}}.done{{color:#00e890;border-color:rgba(0,232,144,.45)}}.pending{{color:#8aa8c5}}.note{{background:rgba(255,140,58,.08);border:1px solid rgba(255,140,58,.35);border-radius:12px;padding:14px;color:#ff8c3a}}@media(max-width:640px){{.grid{{grid-template-columns:1fr}}body{{padding:14px}}}}</style></head><body><div class='wrap'><div class='card'><div style='text-align:center;margin-bottom:6px;'><img src='{LOGO_URL}' alt='C3D Prints' style='width:90px;height:auto;'></div><h1>C3D Prints Order Status</h1><p>Tracking for quote/request #{html_escape(str(row.get('id')))}</p><span class='badge'>{html_escape(status)}</span><div class='price'>{html_escape(price_text)}</div><div class='grid'><div class='detail'><label>Customer</label><strong>{html_escape(str(row.get('name') or ''))}</strong></div><div class='detail'><label>Payment</label><strong>{'Received' if paid else 'Not marked paid yet'}</strong></div><div class='detail'><label>Checkout Sent</label><strong>{html_escape(str(row.get('checkout_sent_at') or 'Not yet'))}</strong></div><div class='detail'><label>Paid At</label><strong>{html_escape(str(row.get('paid_at') or 'Not yet'))}</strong></div></div></div><div class='card'><h2 style='color:#ff8c3a;margin-top:0'>Progress</h2>{steps}</div><div class='card'><h2 style='color:#ff8c3a;margin-top:0'>Note</h2><div class='note'>{note}</div></div></div></body></html>"""

@app.get("/track/{tracking_token}", response_class=HTMLResponse)
def public_tracking_page(tracking_token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,email,status,final_price,quoted_price,checkout_sent_at,paid,paid_at,customer_status_note FROM quote_requests WHERE tracking_token = %(token)s;", {"token": tracking_token})
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tracking page not found")
    return HTMLResponse(render_tracking_page(row))


def render_portal_page(email: str, rows: list) -> str:
    name = ""
    for r in rows:
        if r.get("name"):
            name = r["name"].split(" ")[0]
            break
    greeting = html_escape(name) if name else "there"

    def status_color(status):
        return {
            "New": "var(--muted)", "Reviewing": "#33ccff", "Need Info": "#ffd166",
            "Quoted": "#33ccff", "Approved": "#00e890", "Awaiting Payment": "#ff8c3a",
            "Paid": "#00e890", "Printing": "#ff8c3a", "Completed": "#00e890",
            "Archived": "var(--muted)",
        }.get(status, "var(--muted)")

    cards = []
    if not rows:
        cards.append("<div class='empty'>No quote requests found for your email yet.</div>")
    for r in rows:
        status = r.get("status") or "New"
        price = r.get("quoted_price") or r.get("final_price")
        price_text = f"${float(price):.2f}" if price is not None else "Pending"
        project = html_escape((str(r.get("project_description") or "")[:130]))
        created = ""
        if r.get("created_at"):
            created = html_escape(str(r["created_at"])[:10])
        actions = ""
        if status == "Quoted" and r.get("approval_token"):
            actions += (f"<a class='btn approve' href='{PUBLIC_BASE_URL}/approve/"
                        f"{html_escape(str(r.get('approval_token')))}'>Review &amp; Approve</a>")
        if r.get("tracking_token"):
            actions += (f"<a class='btn track' href='{PUBLIC_BASE_URL}/track/"
                        f"{html_escape(str(r.get('tracking_token')))}'>Track Order</a>")
        actions_html = f"<div class='qactions'>{actions}</div>" if actions else ""
        cards.append(
            f"<div class='qcard'>"
            f"<div class='qhead'><span class='qid'>Request #{html_escape(str(r.get('id')))}</span>"
            f"<span class='badge' style='color:{status_color(status)};border-color:{status_color(status)}'>{html_escape(status)}</span></div>"
            f"<div class='qproject'>{project}</div>"
            f"<div class='qmeta'><span>Submitted: {created or '--'}</span><span class='qprice'>{html_escape(price_text)}</span></div>"
            f"{actions_html}"
            f"</div>"
        )

    return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>My C3D Prints Quotes</title><style>
:root{{--bg:#0b1623;--card:#162236;--input:#0d1928;--border:#1e3550;--blue-l:#33ccff;--orange-l:#ff8c3a;--green:#00e890;--text:#ddeeff;--muted:#7fa0bd}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Arial,sans-serif;padding:24px}}
.wrap{{max-width:760px;margin:auto}}
.top{{text-align:center;margin-bottom:18px}}
.top img{{width:96px;height:auto}}
h1{{color:var(--blue-l);margin:8px 0 4px;font-size:24px}}
.sub{{color:var(--muted);margin:0 0 18px;text-align:center}}
.qcard{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:14px}}
.qhead{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.qid{{font-weight:bold;color:var(--text)}}
.badge{{border:1px solid;border-radius:999px;padding:4px 10px;font-size:12px;font-weight:bold}}
.qproject{{color:#cfe3f7;margin-bottom:10px}}
.qmeta{{display:flex;justify-content:space-between;color:var(--muted);font-size:13px}}
.qprice{{color:var(--green);font-weight:bold;font-size:16px}}
.qactions{{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}}
.btn{{display:inline-block;text-decoration:none;font-weight:bold;padding:10px 16px;border-radius:8px;font-size:14px}}
.btn.approve{{background:#00b341;color:#fff}}
.btn.track{{background:#1a73e8;color:#fff}}
.empty{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:26px;text-align:center;color:var(--muted)}}
</style></head><body><div class='wrap'>
<div class='top'><img src='{LOGO_URL}' alt='C3D Prints'><h1>My Quotes &amp; Orders</h1></div>
<p class='sub'>Hi {greeting}, here is everything tied to {html_escape(email)}.</p>
{''.join(cards)}
</div></body></html>"""


@app.get("/portal/{token}", response_class=HTMLResponse)
def customer_portal(token: str):
    email = read_portal_token(token)
    if not email:
        raise HTTPException(status_code=404, detail="Portal link not found")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, name, project_description, status, quoted_price,
                       final_price, approval_token, tracking_token
                FROM quote_requests
                WHERE lower(email) = %(email)s AND status <> 'Archived'
                ORDER BY created_at DESC;
                """,
                {"email": email},
            )
            rows = cur.fetchall()
    return HTMLResponse(render_portal_page(email, rows))

@app.post("/admin/requests/{request_id}/tracking")
def create_request_tracking_link(request_id: int, admin=Depends(verify_admin)):
    token = get_or_create_tracking_token(request_id)
    return {"success": True, "tracking_token": token, "tracking_url": f"{PUBLIC_BASE_URL}/track/{token}"}


# ----- Customer quote approval -----

APPROVED_STATUSES = {"Approved", "Awaiting Payment", "Paid", "Printing", "Completed"}


def get_or_create_approval_token(request_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_token FROM quote_requests WHERE id = %(id)s;", {"id": request_id})
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Quote request not found")
            if row.get("approval_token"):
                return row["approval_token"]
            token = secrets.token_urlsafe(24)
            cur.execute(
                "UPDATE quote_requests SET approval_token = %(token)s WHERE id = %(id)s RETURNING approval_token;",
                {"token": token, "id": request_id},
            )
            return cur.fetchone()["approval_token"]


def approval_button_html(token: str) -> str:
    url = f"{PUBLIC_BASE_URL}/approve/{token}"
    return (
        f"<div style='margin:28px 0;text-align:center;'>"
        f"<a href='{url}' "
        f"style='background:#00b341;color:#ffffff;text-decoration:none;font-weight:bold;"
        f"font-family:Arial,sans-serif;font-size:16px;padding:14px 30px;border-radius:8px;display:inline-block;'>"
        f"Approve This Quote</a>"
        f"<p style='color:#666;font-size:12px;margin-top:10px;'>"
        f"Or copy this link into your browser:<br>{url}</p>"
        f"</div>"
    )


def render_approval_page(row: dict) -> str:
    status = row.get("status") or "Quoted"
    price = row.get("quoted_price") or row.get("final_price")
    price_text = f"${float(price):.2f}" if price is not None else "the amount in your quote"
    name = html_escape(str(row.get("name") or "there"))
    message = html_escape(str(row.get("quote_message") or "")).replace("\n", "<br>")
    rid = html_escape(str(row.get("id")))
    token = html_escape(str(row.get("approval_token")))

    if status in APPROVED_STATUSES:
        body = (
            "<div class='badge done'>Already approved</div>"
            "<p>Thanks! This quote is already approved. C3D Prints will follow up with checkout and "
            "production details. You can close this page.</p>"
        )
    else:
        body = (
            f"<div class='price'>{html_escape(price_text)}</div>"
            f"<div class='msg'>{message}</div>"
            f"<form method='post' action='{PUBLIC_BASE_URL}/approve/{token}'>"
            f"<button type='submit' class='approve-btn'>Approve This Quote</button>"
            f"</form>"
            "<p class='fine'>By approving, you confirm you want C3D Prints to proceed with this quote. "
            "You will receive checkout details next.</p>"
        )

    return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Approve Your C3D Prints Quote</title><style>
body{{margin:0;background:#0b1623;color:#ddeeff;font-family:Arial,sans-serif;padding:24px}}
.wrap{{max-width:620px;margin:auto}}
.card{{background:#162236;border:1px solid #1e3550;border-radius:18px;padding:26px}}
h1{{color:#33ccff;margin:0 0 6px}}p{{color:#8aa8c5}}
.price{{font-size:34px;color:#00e890;font-weight:bold;margin:16px 0}}
.msg{{background:#0d1928;border:1px solid #1e3550;border-radius:12px;padding:16px;color:#cfe3f7;margin:16px 0;white-space:normal}}
.approve-btn{{background:#00b341;color:#fff;border:0;border-radius:10px;padding:16px 28px;font-size:17px;font-weight:bold;cursor:pointer;width:100%}}
.approve-btn:hover{{background:#00c94a}}
.fine{{font-size:12px;color:#6f8aa6;margin-top:14px}}
.badge{{display:inline-block;border-radius:999px;padding:6px 12px;font-weight:bold;margin-bottom:8px}}
.done{{color:#00e890;border:1px solid rgba(0,232,144,.45)}}
</style></head><body><div class='wrap'><div class='card'>
<div style='text-align:center;margin-bottom:6px;'><img src='{LOGO_URL}' alt='C3D Prints' style='width:96px;height:auto;'></div>
<h1>C3D Prints</h1><p>Quote #{rid} for {name}</p>{body}
</div></div></body></html>"""


def render_approval_result_page(row: dict) -> str:
    rid = html_escape(str(row.get("id")))
    name = html_escape(str(row.get("name") or "there"))
    track = ""
    if row.get("tracking_token"):
        track_url = f"{PUBLIC_BASE_URL}/track/{html_escape(str(row.get('tracking_token')))}"
        track = f"<p><a href='{track_url}' style='color:#33ccff'>Track your order status</a></p>"
    return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Quote Approved</title><style>
body{{margin:0;background:#0b1623;color:#ddeeff;font-family:Arial,sans-serif;padding:24px}}
.wrap{{max-width:620px;margin:auto;text-align:center}}
.card{{background:#162236;border:1px solid #1e3550;border-radius:18px;padding:30px}}
h1{{color:#00e890;margin:0 0 10px}}p{{color:#8aa8c5}}
.check{{font-size:54px;color:#00e890}}
</style></head><body><div class='wrap'><div class='card'>
<div style='margin-bottom:8px;'><img src='{LOGO_URL}' alt='C3D Prints' style='width:90px;height:auto;'></div>
<div class='check'>&#10004;</div>
<h1>Quote Approved</h1>
<p>Thanks, {name}! Your quote #{rid} is approved.</p>
<p>C3D Prints will send your checkout link shortly so you can complete payment.</p>
{track}
</div></div></body></html>"""


@app.get("/approve/{approval_token}", response_class=HTMLResponse)
def public_approval_page(approval_token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, status, quoted_price, final_price, quote_message, approval_token "
                "FROM quote_requests WHERE approval_token = %(token)s;",
                {"token": approval_token},
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Approval link not found")
    return HTMLResponse(render_approval_page(row))


@app.post("/approve/{approval_token}", response_class=HTMLResponse)
def public_approval_confirm(approval_token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, status, tracking_token FROM quote_requests "
                "WHERE approval_token = %(token)s;",
                {"token": approval_token},
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Approval link not found")

            # Idempotent: only advance if not already approved/further along.
            if row["status"] not in APPROVED_STATUSES:
                cur.execute(
                    "UPDATE quote_requests SET status='Approved', approved_at=%(now)s "
                    "WHERE approval_token=%(token)s RETURNING id, name, tracking_token;",
                    {"now": datetime.now(timezone.utc), "token": approval_token},
                )
                row = cur.fetchone()
                _notify_admin_of_approval(row)

    return HTMLResponse(render_approval_result_page(row))


def _notify_admin_of_approval(row: dict) -> None:
    try:
        notify_email = os.getenv("QUOTE_NOTIFY_EMAIL", "hi@c3dprints.com")
        rid = html_escape(str(row.get("id")))
        name = html_escape(str(row.get("name") or ""))
        send_quote_notification(
            to_email=notify_email,
            subject=f"Quote #{row.get('id')} approved by {row.get('name')}",
            html_body=f"<p>Quote request #{rid} was just approved by {name}.</p>"
                      f"<p>Next step: send the checkout link from the admin dashboard.</p>",
            text_body=f"Quote request #{row.get('id')} approved by {row.get('name')}. Send checkout link next.",
        )
    except Exception as exc:
        print(f"Admin approval notification failed: {exc}")

@app.patch("/admin/requests/{request_id}/archive")
def archive_request(request_id: int, admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE quote_requests SET status='Archived' WHERE id=%(id)s RETURNING *;", {"id": request_id})
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Quote request not found")
    return {"success": True, "request": row}

@app.delete("/admin/requests/{request_id}")
def delete_request(request_id: int, admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM quote_requests WHERE id=%(id)s RETURNING id;", {"id": request_id})
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Quote request not found")
    return {"success": True, "deleted_id": row["id"]}


@app.post("/admin/requests/{request_id}/duplicate")
def duplicate_request(request_id: int, admin=Depends(verify_admin)):
    """Create a new request copying the source's contact + project details.

    The new request shares the customer's email, so it automatically appears in
    their portal alongside their other projects. Pricing/quote/payment state,
    tokens, AI output, and uploaded files are NOT copied (it's a fresh project).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, email, phone, project_description, quantity, approx_size,
                       deadline, material_preference, color_preference, use_case,
                       requirements, delivery_method, shipping_location, additional_notes
                FROM quote_requests WHERE id = %(id)s;
                """,
                {"id": request_id},
            )
            src = cur.fetchone()
    if not src:
        raise HTTPException(status_code=404, detail="Quote request not found")

    data = {
        "name": src["name"],
        "email": src["email"],
        "phone": src.get("phone"),
        "project_description": src.get("project_description") or "",
        "quantity": src.get("quantity") or 1,
        "approx_size": src.get("approx_size"),
        "deadline": src.get("deadline"),
        "material_preference": src.get("material_preference"),
        "color_preference": src.get("color_preference"),
        "use_case": src.get("use_case"),
        "requirements": src.get("requirements") or [],
        "delivery_method": src.get("delivery_method"),
        "shipping_location": src.get("shipping_location"),
        "additional_notes": src.get("additional_notes"),
    }
    new_id = save_request(data, "")
    return {"success": True, "new_request_id": new_id, "duplicated_from": request_id}


class AdminCreateRequest(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    project_description: Optional[str] = ""
    quantity: int = 1
    approx_size: Optional[str] = None
    deadline: Optional[str] = None
    material_preference: Optional[str] = None
    color_preference: Optional[str] = None
    use_case: Optional[str] = None
    requirements: Optional[List[str]] = None
    delivery_method: Optional[str] = None
    shipping_location: Optional[str] = None
    additional_notes: Optional[str] = None


@app.post("/admin/requests")
def admin_create_request(req: AdminCreateRequest, admin=Depends(verify_admin)):
    """Create a request from scratch (e.g. a customer who can't use the public form).
    Shares the customer's email, so it appears in their portal like any other."""
    name = (req.name or "").strip()
    email = (req.email or "").strip()
    if not name or not email:
        raise HTTPException(status_code=400, detail="Name and email are required")
    data = {
        "name": name,
        "email": email,
        "phone": req.phone,
        "project_description": (req.project_description or "").strip(),
        "quantity": max(1, int(req.quantity or 1)),
        "approx_size": req.approx_size,
        "deadline": req.deadline,
        "material_preference": req.material_preference,
        "color_preference": req.color_preference,
        "use_case": req.use_case,
        "requirements": req.requirements or [],
        "delivery_method": req.delivery_method,
        "shipping_location": req.shipping_location,
        "additional_notes": req.additional_notes,
    }
    new_id = save_request(data, "")
    return {"success": True, "new_request_id": new_id}


@app.post("/admin/requests/{request_id}/files")
def admin_upload_files(
    request_id: int,
    files: List[UploadFile] = File(...),
    admin=Depends(verify_admin),
):
    """Attach files to an existing request from the admin dashboard. Mirrors the
    public intake's storage + geometry-analysis handling and appends to any
    existing uploaded files (does not replace them)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, material_preference, uploaded_files FROM quote_requests WHERE id = %(id)s;",
                {"id": request_id},
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Quote request not found")

    material = row.get("material_preference")
    existing = list(row.get("uploaded_files") or [])
    new_files = []
    for file in files:
        if not file.filename:
            continue
        raw_for_analysis = file.file.read()
        try:
            file.file.seek(0)
        except Exception:
            pass
        stl_analysis = analyze_model_bytes(raw_for_analysis, file.filename, material)
        if storage_enabled():
            stored = upload_file_to_supabase_storage(request_id, file)
            if stored:
                if stl_analysis:
                    stored["stl_analysis"] = stl_analysis
                new_files.append(stored)
        else:
            metadata = {"filename": file.filename, "original_filename": file.filename, "storage_path": None}
            if stl_analysis:
                metadata["stl_analysis"] = stl_analysis
            new_files.append(metadata)

    if not new_files:
        raise HTTPException(status_code=400, detail="No valid files were uploaded.")

    combined = existing + new_files
    update_request_files(request_id, combined)
    return {"success": True, "uploaded_files": combined, "added": len(new_files)}


# ----- Production queue / printers -----

PRINTER_STATUSES = {"Available", "Printing", "Offline", "Maintenance"}
PRODUCTION_STATUSES = ["Approved", "Awaiting Payment", "Paid", "Printing"]


class PrinterCreate(BaseModel):
    name: str
    notes: Optional[str] = None
    serial: Optional[str] = None


class PrinterUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    serial: Optional[str] = None


class AssignPrinterRequest(BaseModel):
    printer_id: Optional[int] = None


class PrinterReport(BaseModel):
    serial: str
    state: Optional[str] = None        # raw Bambu gcode_state (RUNNING/IDLE/PAUSE/FINISH/FAILED/PREPARE)
    progress: Optional[int] = None     # 0-100
    remaining_min: Optional[int] = None


# Map raw Bambu gcode_state -> our printer status column.
BAMBU_STATE_TO_STATUS = {
    "RUNNING": "Printing", "PREPARE": "Printing", "PAUSE": "Printing",
    "FINISH": "Available", "IDLE": "Available", "FAILED": "Offline",
}


@app.post("/admin/printers")
def add_printer(req: PrinterCreate, admin=Depends(verify_admin)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Printer name is required")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO printers (name, notes, serial) VALUES (%(n)s, %(o)s, %(s)s) RETURNING id, name, status, notes, serial;",
                {"n": name, "o": req.notes, "s": (req.serial or "").strip() or None},
            )
            return {"success": True, "printer": cur.fetchone()}


@app.patch("/admin/printers/{printer_id}")
def update_printer(printer_id: int, req: PrinterUpdate, admin=Depends(verify_admin)):
    if req.status is not None and req.status not in PRINTER_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Valid: {', '.join(sorted(PRINTER_STATUSES))}")
    fields = []
    params = {"id": printer_id}
    if req.name is not None:
        fields.append("name = %(name)s"); params["name"] = req.name.strip()
    if req.status is not None:
        fields.append("status = %(status)s"); params["status"] = req.status
    if req.notes is not None:
        fields.append("notes = %(notes)s"); params["notes"] = req.notes
    if req.serial is not None:
        fields.append("serial = %(serial)s"); params["serial"] = req.serial.strip() or None
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE printers SET {', '.join(fields)} WHERE id = %(id)s RETURNING id, name, status, notes, serial;",
                params,
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Printer not found")
    return {"success": True, "printer": row}


@app.post("/agent/printer-report")
def printer_report(report: PrinterReport, x_agent_key: str = Header(None)):
    """Ingest live status from the on-prem Bambu bridge agent. Auth via a shared
    X-Agent-Key header (PRINTER_AGENT_KEY). Maps the report to a printer by serial."""
    expected = os.getenv("PRINTER_AGENT_KEY")
    if not expected or not x_agent_key or not hmac.compare_digest(x_agent_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing agent key")
    serial = (report.serial or "").strip()
    if not serial:
        raise HTTPException(status_code=400, detail="serial is required")
    status = BAMBU_STATE_TO_STATUS.get((report.state or "").upper())
    sets = ["live_state = %(ls)s", "progress = %(pg)s", "remaining_min = %(rm)s", "last_report_at = %(ts)s"]
    params = {"ls": report.state, "pg": report.progress, "rm": report.remaining_min,
              "ts": datetime.now(timezone.utc), "serial": serial}
    if status:
        sets.append("status = %(status)s"); params["status"] = status
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE printers SET {', '.join(sets)} WHERE serial = %(serial)s RETURNING id;", params)
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"No printer registered with serial {serial}")
    return {"success": True, "printer_id": row["id"]}


@app.delete("/admin/printers/{printer_id}")
def delete_printer(printer_id: int, admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE quote_requests SET assigned_printer_id = NULL WHERE assigned_printer_id = %(id)s;", {"id": printer_id})
            cur.execute("DELETE FROM printers WHERE id = %(id)s RETURNING id;", {"id": printer_id})
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Printer not found")
    return {"success": True, "deleted_id": row["id"]}


@app.patch("/admin/requests/{request_id}/assign-printer")
def assign_printer(request_id: int, req: AssignPrinterRequest, admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if req.printer_id is not None:
                cur.execute("SELECT id FROM printers WHERE id = %(id)s;", {"id": req.printer_id})
                if not cur.fetchone():
                    raise HTTPException(status_code=400, detail="Printer not found")
            cur.execute(
                "UPDATE quote_requests SET assigned_printer_id = %(pid)s WHERE id = %(rid)s RETURNING id, assigned_printer_id;",
                {"pid": req.printer_id, "rid": request_id},
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Quote request not found")
    return {"success": True, "request_id": row["id"], "assigned_printer_id": row["assigned_printer_id"]}


def _job_est_hours(row: dict) -> Optional[float]:
    analysis = pick_best_stl_analysis(row.get("uploaded_files"))
    if analysis and analysis.get("estimated_hours_rough"):
        return round(float(analysis["estimated_hours_rough"]) * max(1, int(row.get("quantity") or 1)), 2)
    return None


@app.get("/admin/production-queue")
def production_queue(admin=Depends(verify_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, status, notes, serial, live_state, progress, remaining_min, last_report_at FROM printers ORDER BY id;")
            printers = cur.fetchall()
            cur.execute(
                """
                SELECT id, name, project_description, status, due_date, quantity,
                       uploaded_files, assigned_printer_id
                FROM quote_requests
                WHERE status = ANY(%(statuses)s)
                ORDER BY (due_date IS NULL OR due_date = ''), due_date ASC, created_at ASC;
                """,
                {"statuses": PRODUCTION_STATUSES},
            )
            rows = cur.fetchall()

    load = {p["id"]: {"jobs": 0, "hours": 0.0} for p in printers}
    jobs = []
    for r in rows:
        est = _job_est_hours(r)
        pid = r.get("assigned_printer_id")
        if pid in load:
            load[pid]["jobs"] += 1
            load[pid]["hours"] += est or 0
        jobs.append({
            "id": r["id"],
            "name": r.get("name"),
            "project": (r.get("project_description") or "")[:90],
            "status": r.get("status"),
            "due_date": r.get("due_date"),
            "quantity": r.get("quantity") or 1,
            "est_hours": est,
            "assigned_printer_id": pid,
            "suggested_printer_id": None,
        })

    # Greedy least-loaded suggestion across Available printers, for unassigned jobs.
    available = [p["id"] for p in printers if p["status"] == "Available"]
    sugg_load = {pid: load[pid]["hours"] for pid in available}
    for j in jobs:
        if j["assigned_printer_id"] is None and available:
            best = min(available, key=lambda pid: sugg_load[pid])
            j["suggested_printer_id"] = best
            sugg_load[best] += j["est_hours"] or 0

    printers_out = [
        {**p, "assigned_jobs": load[p["id"]]["jobs"], "assigned_hours": round(load[p["id"]]["hours"], 2)}
        for p in printers
    ]
    return {"printers": printers_out, "jobs": jobs}
