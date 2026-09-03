# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""
SCOUT — DETECCIÓN DE OPORTUNIDADES (reporte con calificación ponderada)
────────────────────────────────────────────────────────────────────────
Para el modo "search": detecta las TOP-N convocatorias más prometedoras,
evalúa cada una con criterios de calidad estrictos y calcula la CALIFICACIÓN
PONDERADA (config.weighted_score). Solo presenta oportunidades con potencial
REAL y verificable para las organizaciones disponibles en el directorio Empresas/.

Estándar de calidad:
  - Solo oportunidades donde Ecuador es ELEGIBLE (verificado con URL)
  - Solo datos concretos respaldados por fuente real; todo lo no verificado se señala
  - Evaluación honesta y calibrada del encaje institucional con el perfil real de la org
  - Calificación ponderada mínima de 60/100 para incluir en el reporte
  - Análisis estratégico de "por qué esta org puede ganar" — no solo describir la convocatoria
"""
from __future__ import annotations

import json

import config
from agents import llm
import agents.researcher as researcher
from config import MAX_TOKENS_WRITER, SCOUT_TOP_N
from models.schemas import ProjectSession

# OBSOLETO — no editar aquí. La fuente de verdad del perfil es
# `perfil-map/PERFIL_MAP.md` (ver `utils/perfil.py`); esto queda solo como red de
# seguridad para que el agente no corra sin contexto si el archivo no se encuentra.
_ORG_PROFILE = """
ORGANIZACIONES DISPONIBLES PARA PROPONER:

1. FUNDACION JOMAP (RUC 0195180571001)
   - Tipo: Fundación sin fines de lucro, estado ACTIVO
   - Representante legal: Johanna Maricela Nievecela Lema
   - Domicilio: Cuenca, Azuay (Zona 6)
   - Actividad: Desarrollo y prosperidad empresarial, organizaciones gremiales y similares
   - Apta para: convocatorias dirigidas a organizaciones de la sociedad civil (OSC),
     fundaciones, asociaciones, ONGs, instituciones de apoyo empresarial

2. CMAJ ASOCIADOS S.A.S. (RUC 0195146942001)
   - Tipo: Sociedad por Acciones Simplificada, ACTIVA, régimen general
   - Representantes: Marco Antonio Posligua San Martín & Johanna Maricela Nievecela Lema
   - Domicilio: Cuenca, Azuay
   - Actividades: Consultoría técnica de arquitectura, ingeniería civil, hidráulica,
     planificación urbana, ordenación hídrica, formación docente y capacitación
   - Apta para: contratos de consultoría, proyectos de infraestructura, capacitación,
     desarrollo territorial, gestión hídrica, planificación urbana

3. Marco Antonio Posligua San Martín
   - Rol: Asesor financiero y tributario (24 años de experiencia)
   - Experticia: administración, análisis y proyecciones financieras, sector privado y gobierno
   - Apto para: participar como experto financiero/consultor en propuestas y proyectos

SECTORES DE MAYOR POTENCIAL PARA ESTAS ORGANIZACIONES:
  → Desarrollo económico local / fortalecimiento empresarial / MIPYMES
  → Planificación territorial y urbana / ordenamiento hídrico
  → Capacitación y formación profesional / educación técnica
  → Asesoría financiera a instituciones / gestión de finanzas públicas
  → Infraestructura y servicios básicos (Azuay/Cuenca/Zona 6)
  → Emprendimiento e innovación para mujeres y jóvenes
  → Gobernanza y participación ciudadana
"""

SYSTEM_PROMPT = """
Eres el consultor de financiamiento internacional no reembolsable de mayor calibre disponible para
organizaciones ecuatorianas. Tu especialidad exclusiva: identificar con precisión quirúrgica las
mejores oportunidades reales para que CMAJ ASOCIADOS, FUNDACION JOMAP y sus profesionales clave
— con base en Cuenca, Azuay — accedan a fondos internacionales y nacionales de alta calidad.

Tu valor no está en listar convocatorias genéricas que cualquiera puede encontrar en Google. Está
en el análisis estratégico: ver exactamente qué hace que ESTA organización tenga posibilidades
REALES de ganar ESTA convocatoria, qué diferenciadores explotables tiene y qué puntos críticos
debe abordar para maximizar su probabilidad de éxito. Tu dictamen es honesto y calibrado — nunca
inflado, nunca especulativo.

