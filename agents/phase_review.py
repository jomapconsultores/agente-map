"""
GATE DE FASE — revisión cruzada por OTRA IA (≥90/100)
──────────────────────────────────────────────────────
Cada fase del pipeline (investigación, redacción) es auditada por una IA DISTINTA
a la que la produjo. Si el puntaje global es <90 o hay problemas críticos, la fase
NO pasa y el pipeline vuelve al inicio (reinvestiga).

Además incluye review_package() — GATE 3 que usa DeepSeek para revisar el paquete
completo (propuesta + financiero + cumplimiento de plantilla/docs de entrada) antes
de pasar al veredicto final de Claude.

El veredicto FINAL del documento lo da Claude con la regla 90/90 estricta
(agents/reviewer.py); este módulo son los gates intermedios.
"""
from __future__ import annotations

import json
import time

import config
from agents import llm
from models.doc_types import FormatSpec, get_doc_type
from models.schemas import DocumentBrief

MAX_TOKENS_GATE = 1500

# Tipos de máxima exigencia/riesgo reputacional: si el gate técnico falla por
# completo (proveedor caído, JSON irreparable) tras agotar reintentos, NO se
# aprueba automáticamente (fail-open) — se rechaza (fail-closed) y el pipeline
# reintenta, en vez de dejar pasar sin que ninguna IA haya auditado nada.
_STRICT_FAIL_CLOSED_TYPES = {"tesis", "articulo_cientifico", "peer_review", "tdr"}


def _complete_with_retries(provider: str, *, system: str, prompt: str,
                           max_tokens: int, temperature: float, retries: int = 2) -> str:
    """Reintenta con backoff corto en el MISMO proveedor antes de rendirse — un
    fallo transitorio (timeout, rate-limit) no debe apagar de facto la auditoría
    cruzada de calidad para toda una fase."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return llm.complete(provider, system=system, prompt=prompt,
                                max_tokens=max_tokens, temperature=temperature)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_err  # type: ignore[misc]

_SYSTEM = """
Eres un auditor de calidad independiente, riguroso y calibrado. Tu función es evaluar si una fase
del trabajo cumple el estándar profesional mínimo para avanzar en el pipeline. No inflás puntajes:
si algo no está a nivel, lo dices con precisión y señalas exactamente qué falta y cómo subsanarlo.

Verificas tres cosas con igual rigor:
  - Que no haya datos, cifras, fuentes ni citas inventadas (cualquier invención es "critical").
  - Que se respeten los lineamientos nacionales e internacionales requeridos.
  - Que el formato y la estructura exigida estén correctamente aplicados.

Tus correcciones son específicas y accionables — no "mejorar el contenido", sino "la sección X
carece de Y; agregar Z con fuente real". Respondes ÚNICAMENTE con JSON válido:
{"score": <0-100>, "critical": ["<problema que obliga a rehacer — dato inventado / incumplimiento grave>", ...],
 "issues": ["<observación concreta y accionable>", ...],
 "strengths": ["<fortaleza real>", ...], "recommendation": "<resumen 1-3 frases>"}
Sin texto fuera del JSON.
"""


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "\n[…truncado…]"


def _parse(raw: str) -> dict:
    from utils.json_utils import robust_json_loads
    return robust_json_loads(raw)


def _brief_context(brief: DocumentBrief) -> str:
    dt = get_doc_type(brief.doc_type_key)
    fmt = FormatSpec.from_dict(brief.format_spec)
    nat = "\n".join(f"  - {g}" for g in brief.national_guidelines) or "  - (marco nacional aplicable)"
    intl = "\n".join(f"  - {g}" for g in brief.international_guidelines) or "  - (normas del área)"
    reqs = "\n".join(f"  - {r}" for r in brief.key_requirements) or "  - (según el tipo)"
    secs = "\n".join(f"  - {s}" for s in brief.sections) or "  - (según el tipo)"
    return (
        f"TIPO: {dt.name}\nTÍTULO: {brief.title}\nIDIOMA: {brief.language}\n"
        f"FORMATO EXIGIDO: {fmt.to_prompt()}\n"
        f"SECCIONES:\n{secs}\nREQUISITOS:\n{reqs}\n"
        f"LINEAMIENTOS NACIONALES:\n{nat}\nLINEAMIENTOS INTERNACIONALES:\n{intl}\n"
    )


def review(provider: str, *, phase: str, brief: DocumentBrief,
           content: str, focus: str = "") -> dict:
    """Audita una fase. Devuelve dict con score/passed/critical/issues/strengths.

    `passed` = score ≥ PHASE_REVIEW_THRESHOLD y sin problemas críticos.
    Resiliente: si el revisor falla, deja pasar la fase (no bloquea por una caída),
    el veredicto final de Claude seguirá filtrando.
    """
    prompt = f"""
