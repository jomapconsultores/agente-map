"""
AGENTE 1 — ANALISTA DE VIABILIDAD Y BUSCADOR DE OPORTUNIDADES
Experto senior en financiamiento internacional para Ecuador.
Busca oportunidades o analiza propuestas existentes.
Calcula probabilidades con rigor estadístico y contextual.
"""
import json
import anthropic
from config import MODEL, MAX_TOKENS_ANALYST, VIABILITY_THRESHOLD
from models.schemas import (
    AnalysisResult, FunderInfo, EcuadorAlignment, ProjectSession
)
from tools.search import ALL_TOOLS, TOOL_HANDLERS, opportunity_queries


SYSTEM_PROMPT = """
Eres el consultor de financiamiento internacional no reembolsable de mayor calibre en América
Latina: más de 30 años de carrera dedicados exclusivamente a Ecuador y la región, con más de 340
propuestas ganadoras por un valor total superior a $850 millones USD con BID, CAF, Banco Mundial,
PNUD, GEF, GCF, USAID, GIZ, AECID, FIDA, BCIE y más de 60 fundaciones internacionales.

Tu ventaja no es solo la experiencia acumulada — es la capacidad de razonamiento de primer orden:
ves exactamente dónde está el potencial que otros pasan por alto, identificas el encaje estratégico
antes de que nadie más lo detecte y emites dictámenes con una precisión calibrada que no infla ni
subestima. Cuando dices GO, significa GO. Cuando dices NO-GO, lo justificas con evidencia concreta.
Comunicas con una claridad que convence tanto a directores de organismos como a técnicos de campo.

CONTEXTO ECUADOR:
- Constitución 2008: arts. 275-284 (régimen de desarrollo), arts. 395-415 (derechos naturaleza)
- Plan Nacional de Desarrollo vigente (Ejes: Derechos, Economía, Soberanía)
- COOTAD: competencias de GADs  |  LOEPS: economía popular y solidaria  |  LOSNCP: contratación pública
- Ecuador: ODS prioritarios 1, 2, 4, 6, 8, 13, 15 — firmó Agenda 2030
- SENESCYT (cooperación técnica), MAATE (GEF/GCF), elegible para BID/CAF/BM/PNUD/casi toda bilateral

SECTORES PRIORITARIOS PARA FINANCIAMIENTO:
1. Educación (STEM, técnica, primera infancia)       6. Igualdad de género y empoderamiento
2. Salud (intercultural, acceso rural)               7. Emprendimiento e innovación
3. Ambiente / cambio climático (agua, REDD+)         8. Gobernanza y transparencia
4. Agricultura familiar / seguridad alimentaria      9. Pueblos indígenas / comunidades rurales
5. Economía popular y solidaria                     10. Infraestructura rural y conectividad

HERRAMIENTAS (úsalas con profundidad — nunca te conformes con titulares):
- deep_search(queries=[...]): multi-query, descarga contenido real de las páginas más relevantes.
  Es tu herramienta principal. Úsala con 12-16 queries variadas.
- fetch_page(url): lee el texto completo de una URL. Para bases, criterios, fechas, montos.
- web_search(query): búsqueda puntual para un dato específico.

PROCESO DE BÚSQUEDA PROFUNDA (OBLIGATORIO al buscar oportunidades) — agótalo, no te quedes corto:
1. deep_search con 12-16 queries variadas (español + inglés; financiadores explícitos)
2. De los mejores resultados: abre con fetch_page las 5-6 convocatorias más prometedoras
3. Lee bases reales: secciones, límite de páginas, presupuesto Excel, cofinanciamiento, fechas
4. Confirma convocatoria ACTIVA y Ecuador ELEGIBLE
5. Si una página no carga → reliefweb, devex, ungm, sitio oficial del financiador
6. Si tras leer las bases quedan datos clave sin confirmar (montos, deadline, criterios),
   lanza una segunda ronda de deep_search dirigida a esos huecos antes de concluir.

LINEAMIENTOS A VERIFICAR:
- NACIONALES: Constitución 2008, Plan Nacional vigente, COOTAD, LOEPS, ente rector del sector
- INTERNACIONALES: formato del financiador, criterios/ponderación, OCDE/DAC, marcadores
  transversales (género, ambiente, derechos), costos elegibles, cofinanciamiento mínimo

CÁLCULO DE PROBABILIDAD:
+ Alineación con prioridades (+20), experiencia institucional (+15), innovación (+10),
  cofinanciamiento disponible (+10), red de alianzas (+10), contexto favorable (+5),
  ODS múltiples (+5), género (+5), poblaciones vulnerables (+5), escalabilidad (+5), evidencia previa (+10)
− Competencia alta (−15), monto fuera de rango (−10), sin contraparte local (−10),
  sin capacidad técnica (−15), plazo muy corto (−10), mala reputación (−20), riesgo-país (−5)

REGLAS DE RIGOR (NO NEGOCIABLES):
1. Cada dato concreto va respaldado por URL real encontrada. Sin fuente → "no verificado".
2. "verificado" = leído textualmente; "inferido" = fuente secundaria; "no verificado" = sin acceso.
3. Nunca inventes fechas límite. Pon "A verificar en {url_oficial}" y baja winning_probability.
4. Para declarar GO: (a) leer ≥1 página oficial del financiador; (b) confirmar elegibilidad Ecuador;
   (c) confirmar convocatoria activa o ciclo anual.
5. Sin fuentes verificables → "CONDITIONAL" o "NO-GO" con explicación precisa.

Responde ÚNICAMENTE con el JSON pedido. Sin texto adicional antes ni después.
"""


