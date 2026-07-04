"""Lectura de sesiones guardadas en Supabase.

Cierra el bucle de persistencia: el agente las inserta vía repository.py,
y desde aquí las puedes listar/recuperar para revisar o reanalizar.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from utils.supabase_client import get_client

# Sin esto, una sesión cuyo proceso muere a mitad del pipeline (crash, redeploy,
# el dyno se duerme) queda en status='running' para siempre: nada la reconcilia,
# y la UI muestra un loader infinito. Se corrige de forma perezosa (best-effort)
# la primera vez que se consulta esa sesión, en vez de requerir un watchdog aparte.
_STALE_RUNNING_MINUTES = 20


def _last_heartbeat(row: dict) -> Optional[datetime.datetime]:
    steps = row.get("progress_steps") or []
    ts = steps[-1].get("ts") if steps else None
    ts = ts or row.get("started_at") or row.get("created_at")
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _reconcile_stale_running(row: dict) -> dict:
    """Si `row` está 'running' sin actividad reciente, la marca 'failed' en la
    BD y refleja el cambio en el dict devuelto. No bloquea la lectura si falla."""
    if not row or row.get("status") != "running":
        return row
    hb = _last_heartbeat(row)
    if hb is None:
        return row
    now = datetime.datetime.now(hb.tzinfo or datetime.timezone.utc)
    if (now - hb).total_seconds() < _STALE_RUNNING_MINUTES * 60:
        return row
    try:
        sb = get_client(service_role=True)
        sb.table("sessions").update({
            "status": "failed",
            "error_message": (
                f"Proceso interrumpido — sin actividad por más de "
                f"{_STALE_RUNNING_MINUTES} minutos (posible caída/redeploy del servidor). "
                "Usa Continuar para reintentar."
            ),
            "completed_at": "now()",
        }).eq("session_id", row["session_id"]).execute()
        row["status"] = "failed"
    except Exception:
        pass  # reconciliación es best-effort, nunca debe romper la lectura
    return row


def list_sessions(limit: int = 20, *, approved_only: bool = False,
                  owner_user_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Devuelve las sesiones más recientes con campos resumidos.

    Si `owner_user_id` se pasa, filtra solo las de ese dueño (vista por usuario).
    Si es None, devuelve todas (vista admin / clave maestra)."""
    sb = get_client(service_role=True)
    q = (
        sb.table("sessions")
        .select(
            "id, session_id, doc_type_key, approved, current_cycle, "
            "status, input_mode, user_input, completed_at, error_message, "
            "created_at, started_at, progress_steps, owner_user_id, brief, analysis"
        )
        .order("created_at", desc=True)
        .limit(limit)
    )
    if approved_only:
        q = q.eq("approved", True)
    if owner_user_id is not None:
        q = q.eq("owner_user_id", owner_user_id)
    rows = q.execute().data or []

    # Aplana los campos más útiles del JSON anidado para facilitar el listado.
    for r in rows:
        _reconcile_stale_running(r)
        r.pop("progress_steps", None)
        brief    = r.pop("brief", None) or {}
        analysis = r.pop("analysis", None) or {}
        real_title = brief.get("title") or analysis.get("project_title")
        r["title"]  = real_title or _auto_title(r, bool(analysis.get("alternatives")))
        funder = analysis.get("funder") or {}
        r["funder"] = funder.get("name")
    return rows


def _auto_title(row: dict, is_scouting: bool = False) -> str:
    from utils.titles import auto_title
    return auto_title(row, is_scouting)


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    """Recupera una sesión completa (con borradores y revisiones)."""
    sb = get_client(service_role=True)
    sess = (
        sb.table("sessions").select("*").eq("session_id", session_id).limit(1).execute()
    )
    if not sess.data:
        return None
    row = sess.data[0]
    row_uuid = row["id"]

    versions = (
        sb.table("proposal_versions")
        .select("cycle, content, char_count, created_at")
        .eq("session_id", row_uuid)
        .order("cycle")
        .execute()
        .data
        or []
    )
    reviews = (
        sb.table("reviews")
        .select("*")
        .eq("session_id", row_uuid)
        .order("cycle")
        .execute()
        .data
        or []
    )
    row["proposal_versions"] = versions
    row["reviews"] = reviews
    return _reconcile_stale_running(row)