Evalúa esta FASE: «{phase}».

{_brief_context(brief)}
{focus}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTENIDO A AUDITAR:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_clip(content, 48000)}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Asigna un score 0-100 al cumplimiento de esta fase frente a lo exigido. Si hay datos/fuentes
inventados, faltan lineamientos clave o el formato no se respeta, el score debe ser <90 y debes
listarlo en "critical". Devuelve SOLO el JSON.
"""
    threshold = config.PHASE_REVIEW_THRESHOLD
    try:
        raw = _complete_with_retries(provider, system=_SYSTEM, prompt=prompt,
                                     max_tokens=MAX_TOKENS_GATE, temperature=0.2)
        data = _parse(raw)
        score = float(data.get("score", 0) or 0)
        critical = [str(c) for c in (data.get("critical") or [])]
        issues = [str(i) for i in (data.get("issues") or [])]
        strengths = [str(s) for s in (data.get("strengths") or [])]
        passed = score >= threshold and not critical
        return {
            "phase": phase, "provider": provider, "score": score,
            "passed": passed, "critical": critical, "issues": issues,
            "strengths": strengths,
            "recommendation": str(data.get("recommendation", "")),
        }
    except Exception as ex:  # el gate técnico falló tras agotar reintentos
        fail_closed = brief.doc_type_key in _STRICT_FAIL_CLOSED_TYPES
        print(f"[phase_review] GATE {'FAIL-CLOSED' if fail_closed else 'OMITIDO (fail-open)'} "
              f"fase={phase!r} provider={provider!r} doc_type={brief.doc_type_key!r}: "
              f"{type(ex).__name__}: {ex}")
        return {
            "phase": phase, "provider": provider,
            "score": 0.0 if fail_closed else float(threshold),
            "passed": not fail_closed,
            "critical": (["Auditoría técnica no pudo ejecutarse tras reintentos — "
                          "tipo de máxima exigencia: no se aprueba automáticamente."]
                         if fail_closed else []),
            "issues": [], "strengths": [],
            "recommendation": f"(gate omitido: {type(ex).__name__} — "
                              f"{'fail-closed' if fail_closed else 'fail-open'})",
        }


_PACKAGE_SYSTEM = """
Eres un auditor senior de convocatorias internacionales con décadas de experiencia evaluando
paquetes completos de propuestas antes de su entrega a financiadores de la más alta exigencia.
Tu juicio es independiente, calibrado y sin concesiones: si el paquete no está listo, lo dices.

Tu auditoría del paquete completo (documento + presupuesto + cumplimiento de plantilla) cubre:
  1. Cumplimiento de TODAS las secciones y campos obligatorios del formulario/bases detectado.
  2. Datos organizacionales reales (razón social, RUC, representante legal, CV de participantes)
     correctamente integrados — sin datos genéricos ni inventados.
  3. Coherencia interna entre narrativa de la propuesta, presupuesto y marco lógico.
  4. Respeto de límites de extensión, formato y criterios de evaluación.
  5. Ausencia de datos, cifras, fuentes o citas inventadas.