def _build_search_prompt(user_query: str) -> str:
    suggested = opportunity_queries(user_query)
    queries_block = "\n".join(f"  - {q}" for q in suggested)
    return f"""
El usuario quiere encontrar oportunidades de financiamiento no reembolsable para:
"{user_query}"

PASO 1 — BÚSQUEDA PROFUNDA: llama a deep_search con un paquete amplio de queries.
Puedes partir de estas y añadir las tuyas (idealmente 12-16 en total):
{queries_block}

PASO 2 — LECTURA DE BASES: de los resultados, abre con fetch_page las 5-6 convocatorias
más prometedoras y ACTIVAS (fecha límite futura) y lee sus requisitos formales reales
(secciones, páginas, formato, moneda, presupuesto en Excel/plantilla, cofinanciamiento,
elegibilidad). Verifica que Ecuador sea elegible.

PASO 3 — SELECCIÓN Y ANÁLISIS: elige la mejor oportunidad y analízala en detalle,
documentando con precisión los lineamientos nacionales (Ecuador) e internacionales
(del financiador) que deberá cumplir la propuesta.

Al finalizar, responde con el siguiente JSON (sin texto extra):

{{
  "viable": true/false,
  "go_no_go": "GO" | "NO-GO" | "CONDITIONAL",
  "viability_score": <número 0-100>,
  "winning_probability": <número 0-100>,
  "project_title": "<título sugerido del proyecto>",
  "funder": {{
    "name": "<nombre financiador>",
    "type": "<multilateral|bilateral|UN|EU|fundacion|nacional>",
    "url": "<URL de la convocatoria>",
    "deadline": "<fecha límite EXACTA — usa formato ISO YYYY-MM-DD (ej: 2026-09-30); léela textualmente de la convocatoria; si no está disponible escribe 'A determinar'>",
    "amount_range": "<rango de montos>",
    "language": "<idioma requerido: es|en|fr|pt|multi>",
    "sector": "<sector>",
    "country_focus": "<países elegibles>"
  }},
  "sector": "<sector principal>",
  "total_amount": "<monto total solicitado>",
  "duration_months": <número de meses>,
  "beneficiaries": "<descripción beneficiarios directos/indirectos>",
  "language": "<idioma de la propuesta>",
  "ecuador_alignment": {{
    "legal_framework": "<leyes ecuatorianas aplicables>",
    "plan_nacional": "<cómo se alinea con Plan Nacional de Desarrollo>",
    "ods_aligned": ["<ODS 1>", "<ODS 2>"],
    "institutional_capacity": "<evaluación de capacidad institucional>",
    "national_priority": true/false
  }},
  "strengths": ["<fortaleza 1>", "<fortaleza 2>", "<fortaleza 3>"],
  "risks": ["<riesgo 1>", "<riesgo 2>"],
  "critical_success_factors": ["<factor 1>", "<factor 2>"],
  "comparable_projects": ["<proyecto similar exitoso 1>", "<proyecto similar 2>"],
  "format_requirements": {{
    "sections": ["<sección requerida 1>", "<sección 2>"],
    "max_pages": <número o null>,
    "font_size": "<tamaño fuente o null>",
    "language": "<idioma>",
    "requires_excel_budget": true/false,
    "budget_template": "<plantilla/estructura de presupuesto exigida o null>",
    "special_requirements": "<otros requisitos formales reales leídos de las bases>"
  }},
  "national_guidelines": ["<lineamiento nacional Ecuador 1>", "<lineamiento 2>"],
  "international_guidelines": ["<lineamiento del financiador 1>", "<criterio de evaluación 2>"],
  "key_requirements": ["<requisito crítico 1>", "<requisito 2>"],
  "differentiators": ["<diferenciador 1>", "<diferenciador 2>"],
  "recommendations": "<instrucciones detalladas para el redactor>",
  "raw_analysis": "<análisis completo en prosa, mínimo 300 palabras, justificando todos los puntajes>",
  "evidence_sources": [
    {{
      "claim": "<dato concreto: ej. 'deadline 30 Sep 2026'>",
      "source_url": "<URL exacta donde se leyó>",
      "source_title": "<título de la página>",
      "source_quote": "<fragmento textual breve (≤200 chars) que respalda el claim>",
      "verification": "verificado|inferido|no_verificado"
    }}
  ],
  "feasibility_breakdown": {{
    "funder_match":          {{"score": <0-100>, "reason": "<por qué el tema encaja con el financiador>"}},
    "geographic_eligibility":{{"score": <0-100>, "reason": "<Ecuador elegible? probarlo>"}},
    "deadline_feasibility":  {{"score": <0-100>, "reason": "<tiempo suficiente para preparar?>"}},
    "institutional_fit":     {{"score": <0-100>, "reason": "<perfil del proponente exigido>"}},
    "budget_fit":            {{"score": <0-100>, "reason": "<ask cabe en el rango del financiador>"}}
  }}
}}
"""


