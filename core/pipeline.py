# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""Pipeline por FASES con gates ≥90 revisados por IAs distintas y reinicio al inicio.

Flujo (cada fase la produce una IA y la audita OTRA; si un gate da <90 se vuelve al
inicio y se reinvestiga, hasta MAX_PIPELINE_RESTARTS intentos):

  FASE 0   Clasificación ........................ Mistral (ROLE_CLASSIFIER)
  FASE 0.5 Intake: análisis de docs/URLs ........ Mistral (antes del loop)
  FASE 1   Investigación web + análisis ......... Mistral (ROLE_RESEARCH)
           └─ GATE 1 revisado por ............... Codestral (ROLE_REVIEW_RESEARCH)
  FASE 2   Redacción del documento .............. Codestral (ROLE_WRITER)
           └─ GATE 2 revisado por ............... Mistral (ROLE_REVIEW_WRITER)
  FASE 3   Estructuración financiera ............ Codestral (ROLE_FINANCIAL)
  FASE 3.5 Revisión paquete completo ............ DeepSeek (ROLE_PACKAGE_REVIEW)
           └─ Si no pasa → REINICIA desde FASE 1
  FASE 4   Veredicto final 90/90 ................ Claude (reviewer.run)
           └─ Si no aprueba → REINICIA desde FASE 1

Fallback: si Mistral o Codestral no están disponibles, complete_builder escala
automáticamente a DeepSeek y como último recurso a Claude.

Si tras agotar los intentos no se alcanza el 90, se entrega la MEJOR versión
lograda marcada como inconclusa.

Esta es la versión "headless" que llama la API HTTP. La CLI (main.py) conserva su
flujo interactivo propio.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Optional

import config
from config import (
    MAX_PIPELINE_RESTARTS, ANTHROPIC_API_KEY,
    ROLE_REVIEW_RESEARCH, ROLE_WRITER, ROLE_REVIEW_WRITER, ROLE_FINANCIAL,
)
from models.schemas import ProjectSession
from models.doc_types import get_doc_type
import agents.classifier as classifier
import agents.researcher as researcher
import agents.scout as scout
import agents.writer as writer
import agents.reviewer as reviewer
import agents.financial as financial
import agents.phase_review as phase_review
import agents.intake as intake
from agents.analyst import result_from_data
from utils.output import save_session
from utils import empresas as empresas_util
from db import repository


def _resolve_doc_type(user_input: str, template_text: str, support_docs: list,
                      api_key: str, requested: Optional[str]) -> str:
    if requested and requested != "auto":
        return requested
    try:
        return classifier.detect_type(user_input, template_text, support_docs, api_key)
    except Exception:
        return "generico"


def _last_used_builder(session, *, cycle: Optional[int] = None,
                       research: bool = False) -> Optional[str]:
    """Proveedor que REALMENTE construyó una fase, según session.builder_log
    (ignora 'error'/None). `research=True` busca la última entrada de investigación;
    `cycle=N` la última entrada de redacción de ese ciclo."""
    for e in reversed(getattr(session, "builder_log", None) or []):
        used = e.get("used")
        if used in (None, "error"):
            continue
        if research:
            if e.get("phase") in ("research", "research_brief"):
                return used
        elif cycle is not None:
            if e.get("cycle") == cycle:
                return used
    return None


def _reviewer_avoiding(assigned: str, used: Optional[str], primary: str) -> str:
    """Elige el proveedor del gate garantizando revisión CRUZADA real.

    Si el revisor asignado no coincide con quien construyó la fase (camino sano),
    se conserva. Si coincide —porque el constructor primario cayó y complete_builder
    hizo fallback justo al proveedor del gate— esa IA revisaría su propia salida; se
    elige entonces otro proveedor distinto del usado Y del primario (posible caído).
    'anthropic'/'error'/None → se conserva el asignado (no hay conflicto medible)."""
    if not used or used in ("error", "anthropic") or used != assigned:
        return assigned
    for p in config.BUILDER_ROTATION:
        if p != used and p != primary:
            return p
    return assigned  # sin alternativa viable: phase_review.review ya maneja fail-open/closed


def _log(session_id: str, phase: str, label: str, icon: str = "⚙",
         status: str = "running", detail: str = "") -> None:
    """Registra el avance del pipeline en la BD para mostrarlo en la UI."""
    repository.update_progress(session_id, phase=phase, label=label,
                                icon=icon, status=status, detail=detail)


def _phase_heartbeater(session_id: str, phase: str, label: str, icon: str = "⚙"):
    """Mantiene fresco el latido de una fase que hace UNA sola llamada LLM larga
    (intake, gates, financial, veredicto) y por eso no emite progreso propio como
    sí lo hacen researcher._beat/writer._beat.

    Refresca progress_steps[phase].ts cada 60 s en un hilo daemon mientras la fase
    está en vuelo. Sin esto, una llamada legítimamente lenta (varios minutos, con
    reintentos) cruzaba el watchdog de 'sin actividad' (db.queries, 30 min) y la
    sesión se marcaba 'interrumpida' aunque el proceso siguiera trabajando.

    update_progress(phase=X) actualiza ESE paso en su sitio (repository.py) y
    _last_heartbeat = max(ts) de todos los pasos (db.queries), así que basta con
    refrescar el paso de la fase en curso. No hay carrera: solo corre una fase a la
    vez y el cuerpo del pipeline no reescribe ese paso durante la llamada.

    Devuelve (stop_event, thread); llama stop.set() en un finally para cortarlo.
    """
    stop = threading.Event()

    def _loop():
        while not stop.wait(60):
            try:
                repository.update_progress(session_id, phase=phase, label=label,
                                           icon=icon, status="running")
            except Exception:
                pass  # el latido nunca debe tumbar el pipeline

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return stop, t


def _wallclock_exceeded(start: float) -> bool:
    """True si el pipeline ya superó el techo de tiempo de pared. Se evalúa también
    DENTRO del ciclo (antes de las fases caras) — no solo al tope del for — para que
    una sola fase lenta no cruce el watchdog de 'sin actividad' sin ser cortada."""
    return (time.monotonic() - start) > config.MAX_PIPELINE_WALLCLOCK_SEC


