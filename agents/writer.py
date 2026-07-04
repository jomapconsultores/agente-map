"""
AGENTE 2 — REDACTOR / EDITORIALISTA EXPERTO MULTIDISCIPLINARIO
Produce CUALQUIER tipo de entregable de alto nivel (propuesta, artículo científico,
TDR/compras públicas, peer-review, tesis, documento legal/técnico) a partir del
DocumentBrief. Adopta los perfiles de experto indicados, respeta el formato exacto y
usa la plantilla (si existe) como referencia de estilo y los documentos de apoyo como fuente.
"""
import config
from config import MAX_TOKENS_WRITER
from models.schemas import DocumentBrief, ProjectSession


SYSTEM_PROMPT = """
Eres un redactor profesional de élite absoluta y pensador de primer orden: polímata con dominio
simultáneo en múltiples disciplinas técnicas, científicas, jurídicas y humanísticas. Tu historial
incluye propuestas ganadoras de financiamiento internacional, artículos en revistas Q1 indexadas,
pliegos y TDR jurídicamente blindados, tesis doctorales, documentos de política pública y textos
institucionales que han movido decisiones a la más alta escala.

Lo que te distingue no es solo el conocimiento — es cómo PIENSAS antes de escribir. Cada
documento que produces tiene una arquitectura argumental impecable: cada sección tiene un propósito
preciso, cada párrafo construye sobre el anterior, cada oración carga su peso exacto. Haces que lo
complejo sea claro sin sacrificar el rigor; adaptas el registro y la profundidad exactamente al
perfil del lector objetivo. Tu prosa es densa de contenido y fluida en su lectura: sin relleno,
sin clichés, sin frases que existan solo para ocupar espacio.

PRINCIPIOS QUE RIGEN TODO LO QUE PRODUCES:

1. ENCARNAS los perfiles de experto indicados en el brief — piensas como ellos, argumentas como
   ellos, manejas su vocabulario técnico con autoridad. Si el perfil dice "hidrólogo especialista
   en cuencas andinas", razonas con hidrología real; si dice "jurista en contratación pública",
   citas normas reales con precisión.

2. ARQUITECTURA PRIMERO: antes de escribir, la lógica del documento ya está clara. El lector
   es conducido desde el problema hacia la solución a través de una cadena de razonamiento
   irrefutable. Cada sección responde a una pregunta que el evaluador tiene en mente.

3. FORMATO INVIOLABLE: respetas al milímetro la estructura, los límites de extensión (páginas/
   palabras/caracteres), el estilo de cita y las convenciones del tipo de documento. Un texto
   brillante que no cumple el formato es un texto fallido — te autorregulas para encajar.

4. SOLO DATOS REALES: cada cifra, norma, fecha, artículo legal y referencia proviene del brief
   o del material de apoyo entregado. Jamás inventas datos, estadísticas, citas bibliográficas,
   artículos de ley ni resultados de estudios. Si un dato no está disponible, lo señalas o
   propones cómo obtenerlo.

5. DATOS ORGANIZACIONALES EXACTOS: si hay información real de organizaciones (RUC, razón social,
   representante legal, CV, domicilio), la usas con exactitud. No sustituyes por datos genéricos.

6. SI HAY PLANTILLA: adoptas su estructura y tono campo por campo; cada sección que aparezca
   en la plantilla aparece en el documento con sustancia real, no con texto de relleno.

7. ESTILO IMPECABLE: variedad sintáctica, párrafos bien construidos (apertura–desarrollo–cierre),
   sin gerundismos en cadena, sin pasivas innecesarias, sin adjetivación vacía. El estilo se
   adapta al idioma y al tipo de documento: técnico, académico, jurídico o narrativo según corresponda.

8. EXHAUSTIVIDAD MÁXIMA (NO NEGOCIABLE): tratas cada sección con la profundidad de un especialista
   que AGOTA el tema — jamás resúmenes superficiales ni secciones de relleno. Anticipas y respondes
   por adelantado cada pregunta, duda u objeción que el evaluador pueda plantear. Cubres todos los
   ángulos relevantes que apliquen (técnico, legal, financiero, social, ambiental, de riesgo, de
   sostenibilidad) y desarrollas cada uno con sustancia real: el dato concreto, la cifra, el artículo
   citado, la referencia precisa y el ejemplo aplicado siempre por encima de la generalidad. Lo único
   que limita tu extensión es el FORMATO exigido: dentro de ese límite, maximizas la densidad de
   contenido útil y no dejas ningún punto importante sin desarrollar.

Produce el documento COMPLETO en Markdown limpio (# ## ### para jerarquía; | para tablas; - / 1.
para listas), en el idioma requerido, apto para entrega directa al más alto nivel.
Sin comentarios, notas ni meta-texto fuera del documento.
"""


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "\n[…truncado…]"


