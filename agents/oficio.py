"""
AGENTE OFICIO — Redactor de oficios, peticiones y recursos administrativos
──────────────────────────────────────────────────────────────────────────
Genera documentos formales dirigidos a entidades gubernamentales o privadas
(SRI, IESS, Ministerios, GADs, etc.) conforme a la normativa ecuatoriana.

Soporta:
  - Oficio (comunicación interinstitucional)
  - Petición / Solicitud (ciudadano o empresa → entidad)
  - Recurso de reposición (impugnación en sede administrativa)
  - Recurso de apelación
  - Queja formal

El agente inyecta automáticamente el fundamento legal relevante según la
entidad destinataria.
"""
from __future__ import annotations

import config
from agents import llm

# ── Base legal por entidad ────────────────────────────────────────────────────
_LEGAL_BASES: dict[str, str] = {
    "SRI": (
        "Código Tributario (RO 38, 14-jun-2005); Ley Orgánica de Régimen Tributario Interno "
        "(LORTI) y su Reglamento (RALORTI); Ley para la Equidad Tributaria; Resoluciones NAC "
        "del SRI. Art. 66 numeral 23 Constitución (derecho a petición). Art. 101 CT "
        "(peticiones y consultas). Art. 115 CT (recursos administrativos)."
    ),
    "IESS": (
        "Ley de Seguridad Social (RO-S 465, 30-nov-2001); Estatuto del IESS; "
        "Reglamento Orgánico Funcional del IESS; Código del Trabajo; "
        "Art. 34 Constitución (derecho a la seguridad social); "
        "Art. 66 numeral 23 Constitución (derecho a petición)."
    ),
    "Ministerio del Trabajo": (
        "Código del Trabajo (RO-S 167, 16-dic-2005); Ley Orgánica del Servicio Público (LOSEP); "
        "Mandato Constituyente 8; Art. 325-333 Constitución (trabajo y empleo)."
    ),
    "Ministerio de Salud Pública": (
        "Ley Orgánica de Salud (RO-S 423, 22-dic-2006); Ley Orgánica del Sistema Nacional de "
        "Salud; Art. 32 y 362-366 Constitución; Reglamento de Establecimientos de Salud."
    ),
    "SENESCYT": (
        "Ley Orgánica de Educación Superior (LOES, RO-S 298, 12-oct-2010); "
        "Reglamento General LOES; Código Ingenios; Art. 350-357 Constitución "
        "(educación superior, ciencia y tecnología)."
    ),
    "GAD Municipal": (
        "COOTAD (RO-S 303, 19-oct-2010); Código Orgánico de Planificación y Finanzas Públicas "
        "(COPFP); Ley Orgánica de Ordenamiento Territorial; Art. 238-241 Constitución "
        "(autonomía de los GAD)."
    ),
    "GAD Provincial": (
        "COOTAD (RO-S 303, 19-oct-2010); Art. 238-241 Constitución; "
        "Reglamento Orgánico Funcional del Consejo Provincial."
    ),
    "Contraloría General del Estado": (
        "Ley Orgánica de la Contraloría General del Estado (RO-S 595, 12-jun-2002); "
        "Reglamento de la Ley de la Contraloría; Art. 211-213 Constitución; "
        "Código Orgánico Administrativo (COA)."
    ),
    "Procuraduría General del Estado": (
        "Ley Orgánica de la Procuraduría General del Estado (RO-S 312, 13-abr-2004); "
        "Art. 237 Constitución; Código Orgánico Administrativo (COA)."
    ),
    "Superintendencia de Compañías": (
        "Ley de Compañías (RO 312, 5-nov-1999); Reglamento para la Aprobación de "
        "Estatutos; Resoluciones de la Superintendencia de Compañías; "
        "Art. 319 Constitución (formas de organización de la producción)."
    ),
    "Ministerio de Ambiente": (
        "Ley Orgánica del Ambiente (TULMA); Código Orgánico del Ambiente (COA, RO-S 983, "
        "12-abr-2017); Art. 14 y 71-74 Constitución (derechos de la naturaleza)."
    ),
    "Registro Civil": (
        "Ley Orgánica de Gestión de Identidad y Datos Civiles (LOGIDAC, RO-S 684, "
        "4-feb-2016); Reglamento LOGIDAC; Art. 66 numeral 28 Constitución "
        "(derecho a identidad personal)."
    ),
    "MIPRO": (
        "Código Orgánico de la Producción, Comercio e Inversiones (COPCI, RO-S 351, "
        "29-dic-2010); Ley de Fomento Productivo; Art. 319-320 Constitución."
    ),
    "Banco Central del Ecuador": (
        "Código Orgánico Monetario y Financiero (RO-S 332, 12-sep-2014); "
        "Resoluciones de la Junta de Política y Regulación Monetaria; "
        "Art. 302-310 Constitución."
    ),
}

