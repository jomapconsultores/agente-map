# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""Persistencia de credenciales WebAuthn (passkeys/biometría) en Supabase.

Tabla webauthn_credentials:
  user_id, credential_id (base64url), credential_public_key (base64), sign_count, label
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from utils.supabase_client import get_client


def _sb():
    return get_client(service_role=True)


def save_credential(*, user_id: str, credential_id: str,
                    public_key_bytes: bytes, sign_count: int,
                    label: str = "biometric") -> None:
    _sb().table("webauthn_credentials").insert({
        "user_id": user_id,
        "credential_id": credential_id,
        "credential_public_key": base64.b64encode(public_key_bytes).decode(),
        "sign_count": sign_count,
        "label": label,
    }).execute()


def get_credential(credential_id: str) -> Optional[dict[str, Any]]:
    res = _sb().table("webauthn_credentials").select("*").eq(
        "credential_id", credential_id
    ).limit(1).execute()
    row = (res.data or [None])[0]
    if row:
        row["_public_key_bytes"] = base64.b64decode(row["credential_public_key"])
    return row


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    res = _sb().table("webauthn_credentials").select(
        "id, credential_id, label, created_at"
    ).eq("user_id", user_id).order("created_at").execute()
    return res.data or []


def get_credentials_for_user(user_id: str) -> list[dict[str, Any]]:
    """Returns full credentials (with public key bytes) for allowCredentials."""
    res = _sb().table("webauthn_credentials").select(
        "credential_id, credential_public_key, sign_count, label"
    ).eq("user_id", user_id).execute()
    rows = res.data or []
    for r in rows:
        r["_public_key_bytes"] = base64.b64decode(r["credential_public_key"])
    return rows


def update_sign_count(credential_id: str, sign_count: int) -> None:
    _sb().table("webauthn_credentials").update(
        {"sign_count": sign_count}
    ).eq("credential_id", credential_id).execute()


def delete_credential(*, credential_id: str, user_id: str) -> bool:
    res = _sb().table("webauthn_credentials").delete().eq(
        "credential_id", credential_id
    ).eq("user_id", user_id).execute()
    return bool(res.data)
