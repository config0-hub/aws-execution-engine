"""JWT credential token — HMAC-SHA256 signed, no external dependencies."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def create_credentials_token(claims: Dict[str, Any], secret: str) -> str:
    """Create a signed JWT with the given claims."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url_encode(json.dumps(claims).encode())
    sig = hmac.new(
        secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()
    return f"{header}.{payload}.{_b64url_encode(sig)}"


def verify_credentials_token(token: str, secret: str) -> Dict[str, Any]:
    """Verify JWT signature and expiry. Returns claims dict."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid token format")
    header_b64, payload_b64, sig_b64 = parts
    expected = hmac.new(
        secret.encode(),
        f"{header_b64}.{payload_b64}".encode(),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(_b64url_decode(sig_b64), expected):
        raise ValueError("Invalid token signature")
    claims = json.loads(_b64url_decode(payload_b64))
    if claims.get("exp") and time.time() > claims["exp"]:
        raise ValueError("Token expired")
    return claims
