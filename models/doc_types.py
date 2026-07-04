"""
REGISTRO DE TIPOS DE DOCUMENTO Y ESPECIFICACIONES DE FORMATO
────────────────────────────────────────────────────────────
Define QUÉ tipo de entregable produce el sistema y CON QUÉ formato y rigor.
Cada DocType trae: perfiles de experto, secciones, criterios de evaluación,
si requiere presupuesto en Excel, y un FormatSpec por defecto (tipo y tamaño
de letra, márgenes, interlineado, estilo de cita, límites de páginas/palabras/
caracteres). El clasificador puede ajustar el FormatSpec según los requisitos
detectados o la plantilla subida.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class FormatSpec:
    """Especificación tipográfica y de extensión, aplicada con rigor en el Word."""
    font_name: str = "Times New Roman"
    font_size: float = 12.0           # en puntos
    line_spacing: float = 1.5         # 1.0 | 1.15 | 1.5 | 2.0
    margin_top_cm: float = 2.5
    margin_bottom_cm: float = 2.5
    margin_left_cm: float = 3.0
    margin_right_cm: float = 2.5
    alignment: str = "justify"        # justify | left
    citation_style: str = ""          # APA 7, Vancouver, IEEE, Chicago, ...
    language: str = "es"

    # Límites (None = sin límite)
    max_pages: Optional[int] = None
    min_pages: Optional[int] = None
    max_words: Optional[int] = None
    min_words: Optional[int] = None
    max_chars: Optional[int] = None
    notes: str = ""                   # otras exigencias formales

    def to_prompt(self) -> str:
        lim = []
        if self.min_pages or self.max_pages:
            lim.append(f"páginas: {self.min_pages or 0}–{self.max_pages or '∞'}")
        if self.min_words or self.max_words:
            lim.append(f"palabras: {self.min_words or 0}–{self.max_words or '∞'}")
        if self.max_chars:
            lim.append(f"máx. caracteres: {self.max_chars}")
        lim_str = "; ".join(lim) if lim else "sin límites estrictos"
        cite = f"; estilo de cita: {self.citation_style}" if self.citation_style else ""
        return (
            f"Tipo de letra: {self.font_name} {self.font_size}pt; "
            f"interlineado: {self.line_spacing}; alineación: {self.alignment}; "
            f"márgenes (cm) sup/inf/izq/der: {self.margin_top_cm}/{self.margin_bottom_cm}/"
            f"{self.margin_left_cm}/{self.margin_right_cm}; {lim_str}{cite}. "
            f"{self.notes}".strip()
        )

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FormatSpec":
        if not d:
            return cls()
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class DocType:
    key: str
    name: str
    description: str
    personas: list                  # perfiles de experto que adopta el sistema
    sections: list                  # secciones por defecto
    evaluation_criteria: list       # ejes que evalúa el revisor (además de formato + cumplimiento)
    format: FormatSpec
    needs_budget_excel: bool = False
    is_proposal: bool = False       # usa la ruta Analista (viabilidad/financiador)
    rigor_notes: str = ""           # exigencias de máximo nivel para el redactor
    module: str = "proyectos"       # módulo principal: "investigacion" | "proyectos" | "ambos"
    # Umbral de aprobación propio (global y por criterio), más estricto que el
    # genérico APPROVAL_THRESHOLD/ELEMENT_THRESHOLD (90). None = usa el genérico.
    # Un TDR rutinario y una tesis doctoral no deberían aprobarse con la misma
    # exigencia — los tipos de mayor riesgo reputacional (académicos, legales)
    # la tienen más alta.
    strict_threshold: Optional[int] = None


# ─────────────────────────────────────────────────────────────────────────────
#  CRITERIOS BASE (siempre se añaden a los específicos de cada tipo)
# ─────────────────────────────────────────────────────────────────────────────
BASE_CRITERIA = [
    "Cumplimiento de Formato (tipo/tamaño de letra, márgenes, interlineado y límites de extensión)",
    "Cumplimiento de Lineamientos Nacionales (marco legal/normativo del Ecuador aplicable)",
    "Cumplimiento de Lineamientos Internacionales/Organizacionales (normas del organismo, editoriales o estándares del área)",
]


# ─────────────────────────────────────────────────────────────────────────────
#  REGISTRO DE TIPOS
# ─────────────────────────────────────────────────────────────────────────────
DOC_TYPES: dict = {
    "propuesta": DocType(
        key="propuesta",
        name="Propuesta de financiamiento no reembolsable",
        description="Propuesta para cooperación internacional (BID, CAF, BM, UE, PNUD, GIZ, USAID, etc.).",
        personas=[
            "Consultor senior en financiamiento internacional para el desarrollo (28 años)",
            "Especialista en Marco Lógico y Gestión por Resultados",
            "Economista de proyectos / presupuestación",
            "Redactor experto de proyectos y editorialista profesional (claridad, persuasión y estilo)",
        ],
        sections=[
            "Resumen ejecutivo", "Antecedentes y contexto", "Justificación",
            "Población objetivo", "Objetivos (general y específicos)", "Marco lógico",
            "Metodología y plan de trabajo", "Indicadores y M&E", "Presupuesto",
            "Equipo y capacidad institucional", "Alianzas", "Sostenibilidad",
            "Gestión de riesgos", "Enfoque de género y ambiental", "Bibliografía", "Anexos",
        ],
        evaluation_criteria=[
            "Pertinencia y Relevancia", "Calidad Técnica y Metodología",
            "Viabilidad y Sostenibilidad", "Marco Lógico / Gestión por Resultados",
            "Capacidad Institucional y Equipo",
        ],
        format=FormatSpec(font_name="Calibri", font_size=11, line_spacing=1.15,
                          citation_style="APA 7", min_words=2500,
                          notes="Tablas de marco lógico y presupuesto incluidas."),
        needs_budget_excel=True,
        is_proposal=True,
        rigor_notes="Propuesta lista para ganar una convocatoria real; cada objetivo con indicadores SMART.",
    ),

    "articulo_cientifico": DocType(
        key="articulo_cientifico",
        name="Artículo científico",
        description="Paper académico con estructura IMRyD y referencias, listo para someter a una revista indexada.",
        personas=[
            "Investigador principal (PhD) experto en el área temática",
            "Metodólogo/estadístico de investigación",
            "Editor científico y corrector de estilo académico",
        ],
        sections=[
            "Título", "Resumen / Abstract (con palabras clave)", "Introducción",
            "Materiales y métodos", "Resultados", "Discusión", "Conclusiones",
            "Agradecimientos", "Declaración de conflictos de interés", "Referencias",
        ],
        evaluation_criteria=[
            "Originalidad y aporte al conocimiento",
            "Rigor metodológico y validez", "Análisis y tratamiento de datos",
            "Solidez de la argumentación y discusión",
            "Calidad y actualidad de las referencias (citas verificables)",
            "Claridad, estilo académico y coherencia (IMRyD)",
            "Potencia estadística y tamaño de muestra reportados (si aplica diseño cuantitativo)",
            "Novedad frente a la literatura de los últimos 3-5 años",
            "Declaración de disponibilidad de datos y conflictos de interés",
        ],
        format=FormatSpec(font_name="Times New Roman", font_size=12, line_spacing=2.0,
                          citation_style="APA 7", min_words=3000, max_words=8000,
                          notes="Abstract ≤250 palabras; figuras y tablas numeradas y citadas en el texto."),
        rigor_notes="Máximo rigor científico: hipótesis falsables, métodos replicables, citas reales y verificables. "
                    "Nunca inventar resultados ni referencias; señalar supuestos y limitaciones.",
        strict_threshold=94,
    ),

    "tdr": DocType(
        key="tdr",
        name="TDR / Documento de compras públicas",
        description="Términos de Referencia y documentos legales de contratación pública (LOSNCP/SERCOP, Ecuador) o de organismos.",
        personas=[
            "Especialista legal en contratación pública (LOSNCP, RGLOSNCP, SERCOP)",
            "Experto técnico del objeto de contratación",
            "Analista de presupuesto y estructura de costos referenciales",
            "Redactor técnico-jurídico experto y editorialista profesional",
        ],
        sections=[
            "Antecedentes", "Objeto de la contratación", "Objetivos (general y específicos)",
            "Alcance del trabajo", "Especificaciones técnicas / Términos de referencia",
            "Productos y entregables", "Plazo de ejecución", "Perfil del proveedor/consultor y equipo",
            "Metodología requerida", "Presupuesto referencial", "Forma de pago",
            "Obligaciones de las partes", "Criterios de evaluación de ofertas",
            "Garantías", "Marco legal aplicable", "Anexos",
        ],
        evaluation_criteria=[
            "Solidez jurídica y cumplimiento de LOSNCP/normativa aplicable",
            "Precisión técnica de las especificaciones",
            "Claridad de entregables, plazos y forma de pago",
            "Coherencia del presupuesto referencial",
            "Objetividad y legalidad de los criterios de evaluación de ofertas",
        ],
        format=FormatSpec(font_name="Arial", font_size=11, line_spacing=1.5,
                          citation_style="", min_words=1500,
                          notes="Lenguaje jurídico-técnico, numeración de cláusulas, referencias legales exactas."),
        needs_budget_excel=True,
        rigor_notes="Documento legalmente blindado: citar artículos exactos de la normativa; "
                    "especificaciones no direccionadas; criterios de evaluación objetivos y medibles.",
        strict_threshold=92,
    ),

    "peer_review": DocType(
        key="peer_review",
        name="Revisión por pares (peer-review)",
        description="Revisión crítica de un artículo/documento según la rúbrica o parámetros de la organización.",
        personas=[
            "Revisor par experto (PhD) en el área del manuscrito",
            "Metodólogo evaluador",
            "Editor de revista indexada",
        ],
        sections=[
            "Resumen de la evaluación", "Recomendación editorial (aceptar/menor/mayor/rechazar)",
            "Evaluación por criterios de la rúbrica", "Comentarios mayores",
            "Comentarios menores", "Comentarios al editor (confidenciales)",
            "Verificación de referencias y ética", "Calificación final",
        ],
        evaluation_criteria=[
            "Cobertura de todos los criterios de la rúbrica establecida",
            "Justificación basada en evidencia del propio manuscrito",
            "Constructividad y accionabilidad de los comentarios",
            "Detección de fallos metodológicos, éticos o de citación",
            "Objetividad y tono profesional",
        ],
        format=FormatSpec(font_name="Times New Roman", font_size=11, line_spacing=1.5,
                          min_words=500,
                          notes="Comentarios numerados con referencia a página/línea del manuscrito."),
        rigor_notes="Revisión imparcial y reproducible: cada juicio anclado en el texto evaluado y en la rúbrica. "
                    "Si no se entrega rúbrica, usar criterios estándar del área y declararlo.",
        strict_threshold=92,
    ),

    "tesis": DocType(
        key="tesis",
        name="Tesis / Trabajo de titulación",
        description="Tesis de colegio, grado, maestría, doctorado o postdoctorado, con rigor académico del nivel correspondiente.",
        personas=[
            "Director de tesis (PhD) experto en el área",
            "Metodólogo de investigación",
            "Experto estadístico/analítico según el enfoque",
            "Editor académico y normalizador de citas",
        ],
        sections=[
            "Portada", "Resumen / Abstract", "Índice", "Introducción",
            "Planteamiento del problema", "Objetivos e hipótesis", "Marco teórico",
            "Estado del arte", "Metodología", "Resultados", "Discusión",
            "Conclusiones y recomendaciones", "Referencias", "Anexos",
        ],
        evaluation_criteria=[
            "Pertinencia y delimitación del problema",
            "Profundidad del marco teórico y estado del arte",
            "Rigor metodológico apropiado al nivel académico",
            "Análisis de resultados y discusión",
            "Originalidad y aporte (proporcional al nivel: colegio→postdoctorado)",
            "Normas de citación y referencias verificables",
            "Redacción académica, coherencia y estilo",
        ],
        format=FormatSpec(font_name="Times New Roman", font_size=12, line_spacing=1.5,
                          margin_left_cm=3.0, margin_right_cm=2.5, citation_style="APA 7",
                          min_words=8000,
                          notes="Estructura y exigencia ajustadas al nivel (colegio, grado, maestría, doctorado, postdoctorado). "
                                "min_words es un piso conservador para el nivel más bajo (colegio); niveles superiores "
                                "exigen bastante más extensión en la práctica, según rigor_notes."),
        rigor_notes="Nivel académico al máximo según el grado: a mayor nivel, mayor originalidad, profundidad teórica "
                    "y sofisticación metodológica. Citas reales y verificables; nunca inventar fuentes.",
        strict_threshold=93,
    ),

    "legal_tecnico": DocType(
        key="legal_tecnico",
        name="Documento legal o técnico",
        description="Informes técnicos, oficios, manuales, políticas, dictámenes u otros documentos legales/técnicos.",
        personas=[
            "Especialista legal/técnico del área correspondiente",
            "Redactor técnico profesional",
            "Revisor normativo",
        ],
        sections=[
            "Encabezado / datos del documento", "Antecedentes", "Base legal/normativa",
            "Análisis / desarrollo técnico", "Conclusiones", "Recomendaciones",
            "Firmas y responsables", "Anexos",
        ],
        evaluation_criteria=[
            "Exactitud técnica y legal del contenido",
            "Estructura y claridad apropiadas al tipo de documento",
            "Fundamentación normativa precisa",
            "Coherencia y trazabilidad de las conclusiones/recomendaciones",
        ],
        format=FormatSpec(font_name="Arial", font_size=11, line_spacing=1.5,
                          min_words=600,
                          notes="Estilo formal-institucional; numeración y referencias normativas exactas."),
        rigor_notes="Precisión absoluta en datos, fechas y referencias legales; tono institucional y profesional.",
    ),

    "generico": DocType(
        key="generico",
        name="Documento personalizado",
        description="Cualquier otro entregable; el formato y la estructura se infieren de los requisitos o la plantilla.",
        personas=[
            "Experto principal del área solicitada",
            "Redactor y editorialista profesional de alto nivel",
        ],
        sections=["Estructura según requisitos detectados o plantilla"],
        evaluation_criteria=[
            "Adecuación al propósito y a los requisitos",
            "Calidad técnica/profesional del contenido",
            "Claridad, coherencia y estilo",
        ],
        format=FormatSpec(),
        rigor_notes="Adapta perfil de experto, estructura y formato a lo que el caso exija, al más alto nivel.",
    ),
}

DEFAULT_DOC_TYPE = "generico"

# ─────────────────────────────────────────────────────────────────────────────
#  ESCALA POR NIVEL ACADÉMICO (hoy usada por doc_type_key="tesis")
# ─────────────────────────────────────────────────────────────────────────────
# Un trabajo de colegio y una tesis doctoral usaban el mismo min_words y los
# mismos evaluation_criteria pese a que la exigencia real debe escalar con el
# nivel. Esta tabla ajusta el piso de extensión y añade criterios de evaluación
# adicionales según el nivel académico real detectado en el brief.
LEVEL_REQUIREMENTS: dict = {
    "colegio":       {"min_words": 3000,  "extra_criteria": []},
    "pregrado":      {"min_words": 8000,  "extra_criteria": []},
    "maestria":      {"min_words": 15000, "extra_criteria": [
        "Aporte original demostrable frente al estado del arte",
    ]},
    "doctorado":     {"min_words": 25000, "extra_criteria": [
        "Aporte original demostrable frente al estado del arte",
        "Rigor metodológico con potencia estadística/analítica reportada",
    ]},
    "postdoctorado": {"min_words": 25000, "extra_criteria": [
        "Aporte original demostrable frente al estado del arte",
        "Rigor metodológico con potencia estadística/analítica reportada",
        "Contribución teórica/metodológica de frontera en el área",
    ]},
}


def level_requirements(academic_level: str) -> dict:
    """Devuelve {min_words, extra_criteria} para el nivel dado (default: pregrado)."""
    return LEVEL_REQUIREMENTS.get((academic_level or "").strip().lower(),
                                  LEVEL_REQUIREMENTS["pregrado"])


# ─────────────────────────────────────────────────────────────────────────────
#  MÓDULOS PRINCIPALES (capa de organización sobre los tipos de documento)
# ─────────────────────────────────────────────────────────────────────────────
MODULES: dict = {
    "investigacion": {
        "key": "investigacion",
        "name": "Investigación",
        "icon": "🔬",
        "description": "Producción científica y académica de alto nivel: artículos "
                       "indexados, tesis y revisión por pares.",
    },
    "proyectos": {
        "key": "proyectos",
        "name": "Proyectos",
        "icon": "📊",
        "description": "Financiamiento no reembolsable, contratación pública (TDR) "
                       "y documentos técnico-legales.",
    },
}

# Módulo de cada tipo. 'ambos' = aparece en los dos módulos.
_MODULE_OF = {
    "articulo_cientifico": "investigacion",
    "tesis": "investigacion",
    "peer_review": "investigacion",
    "propuesta": "proyectos",
    "tdr": "proyectos",
    "legal_tecnico": "proyectos",
    "generico": "ambos",
}
for _key, _dt in DOC_TYPES.items():
    _dt.module = _MODULE_OF.get(_key, "proyectos")


def get_doc_type(key: str) -> DocType:
    return DOC_TYPES.get((key or "").strip().lower(), DOC_TYPES[DEFAULT_DOC_TYPE])


def list_doc_types() -> list:
    return list(DOC_TYPES.values())


def list_modules() -> list:
    return list(MODULES.values())


def doc_types_for_module(module: str) -> list:
    """Tipos de un módulo (incluye los marcados como 'ambos')."""
    module = (module or "").strip().lower()
    return [d for d in DOC_TYPES.values()
            if d.module == module or d.module == "ambos"]


def all_criteria(doc_type: DocType) -> list:
    """Criterios completos que evaluará el revisor para este tipo (específicos + base)."""
    return list(doc_type.evaluation_criteria) + list(BASE_CRITERIA)