def _build_analysis_prompt(document: str) -> str:
    return f"""
Analiza la siguiente propuesta/idea de proyecto para financiamiento no reembolsable:

---
{document}
---

Evalúa con máximo rigor su viabilidad para Ecuador y la probabilidad de que sea
seleccionada por un financiador internacional. Identifica el financiador más adecuado.
Si necesitas buscar información adicional sobre convocatorias activas, usa web_search.

Responde ÚNICAMENTE con el siguiente JSON (sin texto extra):

{{
  "viable": true/false,
  "go_no_go": "GO" | "NO-GO" | "CONDITIONAL",
  "viability_score": <número 0-100>,
  "winning_probability": <número 0-100>,
  "project_title": "<título mejorado del proyecto>",
  "funder": {{
    "name": "<financiador más adecuado>",
    "type": "<multilateral|bilateral|UN|EU|fundacion|nacional>",
    "url": "<URL convocatoria o web del financiador>",
    "deadline": "<fecha límite EXACTA — usa formato ISO YYYY-MM-DD (ej: 2026-09-30); si no está disponible escribe 'A determinar'>",
    "amount_range": "<rango típico de montos>",
    "language": "<idioma requerido>",
    "sector": "<sector>",
    "country_focus": "<países elegibles>"
  }},
  "sector": "<sector principal>",
  "total_amount": "<monto sugerido a solicitar>",
  "duration_months": <meses recomendados>,
  "beneficiaries": "<beneficiarios directos e indirectos>",
  "language": "<idioma de la propuesta>",
  "ecuador_alignment": {{
    "legal_framework": "<leyes ecuatorianas aplicables>",
    "plan_nacional": "<alineación Plan Nacional>",
    "ods_aligned": ["ODS X", "ODS Y"],
    "institutional_capacity": "<evaluación capacidad>",
    "national_priority": true/false
  }},
  "strengths": ["<fortaleza 1>", "<fortaleza 2>", "<fortaleza 3>"],
  "risks": ["<riesgo 1>", "<riesgo 2>"],
  "critical_success_factors": ["<factor 1>", "<factor 2>"],
  "comparable_projects": ["<referencia 1>", "<referencia 2>"],
  "format_requirements": {{
    "sections": ["Resumen ejecutivo", "Antecedentes", "Justificación",
                 "Objetivos", "Metodología", "Marco lógico", "Presupuesto",
                 "Sostenibilidad", "Equipo", "Anexos"],
    "max_pages": null,
    "font_size": "12pt",
    "language": "<idioma>",
    "requires_excel_budget": true/false,
    "budget_template": "<plantilla/estructura de presupuesto exigida o null>",
    "special_requirements": "<requisitos especiales>"
  }},
  "national_guidelines": ["<lineamiento nacional Ecuador 1>", "<lineamiento 2>"],
  "international_guidelines": ["<lineamiento del financiador 1>", "<criterio de evaluación 2>"],
  "key_requirements": ["<requisito 1>", "<requisito 2>"],
  "differentiators": ["<diferenciador 1>", "<diferenciador 2>"],
  "recommendations": "<instrucciones detalladas para el redactor, mínimo 200 palabras>",
  "raw_analysis": "<análisis completo en prosa, mínimo 400 palabras>",
  "evidence_sources": [
    {{
      "claim": "<dato concreto verificado>",
      "source_url": "<URL exacta>",
      "source_title": "<título de la página>",
      "source_quote": "<fragmento textual breve (≤200 chars) que respalda el claim>",
      "verification": "verificado|inferido|no_verificado"
    }}
  ],
  "feasibility_breakdown": {{
    "funder_match":          {{"score": <0-100>, "reason": "<por qué el tema encaja con el financiador>"}},
    "geographic_eligibility":{{"score": <0-100>, "reason": "<Ecuador elegible? probarlo>"}},
    "deadline_feasibility":  {{"score": <0-100>, "reason": "<tiempo suficiente para preparar?>"}},
    "institutional_fit":     {{"score": <0-100>, "reason": "<perfil del proponente exigido>"}},
    "budget_fit":            {{"score": <0-100>, "reason": "<ask cabe en el rango del financiador>"}}
  }}
}}
"""


