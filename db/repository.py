"""Persistencia de ProjectSession en Supabase.

Mapping:
    sessions          ← ProjectSession (analysis, brief, financial como jsonb)
    proposal_versions ← ProjectSession.proposal_versions[*]
    reviews           ← ProjectSession.review_results[*]

Usa la clave service_role (salta RLS). No-op silencioso si SUPABASE_URL no está
configurada. Lanza SupabaseSaveError si la conexión existe pero algo falla.
"""
from __future__ import annotations

import re
from dataclasses import asdict, fields, is_dataclass
from typing import Any, Optional

import config
from models.schemas import (
    AnalysisResult, DocumentBrief, EcuadorAlignment, FinancialPackage,
    FunderInfo, ProjectSession,
)
from utils.supabase_client import get_client


class SupabaseSaveError(RuntimeError):
    pass


def is_enabled() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SECRET_KEY)


def _dump(obj: Any) -> Any:
    """Serializa dataclasses (anidados) a dict/list/primitivos JSON-friendly."""
    if obj is None:
        return None
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_dump(v) for v in obj]
    return obj


def _session_row(s: ProjectSession) -> dict:
    return {
        "session_id":    s.session_id,
        "user_input":    s.user_input,
        "input_mode":    s.input_mode,
        "doc_type_key":  s.doc_type_key,
        "language":      (s.brief.language if s.brief else None) or "es",
        "approved":      s.approved,
        "current_cycle": s.current_cycle,
        "output_path":   s.output_path,
        "word_path":     s.word_path,
        "excel_path":    s.excel_path,
        "template_text": s.template_text or None,
        "support_docs":  [{"name": n, "text": t} for n, t in (s.support_docs or [])],
        "owner_user_id": s.owner_user_id,
        "analysis":      _dump(s.analysis),
        "brief":         _dump(s.brief),
        "financial":     _dump(s.financial),
        "research_approved": bool(s.research_approved),
    }


def _proposal_rows(session_uuid: str, s: ProjectSession) -> list[dict]:
    return [
        {"session_id": session_uuid, "cycle": i, "content": content}
        for i, content in enumerate(s.proposal_versions, start=1)
    ]


def _review_rows(session_uuid: str, s: ProjectSession) -> list[dict]:
    rows = []
    for r in s.review_results:
        rows.append({
            "session_id":           session_uuid,
            "cycle":                r.cycle,
            "approved":             r.approved,
            "overall_score":        float(r.overall_score),
            "criterion_scores":     _dump(r.criterion_scores),
            "format_check":         _dump(r.format_check),
            "strengths":            _dump(r.strengths),
            "corrections":          _dump(r.corrections),
            "critical_issues":      _dump(r.critical_issues),
            "compliance_checklist": _dump(r.compliance_checklist),
            "failing_elements":     _dump(r.failing_elements),
            "recommendation":       r.recommendation,
        })
    return rows


def update_progress(session_id: str, phase: str, label: str, icon: str = "⚙",
                    status: str = "running", detail: str = "") -> None:
    """Escribe el paso actual del pipeline en la BD (no bloquea si falla)."""
    if not is_enabled():
        return
    try:
        import datetime
        from utils.supabase_client import get_client
        sb = get_client(service_role=True)
        # Recuperar pasos actuales
        row = sb.table("sessions").select("progress_steps").eq(
            "session_id", session_id).limit(1).execute()
        steps: list = (row.data[0].get("progress_steps") or []) if row.data else []
        # Actualizar paso existente si ya existe la misma fase, o añadir nuevo
        ts = datetime.datetime.utcnow().isoformat() + "Z"
        new_step = {"phase": phase, "label": label, "icon": icon,
                    "status": status, "detail": detail, "ts": ts}
        idx = next((i for i, s in enumerate(steps) if s.get("phase") == phase), -1)
        if idx >= 0:
            steps[idx] = new_step
        else:
            steps.append(new_step)
        sb.table("sessions").update(
            {"current_phase": f"{icon} {label}", "progress_steps": steps}
        ).eq("session_id", session_id).execute()
    except Exception:
        pass  # el progreso no debe tumbar el pipeline