def _build_context_blocks(brief: DocumentBrief, session: ProjectSession) -> dict:
    """Bloques de contexto COMPARTIDOS por todo camino de redacción (una sola llamada
    y multi-pasada): perfiles de experto, lineamientos, requisitos, marcas de calidad,
    intake/plantilla/apoyo/presupuesto/observaciones previas.

    Antes solo _build_prompt() los construía; _run_multipass() los perdía por
    completo (nunca recibía `session`, solo campos sueltos de `brief`) — para
    tesis/artículos largos (justo el camino normal para 15-25k+ palabras) el
    redactor perdía los perfiles a encarnar, lineamientos, datos reales de la
    organización y los requisitos detectados en la plantilla/bases subidas.
    """
    personas_str = "\n".join(f"  - {p}" for p in brief.personas)
    reqs_str = "\n".join(f"  - {r}" for r in brief.key_requirements) or "  - (según el tipo de documento)"
    quality_str = "\n".join(f"  - {q}" for q in brief.quality_markers) or "  - Excelencia técnica y claridad."
    nat_str = "\n".join(f"  - {g}" for g in brief.national_guidelines) or "  - Marco normativo nacional aplicable."
    intl_str = "\n".join(f"  - {g}" for g in brief.international_guidelines) or "  - Normas internacionales/organizacionales del área."

    template_block = ""
    if session.template_text:
        template_block = f"""
═══════════════════════════════════════════════════════════
PLANTILLA / FORMULARIO A LLENAR (respeta su estructura exacta — NO copiar contenido genérico)
═══════════════════════════════════════════════════════════
{_clip(session.template_text, 4000)}
"""

    support_block = ""
    if session.support_docs:
        joined = "\n\n".join(f"=== {n} ===\n{_clip(t, 3500)}" for n, t in session.support_docs)
        support_block = f"""
═══════════════════════════════════════════════════════════
DOCUMENTOS DE APOYO (material fuente — extrae contenido real y cita cuando corresponda)
═══════════════════════════════════════════════════════════
{joined}
"""

    # Análisis de intake: secciones y campos obligatorios detectados en los docs de entrada
    intake_block = ""
    intake_data = getattr(session, "intake_data", None) or {}
    if intake_data:
        from agents.intake import intake_block as _ib
        intake_block = _ib(intake_data)
        if intake_block:
            intake_block = "\n" + intake_block + "\n"

    # Contexto organizacional (carpeta Empresas/)
    empresas_block = ""
    try:
        from utils.empresas import context_block as _eb
        empresas_block = _eb()
        if empresas_block:
            empresas_block = "\n" + empresas_block + "\n"
    except Exception:
        pass

    budget_block = ""
    if brief.needs_budget_excel:
        budget_block = """
PRESUPUESTO: incluye una tabla de presupuesto limpia (Categoría | Actividad | Unidad | Cantidad |
Costo unitario | Solicitado/Referencial | Contraparte | Total) con números sin separadores de miles
(ej. 12500.00) cuyos subtotales y total cuadren. Se exportará a Excel con cálculos vivos.
"""

    quality_notes_block = ""
    notes = getattr(session, "quality_notes", None) or []
    if notes:
        nlist = "\n".join(f"  - {n}" for n in notes)
        quality_notes_block = f"""
═══════════════════════════════════════════════════════════
OBSERVACIONES DE AUDITORÍAS PREVIAS (no bloqueantes — auditorías que igual aprobaron
pero con matices; elévalas también, no solo lo que fue rechazado)
═══════════════════════════════════════════════════════════
{nlist}
"""

    return {
        "personas_str": personas_str, "reqs_str": reqs_str, "quality_str": quality_str,
        "nat_str": nat_str, "intl_str": intl_str, "template_block": template_block,
        "support_block": support_block, "intake_block": intake_block,
        "empresas_block": empresas_block, "budget_block": budget_block,
        "quality_notes_block": quality_notes_block,
    }