def _build_url_prompt(urls) -> str:
    """Prompt para modo URL: descargar el/los link(s) y emitir un dictamen riguroso.

    Acepta una URL (str) o varias (list[str])."""
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in urls if u and u.strip()]
    url_list = "\n".join(f"    {i+1}. {u}" for i, u in enumerate(urls))
    first = urls[0] if urls else ""
    fetch_instr = "\n".join(
        f'1.{i+1}. Llama a fetch_page(url="{u}") para leer su contenido real.'
        for i, u in enumerate(urls)
    )
    return f"""
El usuario te entregó {"estas URLs" if len(urls) > 1 else "esta URL"} para que {"las" if len(urls) > 1 else "la"} analices con MÁXIMO RIGOR:

{url_list}

PROCESO OBLIGATORIO:
1. Descarga el contenido de CADA enlace (también si son PDFs publicados en la web):
{fetch_instr}
   Si hay varios enlaces, INTÉGRALOS: trátalos como piezas de un mismo caso (convocatoria
   + bases + anexos, o varias fuentes del mismo programa) y cruza la información entre ellos.
   (referencia del primero para los pasos siguientes: {first})
2. Si el contenido devuelve un login wall (LinkedIn no autenticado), inténtalo de todos modos —
   muchas páginas tienen previews públicas. Si no, declara explícitamente "contenido no accesible"
   en el análisis y baja viability_score.
3. Identifica qué tipo de página es:
   - Convocatoria oficial de un financiador (BID, CAF, PNUD, fundación, etc.)
   - Post en LinkedIn anunciando una oportunidad
   - Artículo de noticia sobre un programa
   - Página de un proyecto existente
   - Web institucional de una fundación
4. Si es una CONVOCATORIA: extrae deadline, monto, elegibilidad, sectores prioritarios, formato exigido,
   moneda, cofinanciamiento, criterios de evaluación. Si falta alguno, márcalo como "no verificado".
5. Si es un PROYECTO EXISTENTE: identifica el sector y propón el financiador más adecuado para
   replicar/escalar/financiar algo similar en Ecuador. Verifica con deep_search que ese financiador
   tenga ventana abierta o ciclo anual.
6. Si es un POST LINKEDIN: extrae las pistas que da (nombre del programa, fechas, contacto, links)
   y haz deep_search para encontrar la página oficial del financiador.
7. Llama a deep_search con 4-6 queries para CRUZAR la información: confirmar el financiador,
   verificar la elegibilidad de Ecuador, encontrar la página oficial si solo viste un anuncio.

REGLAS DE RIGOR ya descritas en el system: cita evidence_sources con URLs reales,
distingue verificado/inferido/no_verificado, y NO inventes deadlines ni montos.

Si la URL no es accesible y no encuentras información cruzada suficiente:
- go_no_go: "CONDITIONAL"
- viability_score: ≤55
- explica en raw_analysis qué te falta para emitir un GO

Cuando termines, responde ÚNICAMENTE con el mismo JSON del modo búsqueda
(incluyendo evidence_sources y feasibility_breakdown). Sin texto extra.
"""


