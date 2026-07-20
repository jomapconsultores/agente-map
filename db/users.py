"""Persistencia de usuarios en Supabase (tabla public.users).

Usa la clave service_role (salta RLS). Las contraseñas se guardan ya hasheadas
(PBKDF2) — este módulo nunca ve texto plano salvo en create_user, que recibe el
hash+salt ya calculados por api.auth.
"""
from __future__ import annotations

from typing import Any, Optional

import config
from utils.supabase_client import get_client, run_with_retry


class UsersError(RuntimeError):
    pass


def is_enabled() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SECRET_KEY)


def _sb():
    return get_client(service_role=True)


# Todas las operaciones van envueltas en run_with_retry: este módulo está en la
# ruta de AUTENTICACIÓN (login, revalidación de sesión en cada request), y era el
# único que no reintentaba. Un parpadeo transitorio de Supabase (ConnectError /
# RemoteProtocolError sobre una conexión keep-alive muerta) tumbaba el login con
# un 500 crudo "Error de base de datos: ConnectError…". run_with_retry recrea el
# pool y reintenta; los errores que NO son de transporte (4xx de PostgREST) se
# propagan igual que antes, sin reintentar.
_PUBLIC_COLS = "id, email, name, role, status, created_at, last_login_at"


def count_users() -> int:
    res = run_with_retry(lambda: _sb().table("users").select("id", count="exact").execute())
    return res.count or 0


def get_by_email(email: str) -> Optional[dict[str, Any]]:
    email = (email or "").strip().lower()
    res = run_with_retry(
        lambda: _sb().table("users").select("*").eq("email", email).limit(1).execute())
    return (res.data or [None])[0]


def get_by_id(user_id: str) -> Optional[dict[str, Any]]:
    res = run_with_retry(
        lambda: _sb().table("users").select("*").eq("id", user_id).limit(1).execute())
    return (res.data or [None])[0]


def create_user(*, email: str, name: str, password_hash: str, password_salt: str,
                role: str, status: str) -> dict[str, Any]:
    row = {
        "email": (email or "").strip().lower(),
        "name": (name or "").strip(),
        "password_hash": password_hash,
        "password_salt": password_salt,
        "role": role,
        "status": status,
    }
    res = run_with_retry(lambda: _sb().table("users").insert(row).execute())
    if not res.data:
        raise UsersError("insert users devolvió data vacía")
    return res.data[0]


def list_users(status: Optional[str] = None) -> list[dict[str, Any]]:
    def _q():
        q = _sb().table("users").select(_PUBLIC_COLS).order("created_at", desc=True)
        if status:
            q = q.eq("status", status)
        return q.execute()
    return run_with_retry(_q).data or []


def set_status(user_id: str, status: str) -> None:
    run_with_retry(
        lambda: _sb().table("users").update({"status": status}).eq("id", user_id).execute())


def set_role(user_id: str, role: str) -> None:
    run_with_retry(
        lambda: _sb().table("users").update({"role": role}).eq("id", user_id).execute())


def touch_login(user_id: str) -> None:
    try:
        run_with_retry(lambda: _sb().table("users")
                       .update({"last_login_at": "now()"}).eq("id", user_id).execute())
    except Exception:
        pass


def public_view(u: dict[str, Any]) -> dict[str, Any]:
    """Quita campos sensibles antes de devolver al cliente."""
    return {k: u.get(k) for k in ("id", "email", "name", "role", "status",
                                  "created_at", "last_login_at")}