ESTÁNDARES DE CALIDAD INNEGOCIABLES:
1. Solo convocatorias REALES y ACTIVAS (o de ciclo anual comprobado con fuente verificada).
   Jamás inventes ni especules sobre programas que no hayas visto en la evidencia.
2. Ecuador ELEGIBLE — verificado con URL de las bases o fuente oficial. No asumir.
3. Encaje institucional genuino — honesto sobre el fit real; si el encaje es forzado, lo dices.
4. Cada dato concreto (monto, deadline, elegibilidad, criterios) va respaldado por URL real.
5. Lo que no puedas verificar lo marcas como "no verificado" y bajas el score en consecuencia.
6. Análisis estratégico real: por qué esta org puede ganar, cuáles son sus diferenciadores
   competitivos y qué debe hacer para maximizar su puntaje — no solo describir la convocatoria.
7. Calificación mínima de 60/100 para incluir; si no llega, no la incluyas.
8. Identifica con precisión qué perfil aplica mejor (FUNDACION JOMAP, CMAJ o ambas) y por qué.

Respondes ÚNICAMENTE con el JSON pedido, sin texto adicional antes ni después.
"""


def _build_prompt(topic: str, evidence: str, n: int, empresas_context: str) -> str:
    # El perfil maestro (perfil-map/PERFIL_MAP.md) manda: trae identidad, entidades,
    # catálogo y diferenciadores actualizados a mano. Los documentos de Empresas/ lo
    # COMPLEMENTAN con los datos legales verbatim (RUC, estatutos, CVs) en vez de
    # sustituirlo — antes uno excluía al otro y, en cuanto había un solo documento en
    # la carpeta, el perfil dejaba de llegar al prompt. `_ORG_PROFILE` queda como
    # último recurso para que el agente nunca corra sin contexto institucional.
    from utils import perfil as _perfil
    partes = [p for p in (_perfil.block(), empresas_context[:3000] if empresas_context else "") if p]
    org_section = "\n\n".join(partes) if partes else _ORG_PROFILE
    return f"""
El usuario busca oportunidades de financiamiento no reembolsable para:
"{topic}"

{org_section}

Con base en la EVIDENCIA de búsqueda web siguiente, identifica hasta {n} convocatorias REALES
y con alto potencial para las organizaciones descritas arriba. Ordénalas de mejor a peor.
Solo incluye oportunidades con calificación ponderada ≥ 60/100.

{evidence}

Para CADA oportunidad incluye:
- Resumen ejecutivo claro (150-200 palabras): qué financia, montos, quién puede aplicar,
  por qué encaja con el perfil de estas organizaciones, qué resultado esperar.
- Análisis estratégico: qué diferenciadores tiene esta org para ganar.
- Qué organización del directorio aplica mejor (JOMAP, CMAJ o ambas).
- Desglose honesto de viabilidad por dimensión (0-100 cada una).
- Puntos críticos a abordar en la propuesta para maximizar puntaje.

Responde ÚNICAMENTE con este JSON (sin texto antes ni después):