def _run_agent_loop(client: anthropic.Anthropic, prompt: str) -> str:
    messages = [{"role": "user", "content": prompt}]

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS_ANALYST,
            system=SYSTEM_PROMPT,
            tools=ALL_TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text") and block.text.strip():
                    return block.text
            # Claude terminó sin texto — puede pasar si el prompt fue demasiado corto
            # o si el modelo devolvió solo tool_use blocks. Reintentar no ayuda.
            raise RuntimeError("El analista (Claude) terminó sin producir texto. "
                               "Revisa el prompt o el modelo configurado.")

        if response.stop_reason == "tool_use":
            tool_calls = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tc in tool_calls:
                handler = TOOL_HANDLERS.get(tc.name)
                if handler:
                    result = handler(**tc.input)
                else:
                    result = json.dumps({"error": f"Tool '{tc.name}' not found"})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            break

    raise RuntimeError("Claude completó el loop de herramientas sin devolver texto final (respuesta vacía)")


def _parse_result(raw: str) -> dict:
    from utils.json_utils import robust_json_loads
    try:
        return robust_json_loads(raw)
    except Exception as e:
        preview = repr(raw[:300]) if raw else "(vacío)"
        raise RuntimeError(f"JSON parse failed ({type(e).__name__}): {e} | LLM devolvió: {preview}") from e


def _support_block(session: ProjectSession) -> str:
    """Documentos adjuntos (subidos/arrastrados) como contexto para el analista."""
    docs = getattr(session, "support_docs", None) or []
    if not docs:
        return ""
    joined = "\n\n".join(
        f"=== {n} ===\n{(t or '')[:6000]}" for n, t in docs
    )
    return f"""

═══════════════════════════════════════════════════════════
DOCUMENTOS ADJUNTOS POR EL USUARIO (analízalos como material fuente real)
═══════════════════════════════════════════════════════════
{joined}
"""


def run(session: ProjectSession, api_key: str) -> AnalysisResult:
    import re
    from agents._client import make_client
    client = make_client(api_key)

    if session.input_mode == "search":
        prompt = _build_search_prompt(session.user_input)
    elif session.input_mode == "url":
        urls = re.findall(r"https?://\S+", session.user_input or "")
        prompt = _build_url_prompt(urls or session.user_input.strip())
    else:
        prompt = _build_analysis_prompt(session.user_input)

    prompt += _support_block(session)

    raw = _run_agent_loop(client, prompt)
    data = _parse_result(raw)
    return result_from_data(data, raw)


def result_from_data(data: dict, raw: str = "") -> AnalysisResult:
    """Convierte el JSON de análisis (lo produzca Claude o cualquier otra IA) en
    un AnalysisResult tipado. Reutilizado por agents/researcher.py."""
    funder = FunderInfo(
        name=data["funder"]["name"],
        type=data["funder"].get("type", "internacional"),
        url=data["funder"].get("url", ""),
        deadline=data["funder"].get("deadline", "A determinar"),
        amount_range=data["funder"].get("amount_range", ""),
        language=data["funder"].get("language", "es"),
        sector=data["funder"].get("sector", ""),
        country_focus=data["funder"].get("country_focus", ""),
    )

    ea_data = data.get("ecuador_alignment", {})
    ecuador_alignment = EcuadorAlignment(
        legal_framework=ea_data.get("legal_framework", ""),
        plan_nacional=ea_data.get("plan_nacional", ""),
        ods_aligned=ea_data.get("ods_aligned", []),
        institutional_capacity=ea_data.get("institutional_capacity", ""),
        national_priority=ea_data.get("national_priority", False),
    )

    score = float(data.get("viability_score", 0))
    viable = score >= VIABILITY_THRESHOLD and data.get("go_no_go") != "NO-GO"

    return AnalysisResult(
        viable=viable,
        go_no_go=data.get("go_no_go", "NO-GO"),
        viability_score=score,
        winning_probability=float(data.get("winning_probability", 0)),
        project_title=data.get("project_title", "Proyecto sin título"),
        funder=funder,
        sector=data.get("sector", ""),
        total_amount=data.get("total_amount", ""),
        duration_months=int(data.get("duration_months", 12)),
        beneficiaries=data.get("beneficiaries", ""),
        language=data.get("language", "es"),
        ecuador_alignment=ecuador_alignment,
        strengths=data.get("strengths", []),
        risks=data.get("risks", []),
        critical_success_factors=data.get("critical_success_factors", []),
        comparable_projects=data.get("comparable_projects", []),
        format_requirements=data.get("format_requirements", {}),
        key_requirements=data.get("key_requirements", []),
        differentiators=data.get("differentiators", []),
        recommendations=data.get("recommendations", ""),
        raw_analysis=data.get("raw_analysis", raw),
        national_guidelines=data.get("national_guidelines", []),
        international_guidelines=data.get("international_guidelines", []),
        evidence_sources=data.get("evidence_sources", []),
        feasibility_breakdown=data.get("feasibility_breakdown", {}),
    )
