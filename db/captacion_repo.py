# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""Persistencia de prospectos y clientes en Supabase."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from utils.supabase_client import get_client


# ── Prospectos ─────────────────────────────────────────────────────────────────
def save_prospectos(prospectos: list[dict], *, owner_user_id: str, producto: str) -> int:
    """Guarda la lista de prospectos y devuelve cuántos se insertaron."""
    sb = get_client(service_role=True)
    rows = []
    for p in prospectos:
        rows.append({
            "prospecto_id": str(uuid.uuid4()),
            "owner_user_id": owner_user_id,
            "producto": producto,
            "nombre": p.get("nombre") or "",
            "sector": p.get("sector") or "",
            "zona": p.get("zona") or "",
            "provincia": p.get("provincia") or "",
            "zona_nivel": int(p.get("zona_nivel") or 1),
            "contacto_web": p.get("contacto_web") or "",
            "contacto_email": p.get("contacto_email") or "",
            "contacto_telefono": p.get("contacto_telefono") or "",
            "fuente_publica": p.get("fuente_publica") or "",
            "fuente_url": p.get("fuente_url") or "",
            "relevancia": int(p.get("relevancia") or 5),
            "razon": p.get("razon") or "",
            "estado": "nuevo",
            "notas": "",
        })
    if rows:
        sb.table("prospectos").insert(rows).execute()
    return len(rows)


def list_prospectos(
    *, owner_user_id: Optional[str] = None,
    producto: Optional[str] = None,
    estado: Optional[str] = None,
    zona_nivel_max: Optional[int] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sb = get_client(service_role=True)
    q = (sb.table("prospectos")
         .select("*")
         .order("relevancia", desc=True)
         .order("zona_nivel")
         .limit(limit))
    if owner_user_id:
        q = q.eq("owner_user_id", owner_user_id)
    if producto:
        q = q.ilike("producto", f"%{producto}%")
    if estado:
        q = q.eq("estado", estado)
    if zona_nivel_max:
        q = q.lte("zona_nivel", zona_nivel_max)
    return q.execute().data or []


def update_prospecto(prospecto_id: str, *, owner_user_id: str, **fields) -> bool:
    sb = get_client(service_role=True)
    allowed = {"estado", "notas", "contacto_email", "contacto_telefono", "contacto_web"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return False
    res = (sb.table("prospectos")
           .update(data)
           .eq("prospecto_id", prospecto_id)
           .eq("owner_user_id", owner_user_id)
           .execute())
    return bool(res.data)


def delete_prospecto(*, prospecto_id: str, owner_user_id: str) -> bool:
    sb = get_client(service_role=True)
    res = (sb.table("prospectos")
           .delete()
           .eq("prospecto_id", prospecto_id)
           .eq("owner_user_id", owner_user_id)
           .execute())
    return bool(res.data)


def prospecto_to_cliente(*, prospecto_id: str, owner_user_id: str) -> Optional[str]:
    """Convierte un prospecto en cliente y devuelve el cliente_id."""
    sb = get_client(service_role=True)
    rows = (sb.table("prospectos")
            .select("*")
            .eq("prospecto_id", prospecto_id)
            .eq("owner_user_id", owner_user_id)
            .limit(1)
            .execute().data)
    if not rows:
        return None
    p = rows[0]
    cliente_id = save_cliente(
        owner_user_id=owner_user_id,
        nombre=p["nombre"],
        sector=p["sector"],
        zona=p["zona"],
        contacto_web=p["contacto_web"],
        contacto_email=p["contacto_email"],
        contacto_telefono=p["contacto_telefono"],
        origen=f"Prospección: {p['fuente_publica']}",
        notas=p["notas"],
    )
    # Marcar prospecto como convertido
    update_prospecto(prospecto_id, owner_user_id=owner_user_id, estado="cliente")
    return cliente_id


# ── Clientes ───────────────────────────────────────────────────────────────────
def save_cliente(
    *, owner_user_id: str,
    nombre: str,
    sector: str = "",
    zona: str = "",
    contacto_web: str = "",
    contacto_email: str = "",
    contacto_telefono: str = "",
    contacto_direccion: str = "",
    origen: str = "manual",
    notas: str = "",
) -> str:
    sb = get_client(service_role=True)
    cliente_id = str(uuid.uuid4())
    sb.table("clientes").insert({
        "cliente_id": cliente_id,
        "owner_user_id": owner_user_id,
        "nombre": nombre,
        "sector": sector,
        "zona": zona,
        "contacto_web": contacto_web,
        "contacto_email": contacto_email,
        "contacto_telefono": contacto_telefono,
        "contacto_direccion": contacto_direccion,
        "origen": origen,
        "notas": notas,
        "estado": "activo",
    }).execute()
    return cliente_id


def list_clientes(
    *, owner_user_id: Optional[str] = None,
    estado: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sb = get_client(service_role=True)
    q = (sb.table("clientes")
         .select("*")
         .order("created_at", desc=True)
         .limit(limit))
    if owner_user_id:
        q = q.eq("owner_user_id", owner_user_id)
    if estado:
        q = q.eq("estado", estado)
    return q.execute().data or []


def update_cliente(cliente_id: str, *, owner_user_id: str, **fields) -> bool:
    sb = get_client(service_role=True)
    allowed = {"nombre", "sector", "zona", "contacto_web", "contacto_email",
               "contacto_telefono", "contacto_direccion", "notas", "estado"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return False
    res = (sb.table("clientes")
           .update(data)
           .eq("cliente_id", cliente_id)
           .eq("owner_user_id", owner_user_id)
           .execute())
    return bool(res.data)


def delete_cliente(*, cliente_id: str, owner_user_id: str) -> bool:
    sb = get_client(service_role=True)
    res = (sb.table("clientes")
           .delete()
           .eq("cliente_id", cliente_id)
           .eq("owner_user_id", owner_user_id)
           .execute())
    return bool(res.data)