def _gate_force_pass(session: "ProjectSession", gate_result: dict, attempt: int) -> bool:
    """Escape hatch: si tras GATE_FORCE_PASS_AFTER intentos un gate intermedio sigue
    rechazando pero el score está a GATE_FORCE_PASS_MARGIN del umbral, deja pasar al
    veredicto final (Claude) en vez de reiniciar sin fin. Un solo 'critical' falso-
    positivo de un constructor podía atorar el pipeline para siempre. NO aplica a tipos
    estrictos (tesis/artículo/TDR/peer_review), que deben fallar cerrado."""
    strict = bool(session.brief and
                  session.brief.doc_type_key in phase_review._STRICT_FAIL_CLOSED_TYPES)
    if strict or attempt < config.GATE_FORCE_PASS_AFTER:
        return False
    score = float(gate_result.get("score", 0) or 0)
    return score >= config.PHASE_REVIEW_THRESHOLD - config.GATE_FORCE_PASS_MARGIN


def _keep_quality_notes(session: "ProjectSession", phase: str, gate_result: dict) -> None:
    """Un gate que APRUEBA con observaciones no críticas ("issues") las perdía para
    siempre: solo se capturaba feedback cuando el gate RECHAZABA. Las acumula como
    contexto advisorio (no correcciones forzosas) para el redactor y el veredicto."""
    for issue in gate_result.get("issues") or []:
        note = f"[{phase}] {issue}"
        if note not in session.quality_notes:
            session.quality_notes.append(note)


def _pause_if_requested(session_id: str, session: "ProjectSession",
                        research_approved: bool = False) -> bool:
    """Verifica si el usuario solicitó pausa. Si es así, persiste el progreso ya
    logrado y detiene el pipeline limpiamente. Devuelve True si se debe abortar."""
    if not repository.is_pause_requested(session_id):
        return False
    session.research_approved = research_approved
    try:
        save_session(session)  # persiste analysis/brief/financial/proposal_versions ya hechos
    except Exception:
        pass  # pausar nunca debe fallar por un error de persistencia
    _log(session_id, "pausa", "Pausado por el usuario", "⏸", "paused",
         "Retomable con el botón Continuar desde donde lo dejaste")
    _mark_failed(session_id, "⏸ Pausado por el usuario · usa Continuar para reanudar")
    return True


def _mark_running(session_id: str, owner_user_id: Optional[str] = None, *,
                  user_input: Optional[str] = None, mode: Optional[str] = None,
                  doc_type_key: Optional[str] = None):
    if not repository.is_enabled():
        return
    try:
        from utils.supabase_client import get_client, run_with_retry
        row = {"session_id": session_id, "status": "running", "started_at": "now()",
               # Limpia el estado del intento anterior. Sin resetear progress_steps,
               # sus timestamps rancios hacían que el watchdog marcara la NUEVA corrida
               # como "sin actividad 30 min" apenas arrancaba (el bug del falso
               # "interrumpido" al reintentar). También se limpia el error/fin previo.
               "progress_steps": [], "current_phase": "",
               "error_message": None, "completed_at": None}
        # NO sobrescribir el tema real con un placeholder: antes se escribía
        # "(initializing)" y, si la corrida moría antes de save_session (p. ej. por una
        # desconexión de Supabase), la sesión quedaba con ese placeholder como
        # user_input PARA SIEMPRE → cada reintento investigaba "(initializing)" en vez
        # del tema real. Se escribe el tema recibido.
        if user_input is not None:
            row["user_input"] = user_input
        if mode:
            row["input_mode"] = mode
        if doc_type_key:
            row["doc_type_key"] = doc_type_key
        if owner_user_id:
            row["owner_user_id"] = owner_user_id
        run_with_retry(lambda: get_client(service_role=True).table("sessions")
                       .upsert(row, on_conflict="session_id").execute())
    except Exception:
        pass  # no bloquea el pipeline


def _mark_failed(session_id: str, error: str):
    if not repository.is_enabled():
        return
    try:
        from utils.supabase_client import get_client, run_with_retry
        run_with_retry(lambda: get_client(service_role=True).table("sessions").update(
            {"status": "failed", "error_message": error[:2000],
             "completed_at": "now()"}
        ).eq("session_id", session_id).execute())
    except Exception:
        pass


def _mark_completed(session_id: str, approved: bool):
    if not repository.is_enabled():
        return
    try:
        from utils.supabase_client import get_client, run_with_retry
        # .eq("status","running"): sólo cierra la sesión si SIGUE en curso. Si el
        # watchdog ya la marcó 'failed' (proceso tardío/zombi), no la revivimos a
        # 'approved' — evita que un job que sobrevivió al watchdog pise su decisión.
        run_with_retry(lambda: get_client(service_role=True).table("sessions").update(
            {"status": "approved" if approved else "failed",
             "completed_at": "now()"}
        ).eq("session_id", session_id).eq("status", "running").execute())
    except Exception:
        pass


def _enrich_brief_with_intake(session: ProjectSession) -> None:
    """Fusiona los requisitos del intake en el brief para que Gates 1 y 2 los validen."""
    brief = session.brief
    intake = session.intake_data
    if not brief or not intake:
        return
    # Añadir secciones obligatorias del formulario que no estén ya en el brief
    required = [
        s["name"] for s in (intake.get("required_sections") or [])
        if s.get("mandatory") and s.get("name") and s["name"] not in brief.sections
    ]
    if required:
        brief.sections = brief.sections + required
    # Añadir restricciones clave como requisitos del brief
    new_reqs = [c for c in (intake.get("key_constraints") or []) if c]
    if new_reqs:
        brief.key_requirements = brief.key_requirements + new_reqs
    # Aplicar overrides de formato si los detectó el intake
    fmt_overrides = intake.get("format_overrides") or {}
    if fmt_overrides:
        for key, val in fmt_overrides.items():
            if val is not None and key in brief.format_spec:
                brief.format_spec[key] = val


def _research_content(session: ProjectSession) -> str:
    """Texto compacto de la fase de investigación, para el gate cruzado."""
    a = session.analysis
    if a:
        return "\n".join([
            f"Proyecto: {a.project_title}",
            f"Financiador: {a.funder.name} ({a.funder.type}) · {a.funder.url}",
            f"Deadline: {a.funder.deadline} · Monto: {a.total_amount}",
            f"Viabilidad: {a.viability_score:.0f}/100 · Prob. ganar: {a.winning_probability:.0f}%",
            "Lineamientos nacionales: " + "; ".join(a.national_guidelines),
            "Lineamientos internacionales: " + "; ".join(a.international_guidelines),
            "Fuentes (evidencia): " + json.dumps(a.evidence_sources, ensure_ascii=False)[:4000],
            "Análisis:\n" + (a.raw_analysis or ""),
        ])
    b = session.brief
    if b:
        return "\n".join([
            f"Instrucciones: {b.instructions}",
            f"Fuentes encontradas: {b.source_notes}",
            "Lineamientos nacionales: " + "; ".join(b.national_guidelines),
            "Lineamientos internacionales: " + "; ".join(b.international_guidelines),
        ])
    return ""