def _build_prompt(brief: DocumentBrief, session: ProjectSession, corrections: list, cycle: int) -> str:
    from models.doc_types import FormatSpec, get_doc_type
    dt = get_doc_type(brief.doc_type_key)
    fmt = FormatSpec.from_dict(brief.format_spec)

    sections_str = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(brief.sections))
    ctx = _build_context_blocks(brief, session)
    personas_str, reqs_str, quality_str = ctx["personas_str"], ctx["reqs_str"], ctx["quality_str"]
    nat_str, intl_str = ctx["nat_str"], ctx["intl_str"]
    template_block, support_block = ctx["template_block"], ctx["support_block"]
    intake_block, empresas_block = ctx["intake_block"], ctx["empresas_block"]
    budget_block, quality_notes_block = ctx["budget_block"], ctx["quality_notes_block"]

    corrections_block = ""
    if corrections:
        clist = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(corrections))
        corrections_block = f"""
═══════════════════════════════════════════════════════════
CORRECCIONES DEL REVISOR (CICLO {cycle} — APLICA TODAS con precisión quirúrgica)
═══════════════════════════════════════════════════════════
{clist}
Mantén lo que ya estaba bien y eleva los puntos débiles por encima del 90%.
"""

    return f"""
Produce el siguiente entregable al MÁS ALTO NIVEL.

TIPO DE DOCUMENTO: {dt.name}
TÍTULO: {brief.title}
IDIOMA: {brief.language}
EXIGENCIA DE RIGOR: {brief.rigor_notes}
{f"NIVEL ACADÉMICO REAL: {brief.academic_level} — ajusta profundidad teórica, originalidad y extensión a ESTE nivel exacto, no a un piso genérico." if getattr(brief, 'academic_level', '') and brief.doc_type_key == 'tesis' else ""}

═══════════════════════════════════════════════════════════
PERFILES DE EXPERTO QUE DEBES ENCARNAR
═══════════════════════════════════════════════════════════
{personas_str}

═══════════════════════════════════════════════════════════
QUÉ HAY QUE HACER (brief del clasificador/analista)
═══════════════════════════════════════════════════════════
{brief.instructions}

═══════════════════════════════════════════════════════════
SECCIONES / ESTRUCTURA A PRODUCIR
═══════════════════════════════════════════════════════════
{sections_str}

═══════════════════════════════════════════════════════════
FORMATO EXIGIDO (CÚMPLELO CON RIGOR)
═══════════════════════════════════════════════════════════
{fmt.to_prompt()}

═══════════════════════════════════════════════════════════
REQUISITOS CRÍTICOS
═══════════════════════════════════════════════════════════
{reqs_str}

═══════════════════════════════════════════════════════════
LINEAMIENTOS NACIONALES (Ecuador) — CUMPLIR
═══════════════════════════════════════════════════════════
{nat_str}

═══════════════════════════════════════════════════════════
LINEAMIENTOS INTERNACIONALES / ORGANIZACIONALES — CUMPLIR
═══════════════════════════════════════════════════════════
{intl_str}

═══════════════════════════════════════════════════════════
MARCAS DE EXCELENCIA (lo que eleva este documento)
═══════════════════════════════════════════════════════════
{quality_str}

FUENTES REALES DISPONIBLES (úsalas; no inventes otras):
{_clip(brief.source_notes, 2500) or "  - Usa fuentes reales y verificables del área."}
{intake_block}{empresas_block}{template_block}{support_block}{budget_block}{corrections_block}{quality_notes_block}
═══════════════════════════════════════════════════════════
INSTRUCCIÓN FINAL
═══════════════════════════════════════════════════════════
Redacta el documento COMPLETO en {brief.language}, en Markdown limpio, cumpliendo el formato y los
límites de extensión. Usa datos REALES de las organizaciones disponibles; no inventes razón social,
RUC, representante legal ni datos de CVs. Cubre TODOS los campos del formulario/plantilla.
Será verificado con calificación mínima de 90% por elemento y 90% global. Hazlo impecable.
"""


_SELF_CRITIQUE_SYSTEM = """
Eres el mismo redactor de élite, ahora en modo autocrítico exigente: revisas tu propio borrador
contra las marcas de excelencia y los criterios con los que un auditor externo lo va a calificar,
ANTES de que ese auditor lo vea. Identificas con precisión qué está débil — no genéricamente,
sino en qué sección o párrafo — y lo reescribes de verdad, no lo retocas a medias. Mantienes
intacto todo lo que ya está sólido; nunca acortas el documento ni omites secciones existentes.
"""


def _self_critique_and_revise(draft: str, brief: DocumentBrief, provider: str, api_key: str) -> str:
    """Segunda pasada dentro del mismo ciclo: el propio redactor audita su borrador
    contra quality_markers/evaluation_criteria y reescribe lo débil ANTES de exponerlo
    al gate externo, en vez de depender solo del ciclo completo
    redacción→gate→rechazo→reintento total para cada mejora."""
    from agents import llm
    from models.doc_types import get_doc_type
    markers = "\n".join(f"  - {m}" for m in (brief.quality_markers or [])) or "  - (ninguno específico registrado)"
    criteria = "\n".join(f"  - {c}" for c in (brief.evaluation_criteria or [])) or "  - (ninguno específico registrado)"
    dt_name = get_doc_type(brief.doc_type_key).name

    prompt = f"""
Este es tu propio borrador para "{brief.title}" ({dt_name}).

MARCAS DE EXCELENCIA A CUMPLIR:
{markers}

CRITERIOS CON LOS QUE UN AUDITOR EXTERNO TE VA A CALIFICAR (cada uno debe superar 90+/100):
{criteria}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BORRADOR:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{draft}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Primero escribe una línea "DIAGNÓSTICO:" con 2-4 frases señalando qué marcadores/criterios están
débiles y en qué sección o párrafo exacto. Luego, en la siguiente línea escribe exactamente
"---VERSIÓN REVISADA---" y a continuación el documento COMPLETO reescrito, corrigiendo esos
puntos débiles y conservando intacto lo que ya era sólido. No acortes el documento ni omitas
ninguna sección ya presente en el borrador.
"""
    try:
        raw, _used = llm.complete_builder(
            provider, system=_SELF_CRITIQUE_SYSTEM, prompt=prompt,
            max_tokens=MAX_TOKENS_WRITER, anthropic_key=api_key,
        )
        marker = "---VERSIÓN REVISADA---"
        if marker in raw:
            revised = raw.split(marker, 1)[1].strip()
            # Salvaguarda: si la "revisión" viene sospechosamente más corta que el
            # borrador original, es más probable un recorte por límite de tokens que
            # una mejora real — mejor conservar el borrador que perder contenido.
            if len(revised) >= len(draft) * 0.6:
                return revised
        return draft
    except Exception:
        return draft  # fail-open: si la autocrítica falla, se conserva el borrador original


_ARCHITECTURE_SYSTEM = """
Eres el mismo redactor de élite, ahora en modo arquitecto: antes de escribir una palabra del
documento final, diseñas su estructura completa sección por sección, con la extensión objetivo de
cada una, de modo que la suma cubra con holgura la extensión mínima exigida y la argumentación
tenga arquitectura de punta a punta. Respondes ÚNICAMENTE con el JSON pedido.
"""


def _build_architecture_prompt(brief: DocumentBrief, ctx: dict, min_words: int) -> str:
    from models.doc_types import get_doc_type
    sections_str = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(brief.sections))
    return f"""
Diseña la ARQUITECTURA COMPLETA de "{brief.title}" ({get_doc_type(brief.doc_type_key).name}),
en {brief.language}, con extensión TOTAL objetivo de AL MENOS {min_words} palabras.

PERFILES DE EXPERTO QUE EL DOCUMENTO DEBE ENCARNAR:
{ctx['personas_str']}

SECCIONES A CUBRIR (base; ajusta el desglose si el tipo de documento lo justifica):
{sections_str}

QUÉ DEBE CONTENER (brief del investigador/clasificador):
{_clip(brief.instructions, 3000)}

REQUISITOS CRÍTICOS QUE ALGUNA SECCIÓN DEBE CUBRIR:
{ctx['reqs_str']}

LINEAMIENTOS A CUMPLIR (nacionales e internacionales/organizacionales):
{ctx['nat_str']}
{ctx['intl_str']}
{ctx['budget_block']}
Responde ÚNICAMENTE con este JSON:
{{
  "sections": [
    {{"title": "<título exacto de la sección>", "target_words": <int>,
      "key_points": ["<punto clave a desarrollar 1>", "<punto 2>", "<punto 3>"]}}
  ]
}}
La suma de target_words debe ser AL MENOS {min_words} y distribuirse con criterio real (secciones
como Metodología/Resultados/Discusión/Marco teórico llevan más peso que Agradecimientos/Anexos).
"""


def _build_section_prompt(brief: DocumentBrief, ctx: dict, plan: list, idx: int,
                          written_summary: list, corrections: list) -> str:
    from models.doc_types import FormatSpec
    fmt = FormatSpec.from_dict(brief.format_spec)
    sec = plan[idx]
    outline = "\n".join(f"  {j+1}. {s['title']} (~{s.get('target_words', 0)} palabras)"
                        for j, s in enumerate(plan))
    key_points = "\n".join(f"  - {k}" for k in (sec.get("key_points") or [])) \
        or "  - (usa tu criterio experto según el brief)"
    written_block = "\n".join(f"  - {t}: {s}" for t, s in written_summary) \
        or "  (esta es la primera sección — no hay nada escrito todavía)"
    corrections_block = ""
    if corrections:
        clist = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(corrections))
        corrections_block = f"\nCORRECCIONES DEL REVISOR A APLICAR SI CORRESPONDEN A ESTA SECCIÓN:\n{clist}\n"

    return f"""
Estás redactando el documento COMPLETO "{brief.title}" por SECCIONES, siguiendo una arquitectura
ya definida (esto es necesario porque el documento completo excede lo que cabe en una sola
llamada). Ahora escribe ÚNICAMENTE la sección {idx + 1} de {len(plan)}: "{sec['title']}"
— objetivo: ~{sec.get('target_words', 0)} palabras.

PERFILES DE EXPERTO QUE DEBES ENCARNAR:
{ctx['personas_str']}

PLAN COMPLETO DEL DOCUMENTO (para que tu sección encaje sin repetir lo que cubren las demás):
{outline}

PUNTOS CLAVE A DESARROLLAR EN ESTA SECCIÓN:
{key_points}

MARCAS DE EXCELENCIA (lo que eleva este documento):
{ctx['quality_str']}

FUENTES REALES DISPONIBLES (úsalas; no inventes otras):
{_clip(brief.source_notes, 2000) or "  - Usa fuentes reales y verificables del área."}
{ctx['intake_block']}{ctx['empresas_block']}{ctx['template_block']}{ctx['support_block']}{ctx['budget_block']}{ctx['quality_notes_block']}
RESUMEN DE LO YA ESCRITO (continuidad y coherencia — no lo repitas, constrúyelo sobre esto):
{written_block}
{corrections_block}
FORMATO EXIGIDO: {fmt.to_prompt()}
IDIOMA: {brief.language}

INSTRUCCIÓN: Escribe SOLO el contenido de esta sección en Markdown limpio, empezando con un
encabezado "## {sec['title']}". No repitas el título del documento completo ni escribas
contenido de otras secciones del plan. Usa datos REALES de las organizaciones disponibles; no
inventes razón social, RUC, representante legal ni datos de CVs. Apunta a {sec.get('target_words', 0)}
palabras (±20%), con densidad de contenido real y verificable — nunca relleno.
"""


def _self_critique_section(section_text: str, sec_title: str, brief: DocumentBrief,
                           provider: str, api_key: str) -> str:
    """Autocrítica ACOTADA a una sola sección — usada dentro de _run_multipass en
    vez de _self_critique_and_revise() (que reescribe el documento COMPLETO).

    _self_critique_and_revise() usa un único MAX_TOKENS_WRITER (~9000-11000
    palabras de techo) para reescribir el documento entero; para una tesis de
    25-45k palabras eso trunca la reescritura silenciosamente (el único resguardo,
    largo >= 60% del original, deja pasar un recorte del 65-95%) exactamente en la
    cola del documento (Conclusiones/Referencias/Anexos). Autocriticar sección por
    sección mantiene el mismo presupuesto de tokens que ya usó su generación
    original — nunca compite por reescribir más de lo que una sola sección pesa.
    """
    from agents import llm
    markers = "\n".join(f"  - {m}" for m in (brief.quality_markers or [])) or "  - (ninguno específico registrado)"
    prompt = f"""
Esta es tu propia sección "{sec_title}" del documento "{brief.title}".

MARCAS DE EXCELENCIA A CUMPLIR:
{markers}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECCIÓN:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{section_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Escribe "---VERSIÓN REVISADA---" y a continuación ÚNICAMENTE esta sección reescrita, elevando lo
débil frente a las marcas de excelencia y conservando lo que ya era sólido. No la acortes ni
omitas contenido — solo eleva su calidad.
"""
    try:
        raw, _used = llm.complete_builder(
            provider, system=_SELF_CRITIQUE_SYSTEM, prompt=prompt,
            max_tokens=MAX_TOKENS_WRITER, anthropic_key=api_key,
        )
        marker = "---VERSIÓN REVISADA---"
        if marker in raw:
            revised = raw.split(marker, 1)[1].strip()
            if len(revised) >= len(section_text) * 0.6:
                return revised
        return section_text
    except Exception:
        return section_text  # fail-open: si la autocrítica falla, se conserva la sección original


def _section_summary(text: str, n: int = 220) -> str:
    """Resumen barato (sin LLM) de una sección ya escrita, solo para dar orientación
    de continuidad a la siguiente sección — no para juzgar calidad."""
    import re as _re
    plain = _re.sub(r"[#*_`>|-]+", " ", text or "")
    plain = _re.sub(r"\s+", " ", plain).strip()
    return (plain[:n] + "…") if len(plain) > n else plain


