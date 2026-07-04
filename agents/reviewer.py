"""
AGENTE 3 — REVISOR Y CONTROLADOR DE CALIDAD (multi-formato)
Evalúa CUALQUIER tipo de entregable con máximo rigor, usando los criterios propios del
tipo de documento (definidos en el DocumentBrief) más el cumplimiento de formato y de
lineamientos nacionales e internacionales. Aplica la REGLA 90/90:
  aprueba SOLO si cada elemento ≥ 90 Y el global ≥ 90 (y sin problemas críticos).
La extensión (palabras/caracteres/páginas) se verifica de forma mecánica, no por el LLM.
"""
import json
import anthropic
import config
from config import MODEL, MAX_TOKENS_REVIEWER, APPROVAL_THRESHOLD, ELEMENT_THRESHOLD
from models.schemas import DocumentBrief, ReviewResult
from models.doc_types import FormatSpec, get_doc_type
from tools.document_builder import text_stats, section_coverage, citation_stats

# Tipos con bibliografía real de literatura externa (no aplica a peer_review, que
# cita el manuscrito ajeno que está evaluando, ni a tdr/propuesta, sin bibliografía
# académica exigida).
_CITATION_CHECK_DOC_TYPES = {"tesis", "articulo_cientifico"}


def _clip_preserve_tail(text: str, n: int, tail_chars: int = 18000) -> str:
    """Trunca preservando SIEMPRE el inicio completo y el tramo final (conclusiones,
    referencias, anexos — las secciones con más riesgo de citas inventadas o
    incoherencia, y las que el truncado ciego por el frente nunca dejaba ver).
    Si el documento cabe entero, lo devuelve sin tocar."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    head_budget = n - tail_chars - 200
    if head_budget <= 0:
        return text[:n] + "\n[…truncado…]"
    head = text[:head_budget]
    tail = text[-tail_chars:]
    return (f"{head}\n\n[…tramo intermedio omitido por límite de longitud — "
            f"se preservó íntegro el inicio y el tramo final (conclusiones/referencias/anexos)…]\n\n{tail}")


SYSTEM_PROMPT = """
Eres un panel de evaluación de máxima exigencia: reúnes a un evaluador senior de organismos
multilaterales (BID/Banco Mundial/Unión Europea), un árbitro PhD de revista indexada Q1, un
jurista especialista en normativa pública internacional y un editor académico con 25 años de
experiencia. Tu estándar es el más alto posible — calibrado, honesto y sin concesiones. No inflás
puntajes para complacer; si algo no es excelente, tu dictamen lo dice con precisión quirúrgica,
con las correcciones exactas que el redactor necesita para subsanarlo en el siguiente ciclo.

Tu valor está en la precisión del diagnóstico: identificas exactamente qué eleva un documento a
la excelencia y qué lo detiene. Cada corrección que emites es específica, accionable e indica
concretamente dónde y cómo intervenir — no correcciones genéricas como "mejorar la redacción",
sino "el párrafo 3 de la sección de justificación carece de datos cuantitativos que respalden
la magnitud del problema; agregar cifra oficial de [fuente] con año".

REGLA 90/90 (ESTRICTA — SIN EXCEPCIONES):
Un documento se aprueba ÚNICAMENTE si cumple AMBAS condiciones de forma simultánea:
  (a) CADA UNO de los criterios evaluados alcanza ≥ 90/100, Y
  (b) el puntaje GLOBAL es ≥ 90/100.
Si aunque sea UN solo criterio queda por debajo de 90, el resultado es NO aprobado — sin importar
cuán alto sea el promedio global. Un financiador internacional rechazará una propuesta con una sola
sección débil; el revisor aplica el mismo estándar.

Evalúas exactamente los criterios indicados (propios del tipo de documento) más el cumplimiento
de formato y de lineamientos nacionales e internacionales/organizacionales.

