# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
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
                                  "created_at", "last_login_at",
                                  "phone", "position", "must_change_password")}


# ── Módulo de cuenta ─────────────────────────────────────────────────────────
# Los campos nuevos viven en db/014_cuenta_autoservicio.sql. Todo lo que los
# usa lo hace con .get(...) para que la API siga arrancando si la migración
# todavía no se aplicó.

def update_profile(user_id: str, *, name: Optional[str] = None,
                   email: Optional[str] = None, phone: Optional[str] = None,
                   position: Optional[str] = None) -> dict[str, Any]:
    """Actualiza los datos que el propio usuario mantiene."""
    row: dict[str, Any] = {}
    if name is not None:
        row["name"] = name.strip()
    if email is not None:
        row["email"] = email.strip().lower()
    if phone is not None:
        row["phone"] = phone.strip()
    if position is not None:
        row["position"] = position.strip()
    if not row:
        return get_by_id(user_id) or {}
    res = run_with_retry(
        lambda: _sb().table("users").update(row).eq("id", user_id).execute())
    if not res.data:
        raise UsersError("update users devolvió data vacía")
    return res.data[0]


def set_password(user_id: str, *, password_hash: str, password_salt: str,
                 must_change: bool = False,
                 temp_expires: Optional[str] = None,
                 reset_by: Optional[str] = None) -> None:
    """Escribe una contraseña nueva. `must_change=True` marca la clave como
    temporal: la API obliga a cambiarla antes de dejar operar."""
    row: dict[str, Any] = {
        "password_hash": password_hash,
        "password_salt": password_salt,
        "must_change_password": must_change,
        "temp_password_expires": temp_expires,
        "password_updated_at": "now()",
    }
    if reset_by:
        row["password_reset_by"] = reset_by
    run_with_retry(
        lambda: _sb().table("users").update(row).eq("id", user_id).execute())


def log_password(user_id: str, action: str, executed_by: Optional[str] = None,
                 ip: str = "") -> None:
    """Bitácora de cambios de clave. Nunca guarda la clave, solo el hecho."""
    try:
        run_with_retry(lambda: _sb().table("password_log").insert({
            "user_id": user_id, "action": action,
            "executed_by": executed_by, "ip": ip,
        }).execute())
    except Exception:  # noqa: BLE001
        # Auxiliar: si la tabla aún no está migrada no debe frenar el cambio.
        pass