def is_pause_requested(session_id: str) -> bool:
    """True si el usuario solicitó pausa (el pipeline lo verifica antes de cada fase)."""
    if not is_enabled():
        return False
    try:
        from utils.supabase_client import get_client
        sb = get_client(service_role=True)
        row = sb.table("sessions").select("pause_requested").eq(
            "session_id", session_id).limit(1).execute()
        return bool((row.data or [{}])[0].get("pause_requested", False))
    except Exception:
        return False


def request_pause(session_id: str) -> bool:
    """Activa el flag de pausa. El pipeline lo detecta y se detiene limpiamente."""
    if not is_enabled():
        return False
    try:
        from utils.supabase_client import get_client
        sb = get_client(service_role=True)
        sb.table("sessions").update({"pause_requested": True}).eq(
            "session_id", session_id).execute()
        return True
    except Exception as e:
        raise SupabaseSaveError(f"{type(e).__name__}: {e}") from e


def clear_pause_requested(session_id: str) -> None:
    """Limpia el flag de pausa al arrancar/reanudar un pipeline.

    Sin esto, una sesión pausada una vez queda con pause_requested=True para
    siempre (no existía ningún lugar que lo reseteara), así que "Continuar"
    se pausaba de inmediato otra vez en el primer checkpoint."""
    if not is_enabled():
        return
    try:
        sb = get_client(service_role=True)
        sb.table("sessions").update({"pause_requested": False}).eq(
            "session_id", session_id).execute()
    except Exception:
        pass  # best-effort, igual que el resto del control de progreso


def _dc_from_dict(cls, data: dict):
    """Reconstruye un dataclass desde un dict, ignorando claves desconocidas
    (tolera drift entre lo persistido y los campos actuales del dataclass)."""
    if not data:
        return None
    valid = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


def _analysis_from_dict(data: Optional[dict]) -> Optional[AnalysisResult]:
    if not data:
        return None
    data = dict(data)
    if data.get("funder"):
        data["funder"] = _dc_from_dict(FunderInfo, data["funder"])
    if data.get("ecuador_alignment"):
        data["ecuador_alignment"] = _dc_from_dict(EcuadorAlignment, data["ecuador_alignment"])
    try:
        return _dc_from_dict(AnalysisResult, data)
    except TypeError:
        return None  # esquema persistido incompatible: no reutilizable, se reinvestiga


def load_resumable_state(session_id: str) -> Optional[dict]:
    """Lee el estado persistido de una sesión para reanudarla tras una pausa.

    Devuelve None si Supabase está apagado, la sesión no existe, o no había
    investigación aprobada guardada (nada seguro que reutilizar — el pipeline
    reinvestigará desde cero, que es el comportamiento actual). Si existe,
    devuelve analysis/brief/financial/proposal_versions/current_cycle listos
    para poblar un ProjectSession nuevo sin repetir Fase 1/Gate 1.
    """
    if not is_enabled():
        return None
    try:
        sb = get_client(service_role=True)
        row = sb.table("sessions").select(
            "id, analysis, brief, financial, current_cycle, research_approved"
        ).eq("session_id", session_id).limit(1).execute()
        if not row.data:
            return None
        data = row.data[0]
        # Solo es seguro saltar Fase 1 si Gate 1 ya había aprobado esta investigación.
        if not data.get("research_approved") or not data.get("analysis") or not data.get("brief"):
            return None

        analysis = _analysis_from_dict(data.get("analysis"))
        brief = _dc_from_dict(DocumentBrief, data.get("brief")) if data.get("brief") else None
        if not analysis or not brief:
            return None
        financial = (_dc_from_dict(FinancialPackage, data["financial"])
                     if data.get("financial") else None)

        proposals_resp = (
            sb.table("proposal_versions").select("cycle, content")
            .eq("session_id", data["id"]).order("cycle").execute()
        )
        proposal_versions = [r["content"] for r in (proposals_resp.data or []) if r.get("content")]

        return {
            "analysis": analysis,
            "brief": brief,
            "financial": financial,
            "proposal_versions": proposal_versions,
            "current_cycle": int(data.get("current_cycle") or 0),
        }
    except Exception:
        return None  # reanudar es best-effort: si falla, el pipeline arranca de cero