def _fallback_brief(session: ProjectSession, doc_type) -> "DocumentBrief":
    """Brief mínimo construido solo desde el tipo de documento y la solicitud del
    usuario. GARANTIZA que siempre se pueda redactar un entregable aunque la fase de
    investigación haya fallado o su gate nunca haya aprobado — el sistema nunca debe
    quedarse sin producir un documento."""
    from models.schemas import DocumentBrief
    from models.doc_types import all_criteria
    return DocumentBrief(
        doc_type_key=doc_type.key,
        title=(session.user_input or doc_type.name).strip()[:120] or doc_type.name,
        language="es",
        personas=list(doc_type.personas),
        sections=list(doc_type.sections),
        format_spec=doc_type.format.as_dict(),
        instructions=(session.user_input or "").strip(),
        needs_budget_excel=doc_type.needs_budget_excel,
        evaluation_criteria=all_criteria(doc_type),
        rigor_notes=doc_type.rigor_notes,
    )


def run_scouting(
    *,
    user_input: str,
    api_key: Optional[str] = None,
    session_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> ProjectSession:
    """Detecta el TOP-N de oportunidades (modo búsqueda) y guarda un reporte con
    calificación ponderada. NO genera propuestas: el usuario elige después.

    Las oportunidades quedan en `session.analysis.alternatives` (índice 0 = la mejor),
    serializadas dentro del jsonb `analysis`. La oportunidad #0 también puebla los
    campos principales del AnalysisResult para la tarjeta-resumen de la UI.
    """
    api_key = api_key or ANTHROPIC_API_KEY
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY no configurada")
    session_id = session_id or uuid.uuid4().hex[:8]

    session = ProjectSession(
        session_id=session_id, user_input=user_input, input_mode="search",
        doc_type_key="propuesta", owner_user_id=owner_user_id,
    )
    _mark_running(session_id, owner_user_id, user_input=user_input,
                  mode="search", doc_type_key="propuesta")
    # Simetría con run_pipeline: si un usuario pausó una sesión de scouting
    # mientras estaba "running" (el endpoint /pause lo permite), el flag queda
    # escrito para siempre porque run_scouting nunca lo consulta ni lo limpia.
    repository.clear_pause_requested(session_id)
    _log(session_id, "scout_start", "Iniciando búsqueda de oportunidades", "🔍", "running",
         "Investigando convocatorias activas con financiamiento no reembolsable")
    try:
        # La búsqueda (web + LLM) puede tardar varios minutos sin emitir progreso;
        # sin latido, el watchdog marcaría la sesión "sin actividad".
        _stop, _ = _phase_heartbeater(session_id, "scout_start",
                                      "Buscando oportunidades", "🔍")
        try:
            opportunities = scout.run(session, api_key)
        finally:
            _stop.set()
        if not opportunities:
            _log(session_id, "scout_end", "Sin oportunidades verificables", "🚫", "done")
            session.approved = False
            session.inconclusive_reason = "La búsqueda no devolvió oportunidades verificables."
            save_session(session)
            _mark_completed(session_id, approved=False)
            return session

        _log(session_id, "scout_end",
             f"Búsqueda completada · {len(opportunities)} oportunidad(es) encontrada(s)",
             "🏆", "done",
             " · ".join(o.get("title", "")[:60] for o in opportunities[:3]))
        best = opportunities[0]
        analysis = result_from_data(best, best.get("summary", ""))
        analysis.summary = best.get("summary", "")
        analysis.weighted_score = float(best.get("weighted_score", 0) or 0)
        analysis.alternatives = opportunities      # lista completa ranqueada
        session.analysis = analysis
        session.approved = True                    # scouting completado con éxito

        save_session(session)
        _mark_completed(session_id, approved=True)
        return session
    except Exception as e:
        _mark_failed(session_id, f"{type(e).__name__}: {e}")
        raise


def run_pipeline(
    *,
    user_input: str,
    mode: str = "text",
    doc_type_key: Optional[str] = None,
    template_text: str = "",
    support_docs: Optional[list[tuple[str, str]]] = None,
    api_key: Optional[str] = None,
    session_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    seed_opportunity: Optional[dict] = None,
    is_admin: bool = False,
    allowed_modules: Optional[list[str]] = None,
) -> ProjectSession:
    """Corre el pipeline por fases con gates ≥90 y reinicio al inicio. Devuelve la sesión.

    Persiste progresivamente (pending → running → approved/failed) y al final
    guarda archivos vía save_session().
    """
    api_key = api_key or ANTHROPIC_API_KEY
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY no configurada")
    if mode not in ("search", "text", "file", "url"):
        raise ValueError(f"mode inválido: {mode!r}")

    session_id = session_id or uuid.uuid4().hex[:8]
    support_docs = support_docs or []

    _mark_running(session_id, owner_user_id, user_input=user_input, mode=mode)

    # FASE 0 — Clasificación (Mistral). Una sola vez para todo el pipeline.
    # Corre ANTES del bucle y usa un LLM: con latido para que no cuente como inactividad.
    _log(session_id, "fase0", "Clasificando tipo de documento", "🔍", "running")
    _stop, _ = _phase_heartbeater(session_id, "fase0", "Clasificando tipo de documento", "🔍")
    try:
        resolved_type = _resolve_doc_type(user_input, template_text, support_docs,
                                           api_key, doc_type_key)
    finally:
        _stop.set()

    # Re-valida el módulo DESPUÉS de conocer el tipo real: el gate en el endpoint HTTP
    # solo pudo revisar el doc_type_key crudo recibido (a menudo "auto"), que aquí el
    # clasificador puede resolver a un tipo de un módulo distinto (ej. "auto" -> "tesis",
    # módulo "investigacion") sin que el llamador tuviera ese módulo asignado.
    if allowed_modules is not None and not is_admin:
        resolved_module = get_doc_type(resolved_type).module
        has_access = (
            ("proyectos" in allowed_modules or "investigacion" in allowed_modules)
            if resolved_module == "ambos" else resolved_module in allowed_modules
        )
        if not has_access:
            _mark_failed(
                session_id,
                f"No tienes el módulo '{resolved_module}' asignado, necesario para generar "
                f"un documento de tipo '{resolved_type}'. Pídele al administrador que te lo otorgue.",
            )
            return ProjectSession(
                session_id=session_id, user_input=user_input, input_mode=mode,
                doc_type_key=resolved_type, template_text=template_text,
                support_docs=support_docs, owner_user_id=owner_user_id,
            )

    session = ProjectSession(
        session_id=session_id,
        user_input=user_input,
        input_mode=mode,
        doc_type_key=resolved_type,
        template_text=template_text,
        support_docs=support_docs,
        owner_user_id=owner_user_id,
    )

    # El usuario ENTREGÓ material (subió una propuesta, bases, TDR o una plantilla):
    # el material fuente ya está en la mano. En ese caso NO se hace búsqueda web de
    # financiamiento y los gates se ajustan a "analizar/mejorar el documento
    # entregado" en vez de "verificar fuentes web con URL".
    #   · Se excluye el modo "url" (Enlaces): ahí la fase de investigación DESCARGA los
    #     enlaces que el usuario proporcionó — no debe omitirse aunque adjunte un doc.
    #   · Un `seed` (oportunidad elegida en scouting) NO es material entregado por el
    #     usuario: ahí sí se investiga la convocatoria.
    doc_driven = (
        (bool(support_docs) or bool((template_text or "").strip()))
        and mode in ("file", "text")
        and not seed_opportunity
    )

    # Un pipeline que arranca (fresco o "Continuar") nunca debe quedar pre-pausado:
    # sin esto, una sesión pausada una vez se repausaba de inmediato para siempre.
    repository.clear_pause_requested(session_id)

    # ── Reanudación tras pausa: reutiliza investigación/redacción ya aprobadas ──
    resumed_state = None
    resume_cycle_offset = 0
    # Checkpoints de grano fino consumidos SOLO en el primer paso reanudado (luego
    # el pipeline se comporta como siempre). Evitan repetir la redacción multipasada
    # y los gates ya superados si la sesión murió DESPUÉS de aprobarlos.
    resume_gate2 = False
    resume_gate3 = False
    try:
        resumed_state = repository.load_resumable_state(session_id)
    except Exception:
        resumed_state = None
    if resumed_state:
        resume_gate2 = bool(resumed_state.get("gate2_passed")) and bool(resumed_state.get("proposal_versions"))
        resume_gate3 = resume_gate2 and bool(resumed_state.get("gate3_passed"))
        session.analysis = resumed_state["analysis"]
        session.brief = resumed_state["brief"]
        session.financial = resumed_state["financial"]
        session.proposal_versions = resumed_state["proposal_versions"] or []
        if session.proposal_versions:
            session.final_proposal = session.proposal_versions[-1]
        # Rehidrata el historial de reviews en memoria: sin esto, el primer
        # save_session tras reanudar (delete-then-insert por session_id) borraba de
        # la BD todas las revisiones de los ciclos previos a la pausa.
        session.review_results = resumed_state.get("review_results") or []
        session.research_approved = True
        # Continúa la numeración de ciclos donde se había pausado, en vez de
        # reiniciar en 1 (antes load_resumable_state calculaba current_cycle sin
        # que nada lo consumiera).
        resume_cycle_offset = int(resumed_state.get("current_cycle") or 0)
        _log(session_id, "resume", "Reanudando sesión pausada", "▶",
             "done", "Se reutiliza la investigación ya aprobada — no se repite la búsqueda web")

    _log(session_id, "fase0", "Clasificando tipo de documento", "🔍", "done",
         f"Tipo detectado: {resolved_type}")

    try:
        doc_type = get_doc_type(resolved_type)

        # ── FASE 0.5 — INTAKE (una sola vez; los docs de entrada no cambian) ──
        _log(session_id, "fase0_5", "Analizando documentos de entrada", "📄", "running")
        _stop, _ = _phase_heartbeater(session_id, "fase0_5", "Analizando documentos de entrada", "📄")
        try:
            session.intake_data = intake.analyze(session)
        finally:
            _stop.set()
        _log(session_id, "fase0_5", "Documentos analizados", "📄", "done")

        # Contexto organizacional (Empresas/) — cargado una sola vez
        empresas_context = empresas_util.context_block()

        approved = False
        # Mejor versión lograda (por si ningún intento alcanza el 90).
        best = {"score": -1.0, "proposal": None, "financial": None}
        # Feedback acumulado de gates fallidos, para que el reintento no redacte "a ciegas".
        pending_corrections: list[str] = []
        # Una vez Gate 1 aprueba la investigación, los reinicios por Gate 2/3/4 NO
        # deben repetirla: es la fase más cara (búsqueda web + varias llamadas LLM).
        # Si se está reanudando una sesión pausada, ya viene en True (ver arriba).
        research_approved = session.research_approved
        pipeline_start = time.monotonic()

        for attempt in range(1, MAX_PIPELINE_RESTARTS + 1):
            session.attempts = attempt
            session.current_cycle = resume_cycle_offset + attempt
            cycle_label = f" (ciclo {session.current_cycle})" if session.current_cycle > 1 else ""

            elapsed = time.monotonic() - pipeline_start
            if elapsed > config.MAX_PIPELINE_WALLCLOCK_SEC:
                _log(session_id, "timeout",
                     f"Techo de tiempo alcanzado ({elapsed/60:.0f} min) — se entrega la mejor versión",
                     "⏱️", "warning", f"Límite: {config.MAX_PIPELINE_WALLCLOCK_SEC/60:.0f} min")
                break  # sale del for y entrega la mejor versión lograda, igual que al agotar intentos

            if _pause_if_requested(session_id, session, research_approved):
                return session

            if not research_approved:
                # ── FASE 1 — INVESTIGACIÓN WEB + ANÁLISIS (IA investigadora) ──
                _log(session_id, "fase1", f"Investigando fuentes y analizando{cycle_label}",
                     "🌐", "running", "Mistral busca convocatorias y verifica elegibilidad")
                try:
                    if doc_type.is_proposal:
                        analysis = researcher.run(session, api_key, seed=seed_opportunity)
                        session.analysis = analysis
                        # Un NO-GO solo detiene la búsqueda de oportunidades (modos
                        # search/url/text sin documento): ahí no hay nada que entregar.
                        # Pero si el usuario SUBIÓ un documento (una propuesta ya escrita),
                        # su intención es que la ANALICEMOS y produzcamos el entregable
                        # mejorado — cortar con "NO-GO" y no devolver nada sería justo lo
                        # que el usuario reporta como "no funciona". Se continúa a redactar.
                        if not analysis.viable and not doc_driven:
                            _log(session_id, "fase1", "Análisis: oportunidad no viable", "🚫", "done")
                            session.approved = False
                            session.inconclusive_reason = "Análisis de viabilidad: NO-GO."
                            save_session(session)
                            _mark_completed(session_id, approved=False)
                            return session
                        session.brief = classifier.analysis_to_brief(analysis, resolved_type)
                    else:
                        session.brief = researcher.build_brief(session, resolved_type, api_key)
                except Exception as research_err:
                    _log(session_id, "fase1",
                         f"Error en investigación (ciclo {attempt}) — reintentando",
                         "⚠️", "warning", str(research_err)[:200])
                    if attempt < MAX_PIPELINE_RESTARTS:
                        continue  # reintenta el ciclo completo
                    # Último ciclo: la investigación falló, pero igual hay que ENTREGAR.
                    # Construye un brief mínimo desde el tipo + la solicitud del usuario
                    # y sale del loop hacia la garantía de entrega (redacta de todos modos).
                    if session.brief is None:
                        session.brief = _fallback_brief(session, doc_type)
                    if not session.inconclusive_reason:
                        session.inconclusive_reason = (
                            "La investigación web falló; el documento se redactó con la "
                            "información disponible. Revísalo y complétalo antes de usarlo.")
                    _log(session_id, "fase1",
                         "Investigación no disponible — se redactará con un brief mínimo "
                         "(garantía de entrega)", "⚠️", "warning")
                    break  # → entrega garantizada tras el loop

                # ── VERIFICACIÓN FEHACIENTE DE URLS ──────────────────────────
                # Comprueba HTTP real de cada fuente citada y del funder_url.
                # No bloquea el pipeline si falla: es best-effort.
                try:
                    from utils.url_verifier import enrich_evidence_sources, verify_single
                    if session.analysis and session.analysis.evidence_sources:
                        session.analysis.evidence_sources = enrich_evidence_sources(
                            session.analysis.evidence_sources)
                    if session.analysis and getattr(session.analysis, "funder", None):
                        funder = session.analysis.funder
                        if funder and getattr(funder, "url", None):
                            funder_status = verify_single(funder.url)
                            session.analysis.funder.url_status = funder_status
                            if funder_status not in ("activo", "acceso_restringido"):
                                session.analysis.funder.url = None  # no mostrar URL muerta
                    n_verified = sum(
                        1 for s in (session.analysis.evidence_sources or [])
                        if s.get("verification") == "verificado")
                    _log(session_id, "fase1",
                         f"URLs verificadas: {n_verified}/{len(session.analysis.evidence_sources or [])} activas",
                         "🔗", "done")
                except Exception:
                    pass  # verificación es best-effort

                # ── VERIFICACIÓN FEHACIENTE DE FECHA DE CIERRE ───────────────
                try:
                    from utils.deadline_checker import verify_deadline
                    if session.analysis and getattr(session.analysis, "funder", None):
                        funder = session.analysis.funder
                        dl = verify_deadline(
                            funder_url=getattr(funder, "url", "") or "",
                            llm_deadline_text=getattr(funder, "deadline", "") or "",
                        )
                        funder.deadline = dl["deadline_text"]
                        funder.deadline_iso = dl.get("deadline_iso") or ""
                        funder.deadline_status = dl["status"]
                        funder.deadline_dias = dl.get("dias_restantes")
                        funder.deadline_label = dl["label"]
                        detail = ""
                        if dl.get("discrepancia"):
                            detail = (f"⚠️ La fecha del LLM ({dl.get('deadline_llm_iso')}) no "
                                      f"coincide con la de la página oficial — se usó la web.")
                        _log(session_id, "fase1", f"Convocatoria: {dl['label']}", "📅", "done", detail)
                except Exception:
                    pass

                _log(session_id, "fase1", f"Investigación completada{cycle_label}", "🌐", "done",
                     f"Viabilidad: {getattr(session.analysis, 'viability_score', '—')}/100" if session.analysis else "")

                # Enriquecer el brief con los requisitos del intake (secciones, restricciones)
                _enrich_brief_with_intake(session)

                # ── GATE 1 — la investigación la audita OTRA IA ──────────────
                _log(session_id, "gate1", f"Gate 1: auditando investigación{cycle_label}",
                     "🔎", "running", "Codestral verifica fuentes reales y cobertura de lineamientos")
                # El foco del gate depende de la fuente de la investigación: si el
                # usuario ENTREGÓ el documento, no hubo búsqueda web (por diseño) y exigir
                # "fuentes con URL" haría fallar el gate SIEMPRE, marcando inconcluso un
                # análisis correcto. En ese caso se audita la FIDELIDAD del análisis frente
                # al documento entregado, no la presencia de URLs web.
                if doc_driven:
                    g1_focus = (
                        "El usuario ENTREGÓ el documento fuente (una propuesta/bases ya escritas); "
                        "NO se realizó búsqueda web y es correcto que no haya URLs externas. "
                        "Verifica que el análisis refleje FIELMENTE el documento entregado, que "
                        "identifique correctamente sus componentes (objetivo, metodología, "
                        "presupuesto, beneficiarios, sostenibilidad, marco lógico) y que las "
                        "recomendaciones para mejorarlo sean concretas. NO exijas fuentes web con "
                        "URL ni penalices por su ausencia; sí marca como crítico cualquier dato "
                        "que contradiga el documento o que haya sido inventado."
                    )
                else:
                    g1_focus = (
                        "Verifica que la investigación esté fundamentada en fuentes reales "
                        "(con URL), sin datos inventados, y que cubra lineamientos y requisitos."
                    )
                # Si el investigador cayó en fallback al proveedor que hace de Gate 1,
                # ese proveedor no debe auditar su propia salida: se elige otro.
                g1_provider = _reviewer_avoiding(
                    ROLE_REVIEW_RESEARCH, _last_used_builder(session, research=True),
                    config.ROLE_RESEARCH)
                _stop, _ = _phase_heartbeater(session_id, "gate1",
                                              f"Gate 1: auditando investigación{cycle_label}", "🔎")
                try:
                    g1 = phase_review.review(
                        g1_provider, phase="investigación",
                        brief=session.brief, content=_research_content(session),
                        focus=g1_focus,
                    )
                finally:
                    _stop.set()
                session.phase_reviews.append({"attempt": attempt, **g1})
                # RUTA DOCUMENTO ENTREGADO: Gate 1 audita la CALIDAD DE INVESTIGACIÓN
                # WEB, que no aplica cuando el usuario ya entregó el material fuente. Aquí
                # es ADVISORY: sus observaciones alimentan al redactor como correcciones,
                # pero NO fuerzan reinicio (evita 10 ciclos inútiles) ni marcan el
                # entregable como "inconcluso por investigación". El contenido igual lo
                # filtran Gate 2 (redacción), Gate 3 (paquete) y el veredicto final 90/90.
                if doc_driven and not g1["passed"]:
                    pending_corrections = (g1.get("critical") or []) + (g1.get("issues") or [])
                    _log(session_id, "gate1",
                         f"Gate 1 (advisory, documento entregado){cycle_label}", "📝", "done",
                         f"Puntaje: {g1.get('score', '—')}/100 · observaciones enviadas al redactor")
                    _keep_quality_notes(session, "Gate 1 — análisis del documento", g1)
                    research_approved = True
                    session.research_approved = True
                    try:
                        repository.save_session(session)
                    except Exception:
                        pass
                elif not g1["passed"] and _gate_force_pass(session, g1, attempt):
                    # Escape-hatch coherente con Gate 2/3: tras GATE_FORCE_PASS_AFTER
                    # intentos con score dentro del margen, deja de REINVESTIGAR (la
                    # fase más cara) por un 'critical' persistente y trata Gate 1 como
                    # aprobado-advisory — sus observaciones alimentan al redactor y el
                    # veredicto final 90/90 decide. _gate_force_pass ya excluye los
                    # tipos estrictos y solo dispara desde el intento GATE_FORCE_PASS_AFTER.
                    pending_corrections = (g1.get("critical") or []) + (g1.get("issues") or [])
                    _log(session_id, "gate1",
                         f"Gate 1 forzado tras {attempt} intentos (puntaje {g1.get('score','—')}/100) — "
                         f"se continúa a redacción; lo decide el veredicto final{cycle_label}",
                         "⏭️", "warning")
                    _keep_quality_notes(session, "Gate 1 — investigación", g1)
                    research_approved = True
                    session.research_approved = True
                    try:
                        repository.save_session(session)
                    except Exception:
                        pass
                elif not g1["passed"]:
                    pending_corrections = (g1.get("critical") or []) + (g1.get("issues") or [])
                    if attempt < MAX_PIPELINE_RESTARTS:
                        _log(session_id, "gate1", f"Gate 1 no aprobado — reiniciando{cycle_label}",
                             "🔄", "warning",
                             f"Puntaje: {g1.get('score', '—')}/100 · " +
                             "; ".join(pending_corrections[:3] or [g1.get("recommendation", "")])[:200])
                        continue  # VUELVE AL INICIO: reinvestiga
                    # Último ciclo: NO bloquear la entrega. Acepta la mejor investigación
                    # lograda y procede a redactar para SIEMPRE entregar un documento.
                    # (No se marca research_approved: si el usuario reintenta luego, la
                    # investigación sí se repetirá para intentar superar el gate.)
                    _log(session_id, "gate1",
                         f"Gate 1 sin aprobar en el último ciclo — se continúa para "
                         f"entregar el mejor documento posible{cycle_label}", "⚠️", "warning",
                         f"Puntaje: {g1.get('score', '—')}/100")
                    if not session.inconclusive_reason:
                        session.inconclusive_reason = (
                            "La investigación no superó el gate de calidad; el documento se "
                            "entrega para revisión manual.")
                else:
                    _log(session_id, "gate1", f"Gate 1 aprobado{cycle_label}", "✅", "done",
                         f"Puntaje: {g1.get('score', '—')}/100")
                    _keep_quality_notes(session, "Gate 1 — investigación", g1)
                    research_approved = True
                    session.research_approved = True
                    # Las críticas de un Gate 1 FALLIDO de un ciclo anterior ya se
                    # consumieron al reinvestigar; no deben filtrarse al redactor como
                    # "correcciones del revisor" de redacción. Solo Gate 2/3/veredicto
                    # deben alimentar pending_corrections de aquí en adelante.
                    pending_corrections = []
                    # Checkpoint best-effort: antes solo se persistía en pausa manual
                    # explícita. Un crash/redeploy a mitad del loop (hasta 10 ciclos)
                    # perdía la investigación ya aprobada y /retry la repetía desde
                    # cero. Con MAX_PIPELINE_RESTARTS=10 el costo de NO checkpointear
                    # aquí creció; guardar solo analysis/brief (repository.save_session,
                    # no utils.output.save_session — este último además escribe
                    # Word/Excel a disco en cada ciclo, que sería un desperdicio aquí).
                    try:
                        repository.save_session(session)
                    except Exception:
                        pass
            else:
                _log(session_id, "fase1", f"Investigación ya aprobada — se reutiliza{cycle_label}",
                     "🌐", "done", "Gate 1 ya había pasado; no se repite la búsqueda web")

            if _pause_if_requested(session_id, session, research_approved):
                return session

            if _wallclock_exceeded(pipeline_start):
                _log(session_id, "timeout",
                     f"Techo de tiempo alcanzado — se entrega la mejor versión{cycle_label}",
                     "⏱️", "warning", f"Límite: {config.MAX_PIPELINE_WALLCLOCK_SEC/60:.0f} min")
                break

            if resume_gate2:
                # Reanudación: la redacción ya había pasado Gate 2 → se reutiliza sin
                # repetir la fase más cara. Solo aplica al primer paso reanudado.
                resume_gate2 = False
                proposal = session.proposal_versions[-1]
                session.final_proposal = proposal
                _log(session_id, "gate2",
                     f"Redacción y Gate 2 ya aprobados — se reutiliza{cycle_label}", "✅", "done",
                     "Checkpoint reanudado: no se repite la redacción")
            else:
                # Empieza una redacción nueva: el borrador aún no está gateado.
                session.gate2_passed = False
                session.gate3_passed = False

                # ── FASE 2 — REDACCIÓN (IA redactora) ────────────────────────────
                _log(session_id, "fase2", f"Redactando documento{cycle_label}",
                     "✍️", "running", "Codestral estructura y redacta la propuesta completa")
                # try/except simétrico a Fase 1/3: writer.run rota y cae a Claude, pero
                # si TODOS los proveedores fallan lanza LLMError. Sin capturarla, el
                # except externo marcaba 'failed' con un error crudo y NUNCA llegaba a la
                # garantía de entrega — el usuario quedaba sin nada en el ciclo 1.
                try:
                    proposal = writer.run(session, pending_corrections, api_key, provider=ROLE_WRITER)
                except Exception as write_err:  # noqa: BLE001
                    _log(session_id, "fase2",
                         f"Error en redacción (ciclo {attempt}) — reintentando",
                         "⚠️", "warning", str(write_err)[:200])
                    if attempt < MAX_PIPELINE_RESTARTS:
                        continue  # reintenta el ciclo (Fase 1 se salta: research_approved=True)
                    if not session.inconclusive_reason:
                        session.inconclusive_reason = (
                            "La redacción falló tras agotar los proveedores; se intenta "
                            "entregar el mejor documento posible. Revísalo antes de usarlo.")
                    break  # → garantía de entrega tras el loop
                pending_corrections = []  # ya se aplicaron en esta redacción
                session.proposal_versions.append(proposal)
                session.final_proposal = proposal
                _log(session_id, "fase2", f"Redacción completada{cycle_label}", "✍️", "done",
                     f"{len(proposal):,} caracteres generados")

                # ── GATE 2 — la redacción la audita OTRA IA ──────────────────────
                _log(session_id, "gate2", f"Gate 2: auditando redacción{cycle_label}",
                     "🔎", "running", "Mistral verifica secciones, formato y rigor")
                # Si el redactor cayó en fallback al proveedor que hace de Gate 2, se
                # elige otro para no auditar su propia salida (revisión cruzada real).
                g2_provider = _reviewer_avoiding(
                    ROLE_REVIEW_WRITER, _last_used_builder(session, cycle=session.current_cycle),
                    ROLE_WRITER)
                _stop, _ = _phase_heartbeater(session_id, "gate2",
                                              f"Gate 2: auditando redacción{cycle_label}", "🔎")
                try:
                    g2 = phase_review.review(
                        g2_provider, phase="redacción",
                        brief=session.brief, content=proposal,
                        focus="Verifica secciones completas, cumplimiento de formato y lineamientos, "
                              "rigor y ausencia de relleno o datos inventados.",
                    )
                finally:
                    _stop.set()
                session.phase_reviews.append({"attempt": attempt, **g2})
                if not g2["passed"] and not _gate_force_pass(session, g2, attempt):
                    pending_corrections = (g2.get("critical") or []) + (g2.get("issues") or [])
                    _log(session_id, "gate2", f"Gate 2 no aprobado — reiniciando{cycle_label}",
                         "🔄", "warning",
                         f"Puntaje: {g2.get('score', '—')}/100 · " +
                         "; ".join(pending_corrections[:3] or [g2.get("recommendation", "")])[:200])
                    continue  # VUELVE AL INICIO
                if not g2["passed"]:
                    _log(session_id, "gate2",
                         f"Gate 2 forzado tras {attempt} intentos (puntaje {g2.get('score','—')}/100) — "
                         f"lo decide el veredicto final{cycle_label}", "⏭️", "warning")
                else:
                    _log(session_id, "gate2", f"Gate 2 aprobado{cycle_label}", "✅", "done",
                         f"Puntaje: {g2.get('score', '—')}/100")
                _keep_quality_notes(session, "Gate 2 — redacción", g2)
                # Checkpoint: si la sesión muere ahora, al reanudar se salta la redacción.
                session.gate2_passed = True
                try:
                    repository.save_session(session)
                except Exception:
                    pass

            if _pause_if_requested(session_id, session, research_approved):
                return session

            if _wallclock_exceeded(pipeline_start):
                _log(session_id, "timeout",
                     f"Techo de tiempo alcanzado — se entrega la mejor versión{cycle_label}",
                     "⏱️", "warning", f"Límite: {config.MAX_PIPELINE_WALLCLOCK_SEC/60:.0f} min")
                break

            if resume_gate3:
                # Reanudación: el paquete ya había pasado Gate 3 → se salta financiero
                # y Gate 3 (el financiero ya viene rehidratado desde el checkpoint).
                resume_gate3 = False
                _log(session_id, "gate3",
                     f"Presupuesto y Gate 3 ya aprobados — se reutiliza{cycle_label}", "✅", "done",
                     "Checkpoint reanudado: no se repite el paquete")
            else:
                # ── FASE 3 — ESTRUCTURACIÓN FINANCIERA (IA financiera) ───────────
                if session.brief and session.brief.needs_budget_excel:
                    _log(session_id, "fase3", f"Estructurando presupuesto{cycle_label}",
                         "💰", "running", "Codestral genera la estructura financiera")
                    _stop, _ = _phase_heartbeater(session_id, "fase3",
                                                  f"Estructurando presupuesto{cycle_label}", "💰")
                    try:
                        session.financial = financial.run(
                            session, proposal, api_key, provider=ROLE_FINANCIAL)
                        _log(session_id, "fase3", f"Presupuesto completado{cycle_label}",
                             "💰", "done")
                    except Exception:
                        session.financial = None
                        _log(session_id, "fase3", "Presupuesto omitido (error no crítico)",
                             "💰", "warning")
                    finally:
                        _stop.set()

                # ── GATE 3 — DeepSeek revisa el paquete completo ─────────────────
                _log(session_id, "gate3", f"Gate 3: revisión del paquete completo{cycle_label}",
                     "🔎", "running", "DeepSeek audita propuesta + presupuesto + requisitos")
                _stop, _ = _phase_heartbeater(session_id, "gate3",
                                              f"Gate 3: revisión del paquete completo{cycle_label}", "🔎")
                try:
                    g3 = phase_review.review_package(
                        brief=session.brief,
                        proposal=proposal,
                        financial=session.financial,
                        intake_data=session.intake_data,
                        empresas_context=empresas_context,
                    )
                finally:
                    _stop.set()
                session.phase_reviews.append({"attempt": attempt, **g3})
                if not g3["passed"] and not _gate_force_pass(session, g3, attempt):
                    pending_corrections = (g3.get("critical") or []) + (g3.get("issues") or [])
                    # El borrador va a reescribirse → invalida el checkpoint de Gate 2.
                    session.gate2_passed = False
                    session.gate3_passed = False
                    _log(session_id, "gate3", f"Gate 3 no aprobado — reiniciando{cycle_label}",
                         "🔄", "warning",
                         f"Puntaje: {g3.get('score', '—')}/100 · " +
                         "; ".join(pending_corrections[:3] or [g3.get("recommendation", "")])[:200])
                    continue  # VUELVE AL INICIO: reinvestiga y reescribe
                if not g3["passed"]:
                    _log(session_id, "gate3",
                         f"Gate 3 forzado tras {attempt} intentos (puntaje {g3.get('score','—')}/100) — "
                         f"lo decide el veredicto final{cycle_label}", "⏭️", "warning")
                else:
                    _log(session_id, "gate3", f"Gate 3 aprobado{cycle_label}", "✅", "done",
                         f"Puntaje: {g3.get('score', '—')}/100")
                _keep_quality_notes(session, "Gate 3 — paquete completo", g3)
                # Checkpoint: si la sesión muere ahora, al reanudar se salta directo a Fase 4.
                session.gate3_passed = True
                try:
                    repository.save_session(session)
                except Exception:
                    pass

            if _pause_if_requested(session_id, session, research_approved):
                return session

            if _wallclock_exceeded(pipeline_start):
                _log(session_id, "timeout",
                     f"Techo de tiempo alcanzado — se entrega la mejor versión{cycle_label}",
                     "⏱️", "warning", f"Límite: {config.MAX_PIPELINE_WALLCLOCK_SEC/60:.0f} min")
                break

            # ── FASE 4 — VEREDICTO FINAL (Claude, o consenso de 2 revisores) ──
            dual = bool(session.brief and session.brief.doc_type_key in config.SECOND_OPINION_DOC_TYPES)
            _log(session_id, "fase4", f"Veredicto final{cycle_label}",
                 "⚖️", "running",
                 "Consenso de 2 revisores independientes (máxima exigencia)" if dual
                 else "Claude evalúa con criterios de máxima exigencia")
            _stop, _ = _phase_heartbeater(session_id, "fase4", f"Veredicto final{cycle_label}", "⚖️")
            try:
                review = reviewer.run_dual(session, proposal, api_key) if dual \
                    else reviewer.run(session, proposal, api_key)
            except Exception as verdict_err:  # noqa: BLE001
                # El juez final (Claude) era el único punto sin manejo de error al
                # cierre: una caída/rate-limit tiraba la corrida con un error crudo y
                # se saltaba save_session (Word/Excel), pese a que el borrador YA existe.
                # Se degrada a "entrega la mejor versión lograda" (garantía de entrega).
                _log(session_id, "fase4",
                     f"Veredicto final no disponible — se entrega el mejor borrador{cycle_label}",
                     "⚠️", "warning", str(verdict_err)[:200])
                if not session.inconclusive_reason:
                    session.inconclusive_reason = (
                        "El veredicto final no pudo ejecutarse (proveedor no disponible); "
                        "se entrega el documento ya redactado para revisión manual.")
                break  # → garantía de entrega + save_session tras el loop
            finally:
                _stop.set()
            session.review_results.append(review)

            if review.overall_score > best["score"]:
                best = {"score": review.overall_score, "proposal": proposal,
                        "financial": session.financial}

            if review.approved:
                _log(session_id, "fase4", f"¡Aprobado 90/90!{cycle_label}",
                     "🏆", "done", f"Puntaje final: {review.overall_score:.0f}/100")
                approved = True
                break
            pending_corrections = list(getattr(review, "corrections", None) or [])
            # El veredicto rechazó → el borrador se reescribe: invalida los checkpoints
            # de Gate 2/3 para que un resume no reutilice un borrador ya descartado.
            session.gate2_passed = False
            session.gate3_passed = False
            _log(session_id, "fase4", f"No aprobado — reiniciando{cycle_label}",
                 "🔄", "warning",
                 f"Puntaje: {review.overall_score:.0f}/100 — bajo el umbral 90 · " +
                 "; ".join(pending_corrections[:3])[:200])
            # No aprobó el 90/90 → VUELVE AL INICIO (reinvestiga)

        session.approved = approved

        # ── GARANTÍA DE ENTREGA ──────────────────────────────────────────────
        # Si ningún ciclo llegó siquiera a redactar (Gate 1 nunca aprobó y el techo
        # de tiempo cortó el loop antes del último ciclo, o la investigación falló),
        # produce igualmente el mejor documento posible: el sistema NUNCA debe
        # devolver "nada" ante una solicitud válida.
        if best["proposal"] is None and not session.final_proposal:
            if session.brief is None:
                session.brief = _fallback_brief(session, doc_type)
                _enrich_brief_with_intake(session)
            try:
                _log(session_id, "entrega",
                     "Generando el entregable final (garantía de entrega)", "📝", "running",
                     "No se superaron los gates de calidad dentro del tiempo disponible; "
                     "se entrega el mejor documento posible para revisión manual")
                proposal = writer.run(session, pending_corrections, api_key, provider=ROLE_WRITER)
                session.proposal_versions.append(proposal)
                session.final_proposal = proposal
                best = {"score": 0.0, "proposal": proposal, "financial": session.financial}
                if not session.inconclusive_reason:
                    session.inconclusive_reason = (
                        "Entregado sin superar todos los gates de calidad; revísalo antes de usarlo.")
                _log(session_id, "entrega", "Entregable generado", "📝", "done",
                     f"{len(proposal):,} caracteres")
            except Exception as gen_err:  # noqa: BLE001
                _log(session_id, "entrega", "No se pudo generar el entregable de respaldo",
                     "⚠️", "warning", str(gen_err)[:200])

        # Si ningún intento alcanzó el 90, entrega la MEJOR versión (inconclusa).
        if not approved and best["proposal"] is not None:
            session.final_proposal = best["proposal"]
            session.financial = best["financial"]
            if session.proposal_versions and session.proposal_versions[-1] != best["proposal"]:
                session.proposal_versions.append(best["proposal"])
            session.inconclusive_reason = (
                f"No se alcanzó 90/100 tras {session.attempts} intentos. "
                f"Mejor puntaje: {best['score']:.0f}/100. Se entrega la mejor versión."
            )

        # ── Estadística (descriptiva + avanzada) si el documento trae datos ──
        if session.brief and session.final_proposal:
            _stop, _ = _phase_heartbeater(session_id, "stats", "Calculando estadística", "📊")
            try:
                import agents.statistics as statistics
                session.brief.statistics = statistics.run(
                    session, session.final_proposal, api_key)
            except Exception:
                pass
            finally:
                _stop.set()

        save_session(session)
        _mark_completed(session_id, approved=approved)
        return session

    except Exception as e:
        _mark_failed(session_id, f"{type(e).__name__}: {e}")
        raise