_DEFAULT_LEGAL = (
    "Constitución de la República del Ecuador (Art. 66 numeral 23: derecho a dirigir "
    "quejas y peticiones individuales y colectivas a las autoridades, y a recibir "
    "atención o respuestas motivadas); Código Orgánico Administrativo (COA, "
    "RO-S 31, 7-jul-2017)."
)

_DOC_TYPES = {
    "oficio": "Oficio",
    "peticion": "Petición / Solicitud",
    "recurso_reposicion": "Recurso de Reposición",
    "recurso_apelacion": "Recurso de Apelación",
    "queja": "Queja Formal",
    "memorando": "Memorando",
}

SYSTEM_PROMPT = """
Eres un abogado administrativista ecuatoriano de máximo calibre: más de 30 años litigando ante la
administración pública (SRI, IESS, GADs, ministerios, superintendencias, Contraloría) y procuraduría,
con un historial sobresaliente de peticiones atendidas y recursos ganados en sede administrativa.
Dominas al detalle la Constitución 2008, el Código Orgánico Administrativo (COA), el Código Tributario,
la normativa sectorial aplicable y la jurisprudencia relevante. Redactas oficios, peticiones, recursos,
quejas, memorandos y solicitudes con un rigor técnico-jurídico impecable.

EXHAUSTIVIDAD Y EXPERTICIA (NO NEGOCIABLE):
- Construyes el documento como un especialista que AGOTA el asunto: expones los hechos con orden y
  precisión, desarrollas un fundamento de derecho sólido y completo, y formulas la petición de forma
  inequívoca y plenamente sustentada.
- Citas con exactitud los artículos y normas pertinentes (artículo, ley, registro oficial), y los
  CONECTAS con los hechos del caso — no listas normas sueltas: explicas por qué cada una aplica.
- Anticipas y desactivas por adelantado las posibles objeciones, causales de inadmisión o negativas
  de la autoridad: cierras los flancos procesales (legitimación, oportunidad/plazos, competencia,
  requisitos formales) antes de que puedan oponerse.
- Invocas el derecho constitucional de petición (Art. 66 num. 23) y la obligación de respuesta
  motivada, y cuando proceda los plazos y el silencio administrativo aplicables.
- Tu escritura es formal, técnica, clara y persuasiva: cada párrafo carga su peso, sin relleno.

Trabajas SOLO con datos reales: no inventas nombres, cédulas/RUC, números de trámite, fechas ni
hechos que no consten en la información entregada. Si falta un dato, usa un marcador claro
(p. ej. "[completar: ____]") en vez de fabricarlo.

Formato de salida: texto del documento completo listo para impresión, sin comentarios
ni explicaciones adicionales. Usa saltos de línea para estructura clara.
"""


def _legal_for(entity: str) -> str:
    for key, text in _LEGAL_BASES.items():
        if key.lower() in entity.lower():
            return text
    return _DEFAULT_LEGAL