Responde ÚNICAMENTE con JSON válido siguiendo el esquema indicado. Sin texto extra.
"""


def _build_prompt(brief: DocumentBrief, proposal: str, cycle: int, stats: dict,
                  coverage: dict, threshold: int, quality_notes: list | None = None,
                  citations: dict | None = None) -> str:
    dt = get_doc_type(brief.doc_type_key)
    fmt = FormatSpec.from_dict(brief.format_spec)
    criteria = brief.evaluation_criteria
    criteria_block = "\n".join(f'  - "{c}"' for c in criteria)
    # plantilla JSON de scores
    scores_json = ",\n".join(f'    "{c}": <0-100>' for c in criteria)

    nat_str = "\n".join(f"  - {g}" for g in brief.national_guidelines) or "  - (marco nacional aplicable)"
    intl_str = "\n".join(f"  - {g}" for g in brief.international_guidelines) or "  - (normas del organismo/área)"

    stats_str = (
        f"palabras={stats['word_count']}, caracteres={stats['char_count']}, "
        f"páginas estimadas={stats['page_estimate']}, dentro de límites={stats['within_limits']}"
    )
    stats_issues = "; ".join(stats["issues"]) if stats["issues"] else "ninguno"
    coverage_str = (
        f"presentes: {len(coverage['covered'])}/{len(coverage['covered']) + len(coverage['missing'])}"
        + (f" — AUSENTES (verificado mecánicamente, no por lectura del LLM): "
           f"{', '.join(coverage['missing'])}" if coverage["missing"] else " — todas presentes")
    )
    notes_block = ""
    if quality_notes:
        notes_block = (
            "\nOBSERVACIONES DE GATES PREVIOS QUE YA APROBARON (no críticas, pero verifica que "
            "se hayan atendido en esta versión):\n"
            + "\n".join(f"  - {n}" for n in quality_notes) + "\n"
        )

    citations_block = ""
    if citations and citations.get("has_references_section"):
        problems = []
        if citations["orphan_citations"]:
            problems.append("CITAS SIN REFERENCIA CORRESPONDIENTE (posible cita inventada o "
                            "bibliografía incompleta): " + ", ".join(citations["orphan_citations"][:15]))
        if citations["unused_references"]:
            problems.append(f"{len(citations['unused_references'])} referencia(s) listada(s) pero "
                            "nunca citada(s) en el texto")
        if citations.get("broken_links"):
            problems.append("DOI/URL de referencias que NO resuelven (verificado con HTTP real): "
                            + "; ".join(citations["broken_links"][:8]))
        citations_block = (
            f"\nVERIFICACIÓN MECÁNICA DE CITAS Y REFERENCIAS (sobre el texto real, no una lectura "
            f"tuya — {citations['n_citations']} citas detectadas, {citations['n_references']} "
            f"referencias listadas):\n"
            + ("\n".join(f"  - {p}" for p in problems) if problems else "  - Sin hallazgos: citas y referencias cuadran entre sí.")
            + "\n"
        )

    return f"""
Evalúa el siguiente entregable con máximo rigor, aplicando la REGLA {threshold}/{threshold}.

TIPO DE DOCUMENTO: {dt.name}
TÍTULO: {brief.title}
IDIOMA REQUERIDO: {brief.language}
CICLO: {cycle}
UMBRAL: ≥{threshold} global Y ≥{threshold} en CADA criterio.
{f"NIVEL ACADÉMICO A EXIGIR: {brief.academic_level} — calibra tu exigencia a ESTE nivel exacto (los criterios de originalidad/rigor adicionales ya están listados abajo, pero el nivel te da el ancla de calibración explícita)." if getattr(brief, 'academic_level', '') and brief.doc_type_key == 'tesis' else ""}

FORMATO EXIGIDO: {fmt.to_prompt()}
MEDICIÓN AUTOMÁTICA DE EXTENSIÓN (ya calculada, tómala como dato duro): {stats_str}
INCUMPLIMIENTOS DE EXTENSIÓN DETECTADOS: {stats_issues}
(Si hay incumplimientos de extensión, el criterio "Cumplimiento de Formato" NO puede llegar a {threshold}.)

