"""Roles por módulo (public.user_module_roles): qué áreas — captación,
clientes, investigación, proyectos, oficios — puede operar cada usuario,
además de su rol admin/user. Solo el administrador otorga o revoca estos
accesos; un usuario puede tener varios módulos a la vez."""
from __future__ import annotations

from typing import Any, Optional

from utils.supabase_client import get_client

MODULES = ("captacion", "clientes", "investigacion", "proyectos", "oficios")


def _sb():
    return get_client(service_role=True)


def list_for_user(user_id: str) -> list[str]:
    res = _sb().table("user_module_roles").select("module").eq("user_id", user_id).execute()
    return sorted({r["module"] for r in (res.data or [])})


def list_all() -> dict[str, list[str]]:
    """Todos los grants agrupados por user_id, para el panel de administración."""
    res = _sb().table("user_module_roles").select("user_id, module").execute()
    by_user: dict[str, list[str]] = {}
    for r in (res.data or []):
        by_user.setdefault(r["user_id"], []).append(r["module"])
    return by_user


def grant(user_id: str, module: str, granted_by: Optional[str]) -> None:
    if module not in MODULES:
        raise ValueError(f"Módulo desconocido: {module}")
    _sb().table("user_module_roles").upsert(
        {"user_id": user_id, "module": module, "granted_by": granted_by},
        on_conflict="user_id,module",
    ).execute()


def revoke(user_id: str, module: str) -> None:
    if module not in MODULES:
        raise ValueError(f"Módulo desconocido: {module}")
    _sb().table("user_module_roles").delete().eq("user_id", user_id).eq("module", module).execute()
