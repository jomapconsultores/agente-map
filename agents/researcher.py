"""
AGENTE 1' — INVESTIGADOR WEB (proveedor-agnóstico, NO-Claude)
─────────────────────────────────────────────────────────────
Hace la búsqueda en web y la investigación que antes hacía Claude, pero con una
IA constructora (por defecto Mistral, configurable con ROLE_RESEARCH).

Como Mistral/Codestral/DeepSeek exponen una API tipo chat/completions sin el
tool-use propio del SDK de Anthropic, la búsqueda se orquesta en código:
  1) `deep_search` (DuckDuckGo, GRATIS) ejecuta las queries y DESCARGA páginas reales.
  2) La evidencia se entrega como texto al LLM.
  3) El LLM lee esa evidencia y produce el JSON de análisis / brief.

IMPORTANTE: usa RESEARCHER_SYSTEM_PROMPT en lugar de analyst.SYSTEM_PROMPT para
evitar que los LLMs no-Claude intenten "llamar herramientas" que no existen en
esta ruta de API (el tool-use es exclusivo del SDK de Anthropic).
"""
from __future__ import annotations

import json
import re

import config
from agents import llm
import agents.analyst as analyst
from models.doc_types import get_doc_type, all_criteria, FormatSpec
from models.schemas import AnalysisResult, DocumentBrief, ProjectSession
from tools.search import (
    execute_deep_search, execute_fetch_page, opportunity_queries,
)
from config import (
    MAX_TOKENS_ANALYST, SEARCH_FETCH_PAGES,
)


def _beat(session: ProjectSession, label: str, detail: str = "") -> None:
    """Latido de progreso best-effort: mantiene fresco el heartbeat en la BD
    durante fases largas (búsqueda web + análisis LLM) para que el watchdog de
    'sin actividad' NO mate una sesión que en realidad sigue trabajando."""
    try:
        from db import repository
        repository.update_progress(session.session_id, phase="fase1",
                                   label=label, icon="🌐", status="running", detail=detail)
    except Exception:
        pass

# ── System prompt para LLMs no-Claude ────────────────────────────────────────
# No menciona herramientas (deep_search/fetch_page/web_search) porque este
# proveedor recibe la evidencia ya recolectada como texto, no a través de
# tool-use. Usar analyst.SYSTEM_PROMPT aquí confunde a Mistral/Codestral y
# los hace intentar "llamar herramientas" en vez de devolver el JSON pedido.
RESEARCHER_SYSTEM_PROMPT = """
Eres el consultor de financiamiento internacional no reembolsable de mayor calibre en América
Latina: más de 30 años de carrera dedicados exclusivamente a Ecuador y la región, con más de 340
propuestas ganadoras por un valor total superior a $850 millones USD.

Tu tarea es analizar la evidencia de búsqueda web que ya fue recolectada y entregada a continuación,
y producir un análisis de viabilidad riguroso con el JSON exacto que se te pide.

CONTEXTO ECUADOR:
- Constitución 2008: arts. 275-284 (régimen de desarrollo), arts. 395-415 (derechos naturaleza)
- Plan Nacional de Desarrollo vigente (Ejes: Derechos, Economía, Soberanía)
- Ecuador: ODS prioritarios 1, 2, 4, 6, 8, 13, 15 — firmó Agenda 2030
- SENESCYT (cooperación técnica), MAATE (GEF/GCF), elegible para BID/CAF/BM/PNUD/casi toda bilateral

EXHAUSTIVIDAD (NO NEGOCIABLE): exprimes hasta la última gota de la evidencia. Revisas TODA la
evidencia entregada, no solo los primeros resultados; triangulas cada dato importante con más de
una fuente cuando exista; extraes montos, fechas, criterios de elegibilidad, ponderaciones, costos
elegibles, cofinanciamiento y requisitos de formato con precisión quirúrgica. El análisis, las
recomendaciones al redactor y el desglose de viabilidad son densos en datos concretos y verificables,
nunca generalidades. Agotas cada dimensión de viabilidad con su justificación específica.

REGLAS DE RIGOR (NO NEGOCIABLES):
1. Usa ÚNICAMENTE los datos de la evidencia entregada. No inventes URLs ni fechas.
2. Cada dato concreto debe estar respaldado por una URL real presente en la evidencia.
3. Si un dato no aparece en la evidencia, márcalo como "no verificado".
4. Nunca inventes fechas límite — escribe "A determinar" si no aparece en la evidencia.
5. Sin fuentes verificables → declara CONDITIONAL o NO-GO con explicación.

Responde ÚNICAMENTE con el JSON pedido. Sin texto adicional antes ni después del JSON.
Sin bloque de código Markdown, sin comentarios, sin texto extra.
"""