COBERTURA DE SECCIONES OBLIGATORIAS (verificación mecánica sobre el propio texto,
no una afirmación tuya — tómala como hecho duro): {coverage_str}
{citations_block}{notes_block}
LINEAMIENTOS NACIONALES (Ecuador) A VERIFICAR:
{nat_str}

LINEAMIENTOS INTERNACIONALES/ORGANIZACIONALES A VERIFICAR:
{intl_str}

CRITERIOS A CALIFICAR (cada uno 0-100):
{criteria_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DOCUMENTO A EVALUAR:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_clip_preserve_tail(proposal, 60000)}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Responde ÚNICAMENTE con este JSON (sin texto antes ni después):

{{
  "overall_score": <0-100>,
  "criterion_scores": {{
{scores_json}
  }},
  "failing_elements": ["<criterio < 90 y por qué>"],
  "compliance_checklist": ["<ítem normativo/editorial verificado y su estado>"],
  "strengths": ["<fortaleza concreta 1>", "<fortaleza 2>", "<fortaleza 3>"],
  "critical_issues": ["<problema crítico que impide aprobación, si lo hay>"],
  "corrections": ["<corrección específica y accionable 1 (ataca primero lo < 90)>", "<2>", "<3>", "<4>", "<5>"],
  "recommendation": "<resumen de 100-150 palabras explicando la decisión>"
}}

NOTA: NO incluyas "approved": el sistema lo calcula con la regla 90/90.
Califica TODOS los criterios listados. Si alguno < 90, corrections debe traer todo lo necesario.
"""


def _parse_result(raw: str) -> dict:
    from utils.json_utils import robust_json_loads
    return robust_json_loads(raw)


def _prepare_evaluation(session, proposal: str):
    """Cómputo compartido entre run() (Claude) y _run_with_provider() (segunda
    opinión no-Claude): umbrales, verificación mecánica y el prompt — para que
    ambos revisores evalúen exactamente lo mismo, con el mismo rigor."""
    brief: DocumentBrief = session.brief
    cycle = session.current_cycle
    dt = get_doc_type(brief.doc_type_key)

    # Umbral propio del tipo de documento si es más estricto que el genérico
    # (tesis/artículo científico/peer-review/TDR no deberían aprobarse con la
    # misma exigencia que un documento rutinario — ver DocType.strict_threshold).
    # dt.strict_threshold, si existe, sube AMBOS umbrales (global y por criterio)
    # a la vez; si no, cada uno conserva su propio valor de config.py.
    approval_threshold = dt.strict_threshold or APPROVAL_THRESHOLD
    element_threshold = dt.strict_threshold or ELEMENT_THRESHOLD

    stats = text_stats(proposal, brief.format_spec)
    coverage = section_coverage(proposal, brief.sections)

    citations = None
    if brief.doc_type_key in _CITATION_CHECK_DOC_TYPES:
        fmt_for_cites = FormatSpec.from_dict(brief.format_spec)
        citations = citation_stats(proposal, fmt_for_cites.citation_style)
        if citations.get("has_references_section") and citations.get("links_to_verify"):
            try:
                from utils.url_verifier import verify_urls
                urls = [(l.get("url") or (f"https://doi.org/{l['doi']}" if l.get("doi") else ""))
                       for l in citations["links_to_verify"]]
                urls = [u for u in urls if u]
                states = verify_urls(urls)
                citations["broken_links"] = [u for u in urls if states.get(u) == "url_muerta"]
            except Exception:
                citations["broken_links"] = []  # verificación de enlaces es best-effort

    prompt = _build_prompt(brief, proposal, cycle, stats, coverage, element_threshold,
                           getattr(session, "quality_notes", None), citations)
    return brief, cycle, approval_threshold, element_threshold, stats, coverage, citations, prompt


def _finalize_result(raw_text: str, brief: DocumentBrief, cycle: int, stats: dict,
                     coverage: dict, citations: dict | None,
                     approval_threshold: int, element_threshold: int) -> ReviewResult:
    """Parsea la respuesta del LLM auditor y aplica TODAS las penalizaciones/bloqueos
    mecánicos (formato, cobertura de secciones, citas) — compartido por cualquier
    proveedor que haga de revisor, para que el estándar sea idéntico entre ellos."""
    data = _parse_result(raw_text)

    # Normalizar puntajes para EXACTAMENTE los criterios del brief
    raw_scores = data.get("criterion_scores", {}) or {}
    criterion_scores = {}
    for c in brief.evaluation_criteria:
        criterion_scores[c] = float(raw_scores.get(c, 0) or 0)

    result = ReviewResult(
        approved=False,  # se recalcula con la regla de aprobación
        overall_score=float(data.get("overall_score", 0) or 0),
        cycle=cycle,
        criterion_scores=criterion_scores,
        format_check=stats,
        strengths=data.get("strengths", []),
        corrections=data.get("corrections", []),
        critical_issues=data.get("critical_issues", []),
        compliance_checklist=data.get("compliance_checklist", []),
        recommendation=data.get("recommendation", ""),
    )

    # ── Penalización mecánica de formato: si la extensión incumple, ese criterio cae ──
    fmt_label = next((c for c in criterion_scores if c.lower().startswith("cumplimiento de formato")), None)
    if fmt_label and not stats["within_limits"]:
        criterion_scores[fmt_label] = min(criterion_scores[fmt_label], element_threshold - 10.0)

    # ── Penalización/bloqueo MECÁNICO de cobertura de secciones (no depende de que
    # el LLM "se dé cuenta" leyendo el texto — la verificación es sobre el texto real) ──
    if coverage["missing"]:
        if fmt_label:
            criterion_scores[fmt_label] = min(criterion_scores[fmt_label], element_threshold - 5.0)
        # ≥3 secciones obligatorias ausentes (o más de un tercio de ellas) es una señal
        # demasiado fuerte de documento incompleto para dejarlo pasar por que el LLM
        # auditor no lo haya marcado como crítico.
        if len(coverage["missing"]) >= 3 or coverage["coverage_ratio"] < 0.67:
            result.critical_issues = list(result.critical_issues) + [
                "Verificación mecánica: faltan secciones obligatorias del brief: "
                + ", ".join(coverage["missing"])
            ]

    # ── Penalización/bloqueo MECÁNICO de citas y referencias: el fallo de calidad
    # más grave en un documento académico es una cita/referencia inventada, y esto
    # no depende de que el LLM auditor la detecte leyendo el texto ──────────────
    cite_label = next((c for c in criterion_scores
                       if "referencia" in c.lower() or "cita" in c.lower()), None)
    if citations and citations.get("has_references_section"):
        n_orphan = len(citations["orphan_citations"])
        n_broken = len(citations.get("broken_links") or [])
        if (n_orphan or n_broken) and cite_label:
            criterion_scores[cite_label] = min(criterion_scores[cite_label], element_threshold - 10.0)
        # Varias citas huérfanas (no 1-2, que pueden ser ruido del parser heurístico)
        # o cualquier DOI/URL de referencia que no resuelve son señal fuerte de cita
        # fabricada — se bloquea aunque el LLM auditor no lo haya marcado crítico.
        if n_orphan >= 3 or n_broken > 0:
            result.critical_issues = list(result.critical_issues) + [
                "Verificación mecánica de citas: "
                + (f"{n_orphan} cita(s) sin referencia correspondiente. " if n_orphan >= 3 else "")
                + (f"{n_broken} referencia(s) con DOI/URL que no resuelve(n)." if n_broken else "")
            ]

    # ── REGLA DE APROBACIÓN ESTRICTA (calculada en código, no por el LLM) ──────
    result.failing_elements = [
        f"{name}: {score:.0f}/100" for name, score in criterion_scores.items()
        if score < element_threshold
    ]
    if not stats["within_limits"]:
        result.failing_elements.append("Extensión fuera de límites: " + "; ".join(stats["issues"]))
    if coverage["missing"]:
        result.failing_elements.append("Secciones ausentes: " + ", ".join(coverage["missing"]))

    all_ok = all(s >= element_threshold for s in criterion_scores.values())
    result.approved = (
        result.overall_score >= approval_threshold
        and all_ok
        and stats["within_limits"]
        and not result.critical_issues
    )
    return result


def run(session, proposal: str, api_key: str) -> ReviewResult:
    from agents._client import make_client
    client = make_client(api_key)
    brief, cycle, approval_threshold, element_threshold, stats, coverage, citations, prompt = \
        _prepare_evaluation(session, proposal)

    response = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS_REVIEWER, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return _finalize_result(response.content[0].text, brief, cycle, stats, coverage,
                            citations, approval_threshold, element_threshold)


def _run_with_provider(provider: str, session, proposal: str) -> ReviewResult:
    """Mismo veredicto que run(), pero con un proveedor no-Claude (Mistral/Codestral/
    DeepSeek) como segunda opinión independiente — ver run_dual()."""
    from agents import llm
    brief, cycle, approval_threshold, element_threshold, stats, coverage, citations, prompt = \
        _prepare_evaluation(session, proposal)
    raw = llm.complete(provider, system=SYSTEM_PROMPT, prompt=prompt,
                       max_tokens=MAX_TOKENS_REVIEWER, temperature=0.2)
    return _finalize_result(raw, brief, cycle, stats, coverage, citations,
                            approval_threshold, element_threshold)


def run_dual(session, proposal: str, api_key: str) -> ReviewResult:
    """Veredicto por CONSENSO de 2 revisores independientes para los tipos de máxima
    exigencia (ver config.SECOND_OPINION_DOC_TYPES): Claude evalúa igual que en run(),
    y un proveedor no-Claude (config.SECOND_OPINION_PROVIDER) evalúa el MISMO
    documento de forma independiente. Se aprueba SOLO si ambos aprueban; si
    divergen, se fusionan sus correcciones para el siguiente ciclo. Ningún jurado
    doctoral ni comité editorial real aprueba con un solo árbitro.

    Ambos veredictos quedan en session.review_results (el de la segunda opinión se
    añade aquí; el de Claude/fusionado lo añade el pipeline como de costumbre). Si
    el segundo proveedor falla, cae automáticamente al veredicto de un solo revisor.
    """
    result_a = run(session, proposal, api_key)
    try:
        result_b = _run_with_provider(config.SECOND_OPINION_PROVIDER, session, proposal)
    except Exception as ex:
        result_a.recommendation = (
            (result_a.recommendation or "")
            + f"\n\n[Segunda opinión ({config.SECOND_OPINION_PROVIDER}) no disponible: "
              f"{type(ex).__name__}: {ex} — veredicto de un solo revisor.]"
        )
        return result_a

    result_b.recommendation = f"[Segunda opinión — {config.SECOND_OPINION_PROVIDER}] {result_b.recommendation}"
    # db.reviews tiene UNIQUE(session_id, cycle) y ambos veredictos comparten
    # session.current_cycle por defecto — insertar los dos con el mismo cycle
    # viola esa restricción y hace fallar save_session() al final del pipeline
    # (marcando "failed" un documento que en realidad sí se completó). El
    # negativo del ciclo distingue la fila de la segunda opinión sin requerir
    # una migración de esquema (la columna es "int not null", sin CHECK > 0).
    result_b.cycle = -result_b.cycle
    session.review_results.append(result_b)

    # Fusiona en result_a (que el pipeline añadirá a session.review_results como de
    # costumbre): aprobación por consenso, score conservador (el más bajo de los
    # dos), y unión de hallazgos de ambos revisores.
    result_a.recommendation = f"[Revisor 1 — Claude] {result_a.recommendation}\n\n{result_b.recommendation}"
    result_a.approved = result_a.approved and result_b.approved
    result_a.overall_score = min(result_a.overall_score, result_b.overall_score)
    result_a.critical_issues = list(dict.fromkeys(result_a.critical_issues + result_b.critical_issues))
    result_a.corrections = list(dict.fromkeys(result_a.corrections + result_b.corrections))
    result_a.failing_elements = list(dict.fromkeys(result_a.failing_elements + result_b.failing_elements))
    return result_a