def _build_prompt(
    entity: str,
    entity_authority: str,
    entity_city: str,
    doc_type: str,
    subject: str,
    requester_name: str,
    requester_id: str,
    requester_role: str,
    requester_address: str,
    requester_phone: str,
    request_detail: str,
    extra_legal: str,
    extra_facts: str,
    doc_number: str,
    city: str,
    date_str: str,
) -> str:
    legal_base = _legal_for(entity)
    if extra_legal:
        legal_base = f"{legal_base}\n\nFUNDAMENTO ADICIONAL PROPORCIONADO:\n{extra_legal}"

    doc_label = _DOC_TYPES.get(doc_type, "Oficio")

    facts_block = f"\nHECHOS / ANTECEDENTES RELEVANTES:\n{extra_facts}\n" if extra_facts else ""

    return f"""
Redacta un {doc_label} formal dirigido a la siguiente entidad y autoridad, en nombre
del solicitante indicado, sobre el asunto descrito. Sigue el formato estándar ecuatoriano.

═══════════════════════════════════════════
DATOS DEL DOCUMENTO
═══════════════════════════════════════════
Tipo de documento : {doc_label}
Número de oficio  : {doc_number or "001-2025"}
Ciudad y fecha    : {city}, {date_str}

DESTINATARIO
Entidad           : {entity}
Cargo/Autoridad   : {entity_authority or "Señor/a Director/a"}
Ciudad destino    : {entity_city or city}

SOLICITANTE / REMITENTE
Nombre completo   : {requester_name}
Cédula / RUC      : {requester_id or "(no proporcionado)"}
Calidad / Cargo   : {requester_role or "compareciente"}
Dirección         : {requester_address or "(no proporcionada)"}
Teléfono / Email  : {requester_phone or "(no proporcionado)"}

ASUNTO
{subject}
{facts_block}
PETICIÓN / CONTENIDO PRINCIPAL
{request_detail}

FUNDAMENTO LEGAL A CITAR
{legal_base}
═══════════════════════════════════════════

Instrucciones de formato:
1. Encabezado con número, ciudad y fecha.
2. Datos completos del destinatario (cargo, nombre de la entidad, ciudad).
3. Fórmula de cortesía de apertura adecuada al tipo de documento.
4. Antecedentes/exposición de hechos — relata los hechos de forma ordenada, cronológica y completa.
5. Fundamento de derecho EXHAUSTIVO — cita y desarrolla al menos 3-5 artículos específicos de las
   normas listadas, conectando CADA uno con los hechos del caso; invoca el derecho de petición y la
   obligación de respuesta motivada; cuando aplique, los plazos y el silencio administrativo.
6. Anticipación de objeciones: cierra por adelantado los posibles motivos de inadmisión o negativa
   (competencia, legitimación, oportunidad/plazo, requisitos formales).
7. Petición concreta, numerada si hay varios puntos, inequívoca y plenamente sustentada.
8. Fórmula de cierre formal.
9. Espacio para firma (nombre, cargo, CI/RUC, dirección, teléfono).
10. Si aplica: lista de documentos adjuntos al final.

Redacta el documento completo, exhaustivo y técnicamente blindado ahora:
"""


def generate(
    *,
    entity: str,
    entity_authority: str = "",
    entity_city: str = "",
    doc_type: str = "peticion",
    subject: str,
    requester_name: str,
    requester_id: str = "",
    requester_role: str = "",
    requester_address: str = "",
    requester_phone: str = "",
    request_detail: str,
    extra_legal: str = "",
    extra_facts: str = "",
    doc_number: str = "",
    city: str = "Cuenca",
    date_str: str = "",
    api_key: str | None = None,
) -> str:
    """Genera el documento oficial y devuelve el texto completo."""
    import datetime
    if not date_str:
        today = datetime.date.today()
        meses = ["enero","febrero","marzo","abril","mayo","junio",
                 "julio","agosto","septiembre","octubre","noviembre","diciembre"]
        date_str = f"{today.day} de {meses[today.month-1]} de {today.year}"

    prompt = _build_prompt(
        entity=entity,
        entity_authority=entity_authority,
        entity_city=entity_city,
        doc_type=doc_type,
        subject=subject,
        requester_name=requester_name,
        requester_id=requester_id,
        requester_role=requester_role,
        requester_address=requester_address,
        requester_phone=requester_phone,
        request_detail=request_detail,
        extra_legal=extra_legal,
        extra_facts=extra_facts,
        doc_number=doc_number,
        city=city,
        date_str=date_str,
    )

    text, _ = llm.complete_builder(
        config.ROLE_WRITER,
        system=SYSTEM_PROMPT,
        prompt=prompt,
        max_tokens=8000,  # 4000→8000: fundamento de derecho exhaustivo sin truncar
        anthropic_key=api_key,
        temperature=0.3,
    )
    return text.strip()


def list_entities() -> list[dict]:
    """Entidades disponibles con su base legal resumida."""
    entities = list(_LEGAL_BASES.keys()) + ["Otra entidad"]
    return [{"name": e, "has_legal_base": e in _LEGAL_BASES} for e in entities]


def list_doc_types_oficio() -> list[dict]:
    return [{"key": k, "name": v} for k, v in _DOC_TYPES.items()]