Cada observación en "critical" señala concretamente el campo o sección problemática y por qué
constituye un impedimento para entregar. Respondes ÚNICAMENTE con JSON válido.
"""

_PACKAGE_JSON = (
    '{"score": <0-100>, "critical": ["<problema que impide entregar>", ...],'
    ' "issues": ["<campo incompleto o inconsistencia concreta>", ...],'
    ' "strengths": ["<fortaleza>", ...], "recommendation": "<resumen 1-3 frases>"}'
)


def review_package(
    *,
    brief: "DocumentBrief",
    proposal: str,
    financial=None,
    intake_data: dict | None = None,
    empresas_context: str = "",
) -> dict:
    """GATE 3 (DeepSeek): revisa el paquete completo antes del veredicto final de Claude.

    Verifica cumplimiento de plantilla, datos reales de organizaciones, consistencia
    financiera y ausencia de datos inventados. Si no pasa → el pipeline reinicia.
    """
    provider = config.ROLE_PACKAGE_REVIEW  # deepseek

    intake_block = ""
    if intake_data:
        sections = intake_data.get("required_sections") or []
        constraints = intake_data.get("key_constraints") or []
        if sections or constraints:
            sec_lines = "\n".join(
                f"  - [{('OBLIGATORIO' if s.get('mandatory') else 'opcional')}] "
                f"{s.get('name','')}: {s.get('description','')}"
                for s in sections
            )
            con_lines = "\n".join(f"  - {c}" for c in constraints)
            intake_block = (
                "\nSECCIONES Y CAMPOS REQUERIDOS POR LA PLANTILLA/BASES:\n"
                + (sec_lines or "  (ninguno detectado)")
                + "\n\nRESTRICCIONES CRÍTICAS:\n"
                + (con_lines or "  (ninguna)")
            )

    financial_block = ""
    if financial:
        n_budget = len(getattr(financial, "budget_items", []))
        n_logic = len(getattr(financial, "logframe_rows", []))
        n_sched = len(getattr(financial, "schedule_rows", []))
        financial_block = (
            f"\nPRESUPUESTO ESTRUCTURADO: {n_budget} rubros | "
            f"Marco lógico: {n_logic} filas | Cronograma: {n_sched} actividades\n"
            f"Narrativa presupuestaria: {(getattr(financial,'budget_narrative','') or '')[:1000]}\n"
        )

    org_block = ""
    if empresas_context:
        org_block = f"\nCONTEXTO ORGANIZACIONAL DISPONIBLE:\n{empresas_context[:3000]}\n"

    prompt = f"""
Audita el PAQUETE COMPLETO de esta propuesta.

{_brief_context(brief)}
{intake_block}
{org_block}
{financial_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DOCUMENTO PRINCIPAL (propuesta):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_clip(proposal, 40000)}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Verifica:
1. ¿Están cubiertas TODAS las secciones/campos obligatorios de la plantilla?
2. ¿Los datos organizacionales (razón social, RUC, representante, CVs) son reales
   y provienen del contexto organizacional disponible, no inventados?
3. ¿El presupuesto es coherente con la narrativa? ¿Los montos cuadran?
4. ¿Se respetan los límites de extensión y criterios de evaluación?
5. ¿Hay datos, fuentes o cifras inventadas?

Devuelve SOLO: {_PACKAGE_JSON}
"""
    threshold = config.PHASE_REVIEW_THRESHOLD
    try:
        raw = _complete_with_retries(
            provider, system=_PACKAGE_SYSTEM, prompt=prompt,
            max_tokens=2000, temperature=0.2,
        )
        data = _parse(raw)
        score = float(data.get("score", 0) or 0)
        critical = [str(c) for c in (data.get("critical") or [])]
        issues = [str(i) for i in (data.get("issues") or [])]
        strengths = [str(s) for s in (data.get("strengths") or [])]
        passed = score >= threshold and not critical
        return {
            "phase": "paquete completo", "provider": provider, "score": score,
            "passed": passed, "critical": critical, "issues": issues,
            "strengths": strengths,
            "recommendation": str(data.get("recommendation", "")),
        }
    except Exception as ex:
        fail_closed = brief.doc_type_key in _STRICT_FAIL_CLOSED_TYPES
        print(f"[phase_review] GATE 3 {'FAIL-CLOSED' if fail_closed else 'OMITIDO (fail-open)'} "
              f"provider={provider!r} doc_type={brief.doc_type_key!r}: {type(ex).__name__}: {ex}")
        return {
            "phase": "paquete completo", "provider": provider,
            "score": 0.0 if fail_closed else float(threshold),
            "passed": not fail_closed,
            "critical": (["Auditoría técnica del paquete no pudo ejecutarse tras reintentos — "
                          "tipo de máxima exigencia: no se aprueba automáticamente."]
                         if fail_closed else []),
            "issues": [], "strengths": [],
            "recommendation": f"(gate omitido: {type(ex).__name__} — "
                              f"{'fail-closed' if fail_closed else 'fail-open'})",
        }
