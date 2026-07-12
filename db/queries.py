"""Lectura de sesiones guardadas en Supabase.

Cierra el bucle de persistencia: el agente las inserta vía repository.py,
y desde aquí las puedes listar/recuperar para revisar o reanalizar.
"""
from __future__ import annotations

import datetime
import os
from typing import Any, Optional

from utils.supabase_client import get_client, run_with_retry

# Sin esto, una sesión cuyo proceso muere a mitad del pipeline (crash, redeploy,
# el dyno se duerme) queda en status='running' para siempre: nada la reconcilia,
# y la UI muestra un loader infinito. Se corrige de forma perezosa (best-effort)
# la primera vez que se consulta esa sesión, en vez de requerir un watchdog aparte.
#
# 20→30 min: el umbral DEBE ser mayor que el mayor hueco posible entre latidos de
# una sesión que sigue viva. Una sola llamada LLM tiene hasta 600s de timeout con
# varios reintentos, y algunas fases (análisis de investigación, redacción de una
# sección larga) encadenan varias — con 20 min una sesión activa podía cruzar el
# umbral y marcarse "interrumpida" por error. Ahora las fases largas emiten latidos
# intermedios (researcher._beat / writer._beat) Y el umbral tiene margen.
_STALE_RUNNING_MINUTES = int(os.getenv("STALE_RUNNING_MINUTES", "30"))


def _parse_ts(ts) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _last_heartbeat(row: dict) -> Optional[datetime.datetime]:
    # OJO: repository.update_progress ACTUALIZA cada paso EN SU SITIO por 'phase'
    # (o lo añade), así que el último elemento del arreglo NO es necesariamente el
    # más reciente. Durante las fases lentas de un ciclo posterior (investigación
    # web + redacción), los pasos que se refrescan están en índices tempranos y
    # steps[-1] (p. ej. un gate de un ciclo anterior) conserva un ts congelado.
    # Tomar steps[-1] mataba por "inactividad" a sesiones que seguían trabajando.
    # El latido real es el MÁXIMO ts entre TODOS los pasos.
    steps = row.get("progress_steps") or []
    heartbeats = [_parse_ts(s.get("ts")) for s in steps]
    heartbeats = [h for h in heartbeats if h is not None]
    if heartbeats:
        return max(heartbeats)
    return _parse_ts(row.get("started_at") or row.get("created_at"))


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
        run_with_retry(lambda: get_client(service_role=True).table("sessions").update({
            "status": "failed",
            "error_message": (
                f"Proceso interrumpido — sin actividad por más de "
                f"{_STALE_RUNNING_MINUTES} minutos (posible caída/redeploy del servidor). "
                "Usa Continuar para reintentar."
            ),
            "completed_at": "now()",
        }).eq("session_id", row["session_id"]).execute())
        row["status"] = "failed"
    except Exception:
        pass  # reconciliación es best-effort, nunca debe romper la lectura
    return row


def reconcile_orphaned_running(limit: int = 500) -> int:
    """Barrido ACTIVO de reconciliación (no perezoso): marca 'failed' las sesiones
    que quedaron 'running' sin latido reciente (>_STALE_RUNNING_MINUTES).

    _reconcile_stale_running solo corre al LEER una sesión (list_sessions/get_session),
    así que una sesión muerta por un redeploy/crash que nadie abre se queda 'running'
    para siempre (loader infinito en la UI). Esta función se invoca al ARRANCAR el
    servidor (ver api.main lifespan) para cerrar ese hueco de inmediato tras cada
    redeploy. Reutiliza el mismo umbral y _last_heartbeat, así que es idempotente y
    segura aunque algún día se escale a más de un worker. Devuelve cuántas reconcilió.
    """
    try:
        rows = run_with_retry(lambda: get_client(service_role=True)
                              .table("sessions")
                              .select("session_id, status, progress_steps, started_at, created_at")
                              .eq("status", "running").limit(limit).execute()).data or []
    except Exception:
        return 0
    n = 0
    for r in rows:
        try:
            before = r.get("status")
            _reconcile_stale_running(r)  # reutiliza umbral + _last_heartbeat + UPDATE
            if before == "running" and r.get("status") == "failed":
                n += 1
        except Exception:
            continue  # best-effort: una fila problemática no aborta el barrido
    return n


def list_sessions(limit: int = 20, *, approved_only: bool = False,
                  owner_user_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Devuelve las sesiones más recientes con campos resumidos.

    Si `owner_user_id` se pasa, filtra solo las de ese dueño (vista por usuario).
    Si es None, devuelve todas (vista admin / clave maestra)."""
    def _fetch():
        # Reconstruye el query DENTRO del retry: tras un reset, get_client devuelve
        # un cliente nuevo; reutilizar un query armado con el cliente muerto volvería
        # a fallar sobre la misma conexión caída.
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
        return q.execute()

    rows = run_with_retry(_fetch).data or []

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
    sess = run_with_retry(lambda: get_client(service_role=True)
                          .table("sessions").select("*")
                          .eq("session_id", session_id).limit(1).execute())
    if not sess.data:
        return None
    row = sess.data[0]
    row_uuid = row["id"]

    versions = run_with_retry(lambda: get_client(service_role=True)
                              .table("proposal_versions")
                              .select("cycle, content, char_count, created_at")
                              .eq("session_id", row_uuid).order("cycle").execute()).data or []
    reviews = run_with_retry(lambda: get_client(service_role=True)
                             .table("reviews").select("*")
                             .eq("session_id", row_uuid).order("cycle").execute()).data or []
    row["proposal_versions"] = versions
    row["reviews"] = reviews
    return _reconcile_stale_running(row)