def cancel_session(session_id: str) -> bool:
    """Cancela un trabajo (lo marca como fallido/cancelado, conservando el registro).
    Devuelve True si existía. Idempotente si Supabase está apagado."""
    if not is_enabled():
        return False
    try:
        sb = get_client(service_role=True)
        sess = sb.table("sessions").select("id").eq("session_id", session_id).limit(1).execute()
        if not sess.data:
            return False
        sb.table("sessions").update(
            {"status": "failed", "error_message": "Cancelado por el usuario",
             "completed_at": "now()"}
        ).eq("session_id", session_id).execute()
        return True
    except Exception as e:  # noqa: BLE001
        raise SupabaseSaveError(f"{type(e).__name__}: {e}") from e


def delete_session(session_id: str) -> bool:
    """Borra una sesión y sus borradores/revisiones. Devuelve True si existía.
    Idempotente: si no existe o Supabase está apagado, devuelve False sin error."""
    if not is_enabled():
        return False
    try:
        sb = get_client(service_role=True)
        sess = (
            sb.table("sessions").select("id").eq("session_id", session_id).limit(1).execute()
        )
        if not sess.data:
            return False
        row_uuid = sess.data[0]["id"]
        sb.table("proposal_versions").delete().eq("session_id", row_uuid).execute()
        sb.table("reviews").delete().eq("session_id", row_uuid).execute()
        sb.table("sessions").delete().eq("session_id", session_id).execute()
        return True
    except Exception as e:  # noqa: BLE001
        raise SupabaseSaveError(f"{type(e).__name__}: {e}") from e


_MISSING_COLUMN_RE = re.compile(r"[Cc]ould not find the '(\w+)' column")


def _upsert_session_tolerant(sb, row: dict, max_attempts: int = 5):
    """Upsert de `row` en sessions; si Supabase/PostgREST rechaza por una columna
    cuya migración aún no se aplicó ("Could not find the 'X' column ... in the
    schema cache"), la quita del payload y reintenta — genérico para CUALQUIER
    columna futura, no solo 'research_approved' (el primer caso real que rompió
    la persistencia de TODAS las sesiones hasta que se detectó y se aplicó la
    migración correspondiente)."""
    attempt_row = dict(row)
    last_err: Exception | None = None
    for _ in range(max_attempts):
        try:
            return sb.table("sessions").upsert(attempt_row, on_conflict="session_id").execute()
        except Exception as e:
            m = _MISSING_COLUMN_RE.search(str(e))
            if not m or m.group(1) not in attempt_row:
                raise
            del attempt_row[m.group(1)]
            last_err = e
    raise SupabaseSaveError(f"upsert sessions: demasiadas columnas ausentes tras reintentos: {last_err}")


def save_session(session: ProjectSession) -> str | None:
    """Persiste la sesión completa en Supabase. Devuelve el uuid de la sesión.

    Upsert por session_id (texto corto) → si ya existe, actualiza y reemplaza
    los borradores/revisiones existentes para evitar duplicados al reintentar.
    """
    if not is_enabled():
        return None

    try:
        sb = get_client(service_role=True)

        # Upsert session
        row = _session_row(session)
        resp = _upsert_session_tolerant(sb, row)
        if not resp.data:
            raise SupabaseSaveError("upsert sessions devolvió data vacía")
        session_uuid = resp.data[0]["id"]

        # Reemplazar borradores y revisiones (idempotente ante reintentos)
        sb.table("proposal_versions").delete().eq("session_id", session_uuid).execute()
        sb.table("reviews").delete().eq("session_id", session_uuid).execute()

        prop_rows = _proposal_rows(session_uuid, session)
        if prop_rows:
            sb.table("proposal_versions").insert(prop_rows).execute()

        rev_rows = _review_rows(session_uuid, session)
        if rev_rows:
            sb.table("reviews").insert(rev_rows).execute()

        return session_uuid

    except SupabaseSaveError:
        raise
    except Exception as e:
        raise SupabaseSaveError(f"{type(e).__name__}: {e}") from e