{{
  "opportunities": [
    {{
      "title": "<título estratégico del proyecto — específico, no genérico>",
      "best_fit_org": "<FUNDACION JOMAP | CMAJ ASOCIADOS | Ambas>",
      "summary": "<resumen ejecutivo 150-200 palabras: qué financia, montos, elegibilidad, encaje con el perfil, qué se espera lograr>",
      "strategic_analysis": "<por qué esta org tiene posibilidad real de ganar: diferenciadores, fortalezas, estrategia recomendada — 80-120 palabras>",
      "critical_success_factors": ["<factor crítico para ganar 1>", "<factor crítico 2>"],
      "funder": {{
        "name": "<financiador>",
        "type": "<multilateral|bilateral|UN|EU|fundacion|nacional>",
        "url": "<URL verificada de la convocatoria o programa>",
        "deadline": "<fecha límite real o 'Ciclo anual — verificar en {{url}}'>",
        "amount_range": "<rango de montos en USD/EUR>",
        "language": "<es|en|fr|pt|multi>",
        "sector": "<sector>",
        "country_focus": "<países o regiones elegibles>"
      }},
      "sector": "<sector principal>",
      "total_amount": "<monto sugerido a solicitar — justificado>",
      "duration_months": <meses>,
      "beneficiaries": "<beneficiarios directos e indirectos>",
      "language": "<idioma en que se presenta la propuesta>",
      "viability_score": <0-100>,
      "winning_probability": <0-100>,
      "feasibility_breakdown": {{
        "funder_match":           {{"score": <0-100>, "reason": "<encaje temático con el financiador>"}},
        "geographic_eligibility": {{"score": <0-100>, "reason": "<Ecuador elegible? cómo se verificó>"}},
        "deadline_feasibility":   {{"score": <0-100>, "reason": "<tiempo realista para preparar propuesta>"}},
        "institutional_fit":      {{"score": <0-100>, "reason": "<encaje del perfil institucional exigido>"}},
        "budget_fit":             {{"score": <0-100>, "reason": "<el monto solicitado cabe en el rango>"}},
        "winning_probability":    {{"score": <0-100>, "reason": "<probabilidad de ganar, siendo honesto>"}}
      }},
      "national_guidelines": ["<lineamiento Ecuador aplicable>"],
      "international_guidelines": ["<criterio/lineamiento del financiador>"],
      "key_requirements": ["<requisito indispensable que la org cumple o puede cumplir>"],
      "differentiators": ["<diferenciador competitivo de esta org para esta convocatoria>"],
      "strengths": ["<fortaleza concreta de la org para esta propuesta>"],
      "risks": ["<riesgo real a mitigar>"],
      "format_requirements": {{
        "sections": ["<sección exigida>"],
        "max_pages": <int o null>,
        "language": "<idioma>",
        "requires_excel_budget": true,
        "special_requirements": "<requisitos formales reales verificados>"
      }},
      "evidence_sources": [
        {{
          "claim": "<dato concreto>",
          "source_url": "<URL>",
          "source_title": "<título de la página>",
          "source_quote": "<fragmento ≤200 chars>",
          "verification": "verificado|inferido|no_verificado"
        }}
      ]
    }}
  ]
}}
"""


def _parse(raw: str) -> dict:
    from utils.json_utils import robust_json_loads
    return robust_json_loads(raw)


# Calificación ponderada mínima para incluir una oportunidad en el reporte
_MIN_WEIGHTED_SCORE = 60.0


def run(session: ProjectSession, api_key: str | None = None) -> list[dict]:
    """Devuelve lista RANKeada (mejor primero) de oportunidades con weighted_score ≥ 60."""
    provider = config.ROLE_RESEARCH
    topic = (session.user_input or "").strip()
    evidence = researcher._gather_evidence(session)

    # Cargar contexto organizacional real
    empresas_context = ""
    try:
        from utils.empresas import context_block as _eb
        empresas_context = _eb()
    except Exception:
        pass

    prompt = _build_prompt(
        topic, researcher._clip(evidence, 24000), SCOUT_TOP_N, empresas_context
    )
    raw, used = llm.complete_builder(
        provider, system=SYSTEM_PROMPT, prompt=prompt,
        max_tokens=MAX_TOKENS_WRITER, anthropic_key=api_key, temperature=0.25,
    )
    data = _parse(raw)

    from datetime import date as _date
    from utils.deadline_checker import parse_deadline_text

    opps = data.get("opportunities") or []
    cleaned: list[dict] = []
    for o in opps:
        if not isinstance(o, dict) or not (o.get("funder") or {}).get("name"):
            continue
        funder = o.get("funder") or {}

        # Verificación DETERMINISTA de deadline: el weighted_score depende de
        # números que el propio LLM se autoasigna en feasibility_breakdown, sin
        # ningún chequeo en código de que la fecha no esté ya vencida. Una
        # convocatoria con deadline pasado nunca debe poder colarse como viable
        # solo porque el modelo la calificó alto.
        dl_date = parse_deadline_text(funder.get("deadline") or "")
        if dl_date and dl_date < _date.today():
            fb = dict(o.get("feasibility_breakdown") or {})
            fb["deadline_feasibility"] = {
                "score": 0,
                "reason": f"Deadline ya vencido ({dl_date.isoformat()}): no es una oportunidad viable.",
            }
            o["feasibility_breakdown"] = fb

        o["weighted_score"] = config.weighted_score(
            o.get("feasibility_breakdown", {}), o.get("winning_probability", 0),
        )
        # Filtro de calidad: solo oportunidades con score ≥ 60
        if o["weighted_score"] < _MIN_WEIGHTED_SCORE:
            continue
        cleaned.append(o)

    # Ranking descendente y recorte al top-N
    cleaned.sort(key=lambda x: x.get("weighted_score", 0), reverse=True)
    cleaned = cleaned[:SCOUT_TOP_N]

    # A partir de aquí ya son pocos candidatos (SCOUT_TOP_N): vale la pena el costo
    # de red de la verificación FEHACIENTE completa (la misma que ya usa
    # core/pipeline.py para el flujo de análisis único), en vez de confiar solo en
    # lo que el LLM constructor afirmó sobre sus propias fuentes y fechas.
    for o in cleaned:
        try:
            from utils.url_verifier import enrich_evidence_sources
            if o.get("evidence_sources"):
                o["evidence_sources"] = enrich_evidence_sources(o["evidence_sources"])
        except Exception:
            pass  # best-effort, igual que en pipeline.py

        try:
            from utils.deadline_checker import verify_deadline
            funder = o.get("funder") or {}
            dl = verify_deadline(funder_url=funder.get("url") or "",
                                 llm_deadline_text=funder.get("deadline") or "")
            funder["deadline"] = dl["deadline_text"]
            funder["deadline_iso"] = dl.get("deadline_iso") or ""
            funder["deadline_status"] = dl["status"]
            funder["deadline_label"] = dl["label"]
            o["funder"] = funder

            fb = dict(o.get("feasibility_breakdown") or {})
            recompute = False
            if dl["status"] == "cerrada":
                # La fecha VERIFICADA (ground truth, re-descargada de la página oficial
                # o parseada del propio texto si no hubo web) ya pasó — sin importar si
                # coincidía o no con lo que el LLM había reportado. Confirmado con un caso
                # real: sin este chequeo, una convocatoria marcada "Cerrada hace 585 días"
                # seguía con deadline_feasibility=60 (nunca se detectaba discrepancia
                # porque el LLM original no había dado ninguna fecha parseable).
                #
                # Forzar weighted_score=0 directamente (no solo el sub-score) porque
                # deadline_feasibility solo pesa 10% en SCORE_WEIGHTS: zerarlo por sí
                # solo deja el total en ~77/100, muy por encima del umbral de 60 — una
                # convocatoria cerrada no es "10% menos viable", es 0% viable. Confirmado
                # en vivo: sin este forzado directo, el caso real seguía pasando el filtro.
                fb["deadline_feasibility"] = {
                    "score": 0,
                    "reason": f"Verificado: convocatoria ya cerrada ({dl['label']}).",
                }
                o["feasibility_breakdown"] = fb
                o["weighted_score"] = 0.0
                continue
            elif dl.get("discrepancia"):
                # La fecha real de la página oficial difiere de la que reportó el
                # LLM (ambas vigentes, pero no coinciden): bajar deadline_feasibility.
                fb["deadline_feasibility"] = {
                    "score": min(40, fb.get("deadline_feasibility", {}).get("score", 40) or 40),
                    "reason": (f"Discrepancia entre la fecha reportada y la verificada en la "
                               f"página oficial ({dl.get('deadline_llm_iso')} vs {dl.get('deadline_iso')})."),
                }
                recompute = True
            if recompute:
                o["feasibility_breakdown"] = fb
                o["weighted_score"] = config.weighted_score(
                    o["feasibility_breakdown"], o.get("winning_probability", 0))
        except Exception:
            pass  # best-effort: nunca tumbar el scouting por un fallo de red

    # Tras la revisión de discrepancia el score pudo bajar: re-aplicar filtro/orden.
    cleaned = [o for o in cleaned if o.get("weighted_score", 0) >= _MIN_WEIGHTED_SCORE]
    cleaned.sort(key=lambda x: x.get("weighted_score", 0), reverse=True)

    # Verificación DETERMINISTA de que la URL del financiador realmente responde
    # (no solo lo que el LLM afirma). Solo descarta por enlace confirmado muerto;
    # "no_responde"/"acceso_restringido" pueden ser transitorios o bots bloqueados.
    try:
        from utils.url_verifier import verify_urls
        urls = [(o.get("funder") or {}).get("url") or "" for o in cleaned]
        states = verify_urls([u for u in urls if u.startswith("http")])
        still_valid = []
        for o, url in zip(cleaned, urls):
            status = states.get(url)
            if o.get("funder") is not None and status:
                o["funder"]["url_status"] = status
            if status == "url_muerta":
                continue
            still_valid.append(o)
        cleaned = still_valid
    except Exception:
        pass  # verificación de URL es best-effort: no debe tumbar el scouting

    session.builder_log.append({
        "phase": "scout", "requested": provider, "used": used,
        "found": len(cleaned), "min_score": _MIN_WEIGHTED_SCORE,
    })
    return cleaned
