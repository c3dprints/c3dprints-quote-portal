import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from fastapi import Header, HTTPException


TOKEN_TTL_SECONDS = 60 * 60 * 12  # 12 hours


def _secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET is not configured")
    return secret


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_admin_token(username: str) -> str:
    payload = {
        "sub": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
        "role": "admin",
    }

    payload_raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url_encode(payload_raw)

    signature = hmac.new(
        _secret().encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    return payload_b64 + "." + _b64url_encode(signature)


def verify_admin_token(token: str) -> dict:
    try:
        payload_b64, signature_b64 = token.split(".", 1)

        expected_signature = hmac.new(
            _secret().encode("utf-8"),
            payload_b64.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        provided_signature = _b64url_decode(signature_b64)

        if not hmac.compare_digest(expected_signature, provided_signature):
            raise ValueError("Bad signature")

        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))

        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("Token expired")

        if payload.get("role") != "admin":
            raise ValueError("Wrong role")

        return payload

    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")


def verify_admin(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing admin token")

    token = authorization.split(" ", 1)[1].strip()
    return verify_admin_token(token)


def check_admin_credentials(username: str, password: str) -> bool:
    expected_user = os.getenv("ADMIN_USERNAME")
    expected_pass = os.getenv("ADMIN_PASSWORD")

    if not expected_user or not expected_pass:
        raise RuntimeError("ADMIN_USERNAME or ADMIN_PASSWORD is not configured")

    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_pass)