# JSON schema idéntico al de analyst._build_analysis_prompt pero sin "usa web_search"
_ANALYSIS_JSON_SCHEMA = """{
  "viable": true/false,
  "go_no_go": "GO" | "NO-GO" | "CONDITIONAL",
  "viability_score": <número 0-100>,
  "winning_probability": <número 0-100>,
  "project_title": "<título mejorado del proyecto>",
  "funder": {
    "name": "<financiador más adecuado>",
    "type": "<multilateral|bilateral|UN|EU|fundacion|nacional>",
    "url": "<URL convocatoria o web del financiador>",
    "deadline": "<fecha límite EXACTA — ISO YYYY-MM-DD (ej: 2026-09-30) o 'A determinar'>",
    "amount_range": "<rango típico de montos>",
    "language": "<idioma requerido>",
    "sector": "<sector>",
    "country_focus": "<países elegibles>"
  },
  "sector": "<sector principal>",
  "total_amount": "<monto sugerido a solicitar>",
  "duration_months": <meses recomendados>,
  "beneficiaries": "<beneficiarios directos e indirectos>",
  "language": "<idioma de la propuesta>",
  "ecuador_alignment": {
    "legal_framework": "<leyes ecuatorianas aplicables>",
    "plan_nacional": "<alineación Plan Nacional>",
    "ods_aligned": ["ODS X", "ODS Y"],
    "institutional_capacity": "<evaluación capacidad>",
    "national_priority": true/false
  },
  "strengths": ["<fortaleza 1>", "<fortaleza 2>", "<fortaleza 3>"],
  "risks": ["<riesgo 1>", "<riesgo 2>"],
  "critical_success_factors": ["<factor 1>", "<factor 2>"],
  "comparable_projects": ["<referencia 1>", "<referencia 2>"],
  "format_requirements": {
    "sections": ["Resumen ejecutivo", "Antecedentes", "Justificación",
                 "Objetivos", "Metodología", "Marco lógico", "Presupuesto",
                 "Sostenibilidad", "Equipo", "Anexos"],
    "max_pages": null,
    "font_size": "12pt",
    "language": "<idioma>",
    "requires_excel_budget": true/false,
    "budget_template": "<plantilla exigida o null>",
    "special_requirements": "<requisitos especiales>"
  },
  "national_guidelines": ["<lineamiento nacional Ecuador 1>"],
  "international_guidelines": ["<lineamiento del financiador 1>"],
  "key_requirements": ["<requisito 1>"],
  "differentiators": ["<diferenciador 1>"],
  "recommendations": "<instrucciones detalladas para el redactor, mínimo 200 palabras>",
  "raw_analysis": "<análisis completo en prosa, mínimo 400 palabras>",
  "evidence_sources": [
    {
      "claim": "<dato concreto verificado>",
      "source_url": "<URL exacta>",
      "source_title": "<título de la página>",
      "source_quote": "<fragmento textual breve que respalda el claim>",
      "verification": "verificado|inferido|no_verificado"
    }
  ],
  "feasibility_breakdown": {
    "funder_match":           {"score": <0-100>, "reason": "<por qué el tema encaja>"},
    "geographic_eligibility": {"score": <0-100>, "reason": "<Ecuador elegible?>"},
    "deadline_feasibility":   {"score": <0-100>, "reason": "<tiempo suficiente?>"},
    "institutional_fit":      {"score": <0-100>, "reason": "<perfil del proponente>"},
    "budget_fit":             {"score": <0-100>, "reason": "<monto en rango del financiador>"}
  }
}"""


