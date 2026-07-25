# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""Autenticación propia (sin dependencias externas).

- Contraseñas: PBKDF2-HMAC-SHA256 con salt aleatorio por usuario.
- Tokens de sesión: stateless, firmados con HMAC-SHA256 (secreto = AUTH_SECRET).
  Formato: base64url(payload_json).base64url(hmac).  Payload: {uid, role, exp}.

La X-API-Key maestra (AGENTE_MAP_API_KEY) se acepta aparte como administrador
bootstrap (ver api/main.py).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import config

_PBKDF2_ROUNDS = 200_000
_ALGO = "sha256"


# ── Password hashing ────────────────────────────────────────────────────────
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Devuelve (hash_hex, salt_hex)."""
    salt_bytes = bytes.fromhex(salt) if salt else os.urandom(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt_bytes, _PBKDF2_ROUNDS)
    return dk.hex(), salt_bytes.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    try:
        calc, _ = hash_password(password, salt_hex)
    except ValueError:
        return False
    return hmac.compare_digest(calc, hash_hex or "")


# ── Tokens ──────────────────────────────────────────────────────────────────
def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_dec(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _secret() -> bytes:
    sec = config.AUTH_SECRET or "insecure-dev-secret-change-me"
    return sec.encode("utf-8")


def make_token(user_id: str, role: str, ttl_hours: int | None = None) -> str:
    ttl = ttl_hours if ttl_hours is not None else config.AUTH_TOKEN_TTL_HOURS
    payload = {"uid": user_id, "role": role, "exp": int(time.time()) + ttl * 3600}
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64u(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_token(token: str) -> dict | None:
    """Devuelve el payload {uid, role, exp} si la firma y la expiración son válidas."""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    expected = _b64u(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64u_dec(body))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload
