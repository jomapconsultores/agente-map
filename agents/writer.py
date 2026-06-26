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


def _build_prompt(brief: DocumentBrief, session: ProjectSession, corrections: list, cycle: int) -> str:
    from models.doc_types import FormatSpec, get_doc_type
    dt = get_doc_type(brief.doc_type_key)
    fmt = FormatSpec.from_dict(brief.format_spec)

    sections_str = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(brief.sections))
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
{intake_block}{empresas_block}{template_block}{support_block}{budget_block}{corrections_block}
═══════════════════════════════════════════════════════════
INSTRUCCIÓN FINAL
═══════════════════════════════════════════════════════════
Redacta el documento COMPLETO en {brief.language}, en Markdown limpio, cumpliendo el formato y los
límites de extensión. Usa datos REALES de las organizaciones disponibles; no inventes razón social,
RUC, representante legal ni datos de CVs. Cubre TODOS los campos del formulario/plantilla.
Será verificado con calificación mínima de 90% por elemento y 90% global. Hazlo impecable.
"""


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
    prompt = _build_prompt(brief, session, corrections, cycle)
    text, used = llm.complete_builder(
        provider, system=SYSTEM_PROMPT, prompt=prompt,
        max_tokens=MAX_TOKENS_WRITER, anthropic_key=api_key,
    )
    session.builder_log.append({"cycle": cycle, "requested": provider, "used": used})
    return text