def _build_researcher_prompt(document: str) -> str:
    """Prompt para LLMs no-Claude: la evidencia ya viene incluida, sin tool-use."""
    return (
        "La evidencia de búsqueda web ya fue recolectada y está incluida a continuación.\n"
        "Analiza EXCLUSIVAMENTE esa evidencia para producir el análisis de viabilidad.\n"
        "No intentes buscar más información — todo lo que necesitas está abajo.\n\n"
        "---\n" + document + "\n---\n\n"
        "Con base ÚNICAMENTE en la evidencia anterior, responde con este JSON exacto "
        "(sin texto extra, sin Markdown, sin comentarios):\n\n"
        + _ANALYSIS_JSON_SCHEMA
    )


# ── Utilidades ───────────────────────────────────────────────────────────────
def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "\n[…truncado…]"


def _support_block(session: ProjectSession) -> str:
    docs = getattr(session, "support_docs", None) or []
    if not docs:
        return ""
    joined = "\n\n".join(f"=== {n} ===\n{(t or '')[:6000]}" for n, t in docs)
    return f"\n\nDOCUMENTOS ADJUNTOS POR EL USUARIO (analízalos como material fuente real):\n{joined}\n"


def _propose_queries(provider: str, topic: str, base: list[str]) -> list[str]:
    """La IA investigadora propone consultas dirigidas. Si falla, usa `base`."""
    sys = ("Eres un investigador experto en financiamiento internacional. Devuelves "
           "ÚNICAMENTE un JSON: {\"queries\": [\"...\", ...]} con 8-12 consultas variadas "
           "(español + inglés + francés/portugués si aplica) para encontrar convocatorias, "
           "bases, requisitos y elegibilidad. Sin texto extra.")
    prompt = (f"Tema del usuario: \"{topic}\"\n\n"
              "Propón 8-12 consultas potentes (incluye nombres de financiadores como "
              "BID/CAF/UE/PNUD/GIZ/USAID/GEF/GCF y términos de 'bases/requisitos/elegibilidad').\n"
              "Devuelve SOLO el JSON {\"queries\": [...]}.")
    try:
        raw = llm.complete(provider, system=sys, prompt=prompt, max_tokens=1200, temperature=0.4)
        data = _parse_json(raw)
        qs = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]
        # Combina las propuestas con la base, sin duplicar, conservando orden.
        out, seen = [], set()
        for q in qs + base:
            if q not in seen:
                seen.add(q)
                out.append(q)
        return out[:14] or base
    except Exception:
        return base


def _parse_json(raw: str) -> dict:
    from utils.json_utils import robust_json_loads
    return robust_json_loads(raw)


def _gather_evidence(session: ProjectSession, seed: dict | None = None) -> str:
    """Ejecuta la búsqueda en web y devuelve la evidencia como texto para la IA.

    Si `seed` (una oportunidad ya elegida en el scouting) está presente, la búsqueda
    se ENFOCA en ese financiador/convocatoria: descarga su URL y dirige las queries a
    sus bases, requisitos, formato y elegibilidad.
    """
    provider = config.ROLE_RESEARCH
    topic = (session.user_input or "").strip()
    mode = session.input_mode

    pieces: list[str] = []

    # En modo URL: descarga primero los enlaces entregados por el usuario.
    if mode == "url":
        urls = re.findall(r"https?://\S+", topic)
        for u in urls[:6]:
            page = execute_fetch_page(u)
            pieces.append(f"=== PÁGINA ENTREGADA: {u} ===\n{_clip(page, 6000)}")

    # Oportunidad elegida (scouting → generación): foco en esa entidad.
    if seed:
        funder = (seed.get("funder") or {})
        fname = funder.get("name", "")
        furl = funder.get("url", "")
        if furl and furl.startswith("http"):
            page = execute_fetch_page(furl)
            pieces.append(f"=== BASES DE LA CONVOCATORIA ELEGIDA: {furl} ===\n{_clip(page, 7000)}")
        base = [
            f"{fname} {topic} bases requisitos formato elegibilidad",
            f"{fname} convocatoria {topic} presupuesto plantilla cofinanciamiento",
            f"{fname} {topic} Ecuador criterios de evaluación deadline",
        ] + opportunity_queries(topic)[:6]
        queries = base[:12]
        try:
            evidence = execute_deep_search(queries, fetch_pages=SEARCH_FETCH_PAGES)
        except Exception as ex:
            evidence = json.dumps({"error": f"deep_search falló: {ex}"}, ensure_ascii=False)
        pieces.append("=== EVIDENCIA DE BÚSQUEDA WEB (enfocada) ===\n" + _clip(evidence, 24000))
        return "\n\n".join(pieces)

    # Búsqueda profunda: el paquete estático (35+ queries por categoría) cubre
    # bien los sectores anticipados por keyword-matching (ambiente, urbano,
    # género, educación), pero cualquier sector no anticipado (salud, digital,
    # cultura, derechos humanos, migración...) no recibe ningún refuerzo. Una
    # oleada COMPLEMENTARIA de queries que el propio LLM dirige al sector real
    # del tema rellena ese hueco.
    #
    # Se ejecuta en SECUENCIA (no en paralelo con la oleada estática): cada
    # llamada a execute_deep_search ya abre sus propios ThreadPoolExecutor
    # internos (búsqueda + descarga de páginas); anidar un tercer nivel de
    # threads aquí, ejecutándose además dentro del hilo de un BackgroundTask de
    # Starlette, provocó una caída total del proceso del servidor en pruebas
    # reales (sin traceback — consistente con agotamiento de threads/handles
    # en Windows). El costo es más latencia, no más riesgo de crash.
    base = opportunity_queries(topic)
    try:
        evidence = execute_deep_search(base, fetch_pages=SEARCH_FETCH_PAGES)
    except Exception as ex:  # la búsqueda nunca debe tumbar el pipeline
        evidence = json.dumps({"error": f"deep_search falló: {ex}", "queries": base},
                              ensure_ascii=False)
    pieces.append("=== EVIDENCIA DE BÚSQUEDA WEB (deep_search) ===\n" + _clip(evidence, 28000))
    _beat(session, "Ampliando la búsqueda con consultas dirigidas", "Segunda oleada de fuentes")

    try:
        proposed = _propose_queries(provider, topic, [])
        extra = [q for q in proposed if q not in base][:10]
        evidence_llm = execute_deep_search(extra, fetch_pages=6) if extra else ""
    except Exception:
        evidence_llm = ""
    if evidence_llm:
        pieces.append("=== EVIDENCIA COMPLEMENTARIA (queries dirigidas por IA al sector real del tema) ===\n"
                      + _clip(evidence_llm, 10000))

    return "\n\n".join(pieces)


# ════════════════════════════════════════════════════════════════════════════
#  INVESTIGACIÓN DE PROPUESTAS  →  AnalysisResult
# ════════════════════════════════════════════════════════════════════════════
def run(session: ProjectSession, api_key: str | None = None,
        seed: dict | None = None) -> AnalysisResult:
    """Investiga (web) y produce el análisis de viabilidad con la IA ROLE_RESEARCH.

    Si `seed` (oportunidad elegida en el scouting) está presente, el análisis se
    centra en ESA convocatoria con sus requisitos reales (no re-busca otra)."""
    provider = config.ROLE_RESEARCH

    # ── DOCUMENTO ENTREGADO POR EL USUARIO → se analiza directamente ──────────
    # Cuando el usuario SUBE documentos (una propuesta ya escrita, bases, TDR) el
    # material fuente ya está en la mano. Lanzar la búsqueda web de financiamiento
    # (opportunity_queries, 32 consultas × ~20 s) es: (a) OFF-TOPIC — busca
    # convocatorias en vez de analizar lo que el usuario entregó; (b) LENTO —
    # minutos por ciclo, hasta 10 ciclos; (c) el camino con historial de CRASH del
    # proceso en Windows por anidamiento de ThreadPoolExecutors dentro del
    # BackgroundTask. Para hunting de convocatorias existe el flujo "Buscar".
    # Con un `seed` (oportunidad ya elegida en scouting) sí se investiga esa entidad.
    # Se excluye el modo "url": ahí _gather_evidence DESCARGA los enlaces del usuario.
    doc_driven = (
        (bool(session.support_docs) or bool((session.template_text or "").strip()))
        and session.input_mode in ("file", "text")
    )
    if seed or not doc_driven:
        evidence = _gather_evidence(session, seed=seed)
        _beat(session, "Analizando la evidencia recolectada", "Evaluando viabilidad y financiador")
    else:
        evidence = ""
        _beat(session, "Analizando el documento entregado",
              "Extrayendo componentes y evaluando la propuesta subida")

    seed_block = ""
    if seed:
        seed_block = (
            "\n\nOPORTUNIDAD ELEGIDA POR EL USUARIO (analízala a fondo; NO cambies de "
            "financiador ni de convocatoria):\n" + json.dumps(seed, ensure_ascii=False)[:4000] + "\n"
        )

    # Contexto organizacional (carpeta Empresas/) — para direccionar la propuesta
    empresas_block = ""
    try:
        from utils.empresas import context_block as _eb
        empresas_block = _eb()
        if empresas_block:
            empresas_block = "\n\n" + empresas_block
    except Exception:
        pass

    # Reutiliza el esquema JSON completo del analista (modo "analizar documento"),
    # entregándole la evidencia ya recolectada como material fuente.
    if doc_driven and not evidence:
        # Analiza el DOCUMENTO entregado con todos sus componentes; no hubo búsqueda web.
        closing_rule = (
            "REGLA: El usuario ENTREGÓ el/los documento(s) de arriba (una propuesta ya "
            "escrita, bases o TDR). Analízalo(s) EXHAUSTIVAMENTE con TODOS sus componentes: "
            "objetivo, justificación, metodología, beneficiarios, presupuesto, cronograma, "
            "marco lógico, sostenibilidad, alineación normativa, fortalezas, riesgos y "
            "factores críticos de éxito. NO inventes datos: extrae los REALES del documento. "
            "Si el documento NO especifica financiador/deadline/monto, usa 'A determinar' SIN "
            "penalizar viability_score por ello (la ausencia de una convocatoria concreta no "
            "resta viabilidad al proyecto en sí; evalúa la calidad y coherencia de la propuesta). "
            "En 'recommendations' da instrucciones concretas para mejorar y completar el "
            "documento hasta dejarlo listo para presentar.\n"
            "Al identificar la organización ejecutora/proponente, usa los datos REALES del "
            "documento y de la sección ORGANIZACIONES E INDIVIDUOS DISPONIBLES (si existe arriba)."
        )
    else:
        closing_rule = (
            "REGLA: NO inventes datos. Usa SOLO la evidencia de arriba; cada dato "
            "concreto (financiador, deadline, monto, elegibilidad, criterios) debe estar "
            "respaldado por una URL real presente en la evidencia. Si un dato no aparece, "
            "márcalo como 'no verificado' y baja viability_score en consecuencia.\n"
            "Al identificar la organización ejecutora/proponente, usa los datos REALES de "
            "la sección ORGANIZACIONES E INDIVIDUOS DISPONIBLES (si existe arriba)."
        )
    document = (
        f"TEMA / SOLICITUD DEL USUARIO:\n{session.user_input}\n"
        f"{seed_block}\n"
        f"{evidence}"
        f"{_support_block(session)}"
        f"{empresas_block}\n\n"
        + closing_rule
    )
    # RESEARCHER_SYSTEM_PROMPT es el correcto aquí: no menciona herramientas de tool-use
    # (deep_search/fetch_page/web_search) que confunden a Mistral/Codestral/DeepSeek.
    prompt = _build_researcher_prompt(document)

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            raw, used = llm.complete_builder(
                provider, system=RESEARCHER_SYSTEM_PROMPT, prompt=prompt,
                max_tokens=MAX_TOKENS_ANALYST, anthropic_key=api_key,
                temperature=0.3 + attempt * 0.15)
            data = analyst._parse_result(raw)
            result = analyst.result_from_data(data, raw)
            session.builder_log.append({"phase": "research", "requested": provider, "used": used})
            return result
        except Exception as e:
            last_err = e
            continue
    raise last_err


# ════════════════════════════════════════════════════════════════════════════
#  INVESTIGACIÓN DE OTROS TIPOS  →  DocumentBrief
# ════════════════════════════════════════════════════════════════════════════
_BRIEF_SYSTEM = """
Eres un director editorial e investigador senior de primer orden, con dominio excepcional en
gestión del conocimiento, análisis normativo y arquitectura de documentos de alta complejidad.
Tu especialidad es identificar con exactitud qué debe contener un entregable de alta calidad —
sus secciones obligatorias, su marco legal/normativo nacional e internacional, sus fuentes reales
y su formato preciso — y traducirlo en instrucciones que permiten producir un documento impecable
al primer intento.

Razonas de forma sistemática y sin lagunas: revisas TODA la evidencia disponible, determinas el
estándar real del tipo de documento y defines los criterios de éxito con una claridad que no
deja margen a la interpretación ni al relleno. Eres exhaustivo: las secciones, requisitos, normas
y marcas de calidad que defines agotan lo que un entregable impecable de este tipo debe contener —
no omites nada relevante y cada instrucción es concreta y accionable.

NO inventes fuentes ni normas: usa SOLO la evidencia entregada. Si algo no está en la evidencia,
indícalo como "a verificar" — nunca lo fabules. Respondes ÚNICAMENTE con el JSON pedido.
"""


def build_brief(session: ProjectSession, doc_type_key: str,
                api_key: str | None = None) -> DocumentBrief:
    """Investiga (web) y arma el brief universal con la IA ROLE_RESEARCH."""
    provider = config.ROLE_RESEARCH
    dt = get_doc_type(doc_type_key)
    default_fmt = dt.format.as_dict()
    # Documentos AUTOCONTENIDOS (cotización, proforma, informe, carta, oficio,
    # documento personalizado): la solicitud del usuario y sus documentos de apoyo
    # ya traen lo necesario. _gather_evidence hace una búsqueda web orientada a
    # FINANCIAMIENTO (opportunity_queries) que contamina el brief con contenido
    # off-topic — p. ej. una cotización de software se volvía un "mapeo de
    # convocatorias de financiamiento". Para estos tipos se OMITE la búsqueda y se
    # redacta directamente desde el pedido: más rápido y on-topic.
    # Se OMITE la búsqueda web de financiamiento cuando: (a) el tipo es
    # autocontenido por naturaleza (cotización/informe/carta/oficio/documento
    # personalizado), o (b) el usuario ENTREGÓ documentos (revisión por pares de un
    # manuscrito, análisis de un TDR/bases subido): el material fuente ya está en la
    # mano y opportunity_queries (búsqueda de convocatorias) sería off-topic, lento y
    # con riesgo de crash por threads en Windows.
    doc_driven = (
        (bool(session.support_docs) or bool((session.template_text or "").strip()))
        and session.input_mode in ("file", "text")
    )
    self_contained = dt.key in {"generico", "legal_tecnico"} or doc_driven
    if self_contained:
        evidence = ""
    else:
        evidence = _gather_evidence(session)
        _beat(session, "Analizando la evidencia recolectada",
              "Definiendo estructura y requisitos del documento")
    support_join = "\n\n".join(
        f"=== {n} ===\n{_clip(t, 3500)}" for n, t in (session.support_docs or [])
    )

    prompt = f"""
TIPO DE DOCUMENTO: {dt.name} (clave: {dt.key})
DESCRIPCIÓN: {dt.description}
PERFILES DE EXPERTO BASE: {", ".join(dt.personas)}
SECCIONES SUGERIDAS: {", ".join(dt.sections)}
EXIGENCIA DE RIGOR: {dt.rigor_notes}

FORMATO POR DEFECTO (ajústalo SOLO si la evidencia o la plantilla lo justifican):
{json.dumps(default_fmt, ensure_ascii=False)}

SOLICITUD DEL USUARIO:
{_clip(session.user_input, 4000)}

PLANTILLA/MODELO OPCIONAL A IMITAR:
{_clip(session.template_text, 3000) or "(ninguna)"}

DOCUMENTOS DE APOYO:
{support_join or "(ninguno)"}

{_clip(evidence, 24000) if evidence else "(sin búsqueda web: este documento se define a partir de la solicitud del usuario y sus documentos de apoyo)"}

Céntrate ESTRICTAMENTE en lo que pide la SOLICITUD DEL USUARIO (no cambies de tema
ni introduzcas financiamiento/convocatorias si el usuario no lo pidió). Responde
ÚNICAMENTE con este JSON (sin texto extra):

{{
  "title": "<título preciso del entregable>",
  "language": "es|en|fr|pt",
  "personas": ["<perfil experto 1>", "<perfil 2>"],
  "sections": ["<sección 1>", "<sección 2>"],
  "format_spec": {{
    "font_name": "<tipo de letra>", "font_size": <pt>,
    "line_spacing": <1.0|1.15|1.5|2.0>,
    "margin_top_cm": <cm>, "margin_bottom_cm": <cm>,
    "margin_left_cm": <cm>, "margin_right_cm": <cm>,
    "alignment": "justify|left", "citation_style": "<APA 7|Vancouver|IEEE|... o ''>",
    "max_pages": <int o null>, "min_pages": <int o null>,
    "max_words": <int o null>, "min_words": <int o null>,
    "max_chars": <int o null>, "notes": "<otras exigencias formales reales>"
  }},
  "instructions": "<QUÉ hay que hacer exactamente, ≥200 palabras>",
  "national_guidelines": ["<lineamiento nacional Ecuador 1>"],
  "international_guidelines": ["<norma internacional/organizacional/editorial 1>"],
  "key_requirements": ["<requisito indispensable 1>"],
  "quality_markers": ["<marca de excelencia 1>"],
  "source_notes": "<fuentes y referencias reales encontradas, con datos verificables>",
  "academic_level": "<SOLO si el tipo de documento es académico (p.ej. tesis): 'colegio'|'pregrado'|'maestria'|'doctorado'|'postdoctorado' según lo que indique la solicitud del usuario; si no aplica o no se puede determinar, usa 'pregrado'>"
}}
"""
    # El brief es un JSON compacto (secciones, formato, requisitos): 16k tokens de
    # salida invitaban a que el modelo divagara y TRUNCARA el JSON a mitad → parse
    # fallido. 6k basta de sobra y reduce latencia y riesgo de truncamiento.
    #
    # ROTA de proveedor ante un fallo de PARSEO (no solo ante error de red):
    # algunos modelos (p. ej. Mistral en briefs) divagan y truncan el JSON, lo que
    # NO lanza LLMError, así que complete_builder no rotaba solo. Probar el
    # siguiente constructor rescata en segundos en vez de reintentar al mismo.
    provider_order = [provider] + [p for p in config.BUILDER_ROTATION if p != provider]
    last_err2: Exception | None = None
    raw, used, data = "", "", {}
    for prov in provider_order:
        try:
            raw = llm.complete(prov, system=_BRIEF_SYSTEM, prompt=prompt,
                               max_tokens=6000, temperature=0.3)
            data = _parse_json(raw)
            if data:
                used = prov
                last_err2 = None
                break
        except Exception as e:
            last_err2 = e
            continue
    # Si NINGÚN proveedor entregó un JSON parseable, NO abortes la generación: cae a
    # un brief mínimo derivado del tipo + la solicitud del usuario, para que el
    # pipeline pueda redactar igual (garantía de entrega).
    if not data:
        from core.pipeline import _fallback_brief
        return _fallback_brief(session, dt)

    fmt = FormatSpec.from_dict({**default_fmt, **(data.get("format_spec") or {})})
    academic_level = str(data.get("academic_level") or "").strip().lower()
    evaluation_criteria = all_criteria(dt)

    # Escala de exigencia por nivel académico real (hoy solo aplica a "tesis"): un
    # colegio y un doctorado no deben juzgarse con el mismo piso de extensión ni
    # los mismos criterios — ver models.doc_types.level_requirements.
    if dt.key == "tesis" and academic_level:
        from models.doc_types import level_requirements
        lvl = level_requirements(academic_level)
        fmt_dict = fmt.as_dict()
        fmt_dict["min_words"] = max(int(fmt_dict.get("min_words") or 0), lvl["min_words"])
        fmt = FormatSpec.from_dict(fmt_dict)
        evaluation_criteria = evaluation_criteria + [
            c for c in lvl["extra_criteria"] if c not in evaluation_criteria
        ]

    session.builder_log.append({"phase": "research_brief", "requested": provider, "used": used})
    return DocumentBrief(
        doc_type_key=dt.key,
        title=data.get("title", "Documento sin título"),
        language=data.get("language", "es"),
        personas=data.get("personas") or dt.personas,
        sections=data.get("sections") or dt.sections,
        format_spec=fmt.as_dict(),
        instructions=data.get("instructions", ""),
        national_guidelines=data.get("national_guidelines", []),
        international_guidelines=data.get("international_guidelines", []),
        key_requirements=data.get("key_requirements", []),
        quality_markers=data.get("quality_markers", []),
        source_notes=data.get("source_notes", ""),
        needs_budget_excel=dt.needs_budget_excel,
        evaluation_criteria=evaluation_criteria,
        rigor_notes=dt.rigor_notes,
        raw=raw,
        academic_level=academic_level or "pregrado",
    )