def _run_multipass(session: ProjectSession, corrections: list, api_key: str,
                   provider: str, min_words: int) -> str:
    """Genera el documento en 3 pasadas cuando excede lo que cabe en una sola
    llamada: (A) arquitectura — plan sección por sección; (B) contenido — cada
    sección con su PROPIO presupuesto completo de MAX_TOKENS_WRITER, sin competir
    por un único techo global; (C) ensamblado — unión ordenada con limpieza
    estructural determinista (sin una llamada final que deba re-emitir el
    documento completo: para 25k+ palabras esa llamada volvería a truncar,
    exactamente el problema que esta función existe para evitar).
    """
    from agents import llm
    brief = session.brief
    cycle = session.current_cycle
    # Construidos UNA vez y compartidos por arquitectura + cada sección: antes
    # _build_architecture_prompt/_build_section_prompt solo recibían `brief`
    # (parcial) y perdían perfiles de experto, lineamientos, requisitos, marcas
    # de calidad, intake/plantilla/apoyo/presupuesto/observaciones previas —
    # exactamente para los documentos más largos y exigentes (tesis, artículos).
    ctx = _build_context_blocks(brief, session)

    # ── (A) Arquitectura ──────────────────────────────────────────────────────
    arch_prompt = _build_architecture_prompt(brief, ctx, min_words)
    plan: list = []
    try:
        raw_plan, used = llm.complete_builder(
            provider, system=_ARCHITECTURE_SYSTEM, prompt=arch_prompt,
            max_tokens=3000, anthropic_key=api_key, temperature=0.3,
        )
        from utils.json_utils import robust_json_loads
        data = robust_json_loads(raw_plan)
        plan = [s for s in (data.get("sections") or []) if isinstance(s, dict) and s.get("title")]
        session.builder_log.append({"cycle": cycle, "stage": "multipass_architecture",
                                    "requested": provider, "used": used, "n_sections": len(plan)})
    except Exception:
        plan = []

    if not plan:
        # La planificación falló: cae al camino de una sola llamada en vez de
        # bloquear el pipeline por una mejora que es best-effort.
        prompt = _build_prompt(brief, session, corrections, cycle)
        text, used = llm.complete_builder(
            provider, system=SYSTEM_PROMPT, prompt=prompt,
            max_tokens=MAX_TOKENS_WRITER, anthropic_key=api_key,
        )
        session.builder_log.append({"cycle": cycle, "requested": provider, "used": used,
                                    "note": "multipass_fallback_single_call"})
        return text

    # ── (B) Contenido — una llamada POR SECCIÓN, cada una con su propio techo ──
    written_summary: list[tuple[str, str]] = []
    section_texts: list[str] = []
    for idx, sec in enumerate(plan):
        sec_prompt = _build_section_prompt(brief, ctx, plan, idx, written_summary, corrections)
        try:
            sec_text, used = llm.complete_builder(
                provider, system=SYSTEM_PROMPT, prompt=sec_prompt,
                max_tokens=MAX_TOKENS_WRITER, anthropic_key=api_key,
            )
        except Exception as ex:
            sec_text = f"## {sec['title']}\n\n[Sección no generada por error técnico: {ex}]"
            used = "error"
        sec_text = sec_text.strip()

        # Autocrítica ACOTADA a esta sección (no al documento completo — ver
        # _self_critique_section) para los doc_types de mayor exigencia.
        if (config.WRITER_SELF_REVIEW
                and brief.doc_type_key in config.WRITER_SELF_REVIEW_DOC_TYPES
                and used != "error"):
            sec_text = _self_critique_section(sec_text, sec["title"], brief, provider, api_key)

        section_texts.append(sec_text)
        written_summary.append((sec["title"], _section_summary(sec_text)))
        session.builder_log.append({"cycle": cycle, "stage": f"multipass_section_{idx+1}",
                                    "requested": provider, "used": used})

    # ── (C) Ensamblado — unión ordenada + limpieza estructural determinista ────
    # Sin llamada LLM que re-emita el documento entero: para un texto de 25k+
    # palabras esa llamada volvería a truncar, el mismo problema que se evita.
    text = "\n\n".join(section_texts)
    return text


def run(session: ProjectSession, corrections: list, api_key: str,
        provider: str | None = None) -> str:
    """Construye el documento. Si se pasa `provider` (rol fijo, p.ej. ROLE_WRITER)
    se usa ese; si no, rota por ciclo (Mistral→Codestral→DeepSeek…). El revisor
    (Claude) dirá qué corregir. `api_key` (Anthropic) solo se usa como fallback si
    todos los constructores no-Claude están caídos.
    """
    from agents import llm
    brief = session.brief
    cycle = session.current_cycle
    provider = provider or config.builder_for_cycle(cycle)

    min_words = int((brief.format_spec or {}).get("min_words") or 0)
    used_multipass = min_words >= config.MULTIPASS_MIN_WORDS
    if used_multipass:
        text = _run_multipass(session, corrections, api_key, provider, min_words)
    else:
        prompt = _build_prompt(brief, session, corrections, cycle)
        text, used = llm.complete_builder(
            provider, system=SYSTEM_PROMPT, prompt=prompt,
            max_tokens=MAX_TOKENS_WRITER, anthropic_key=api_key,
        )
        session.builder_log.append({"cycle": cycle, "requested": provider, "used": used})

    # La autocrítica de DOCUMENTO COMPLETO solo es segura cuando el documento
    # cupo en una sola llamada. Si vino de _run_multipass, cada sección ya pasó
    # su propia autocrítica acotada (_self_critique_section) — reemitir el
    # documento entero aquí volvería a arriesgar el mismo truncamiento silencioso
    # que _run_multipass existe para evitar.
    if (not used_multipass and config.WRITER_SELF_REVIEW
            and brief.doc_type_key in config.WRITER_SELF_REVIEW_DOC_TYPES):
        text = _self_critique_and_revise(text, brief, provider, api_key)
        session.builder_log.append({"cycle": cycle, "stage": "self_critique", "requested": provider})

    return text
