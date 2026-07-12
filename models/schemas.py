from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FunderInfo:
    name: str
    type: str           # multilateral | bilateral | UN | EU | fundacion | nacional
    url: str
    deadline: str
    amount_range: str
    language: str       # idioma requerido de la propuesta
    sector: str
    country_focus: str
    url_status: str = ""       # resultado de verificación HTTP: activo|acceso_restringido|url_muerta|no_responde
    deadline_iso: str = ""     # fecha de cierre en formato ISO YYYY-MM-DD (verificada fehacientemente)
    deadline_status: str = ""  # abierta|abierta_proxima|por_cerrar|cierra_hoy|cerrada|sin_fecha
    deadline_dias: Optional[int] = None  # días restantes (negativo si ya cerró)
    deadline_label: str = ""   # texto legible: "Abierta — cierra en 45 días" / "⚠️ Cierra en 5 días"


@dataclass
class EcuadorAlignment:
    legal_framework: str        # leyes/reglamentos aplicables
    plan_nacional: str          # alineación con Plan Nacional de Desarrollo
    ods_aligned: list           # ODS aplicables
    institutional_capacity: str # capacidad del proponente
    national_priority: bool     # es prioridad nacional


@dataclass
class AnalysisResult:
    viable: bool
    go_no_go: str               # "GO" | "NO-GO" | "CONDITIONAL"
    viability_score: float      # 0-100
    winning_probability: float  # 0-100

    # Opportunity info
    project_title: str
    funder: FunderInfo
    sector: str
    total_amount: str
    duration_months: int
    beneficiaries: str
    language: str               # idioma de la propuesta

    # Alignment
    ecuador_alignment: EcuadorAlignment

    # Assessment
    strengths: list
    risks: list
    critical_success_factors: list
    comparable_projects: list   # proyectos similares exitosos como referencia

    # For Agent 2
    format_requirements: dict   # estructura requerida por el financiador
    key_requirements: list      # requisitos indispensables
    differentiators: list       # qué hará que esta propuesta destaque
    recommendations: str        # instrucciones clave para el redactor

    raw_analysis: str           # análisis completo en texto libre

    # Lineamientos para la verificación 90/90
    national_guidelines: list = field(default_factory=list)       # lineamientos nacionales (Ecuador)
    international_guidelines: list = field(default_factory=list)   # lineamientos del financiador

    # Rigor: cada dato concreto (deadline, monto, eligibility) viene con URL/snippet probatorio
    # y desglose de feasibility por dimensión.
    evidence_sources: list = field(default_factory=list)
    feasibility_breakdown: dict = field(default_factory=dict)

    # ── Scouting / reporte ──────────────────────────────────────────────────
    summary: str = ""              # resumen ejecutivo para el reporte
    weighted_score: float = 0.0    # calificación ponderada 0-100 (config.weighted_score)
    # Lista RANKeada de oportunidades detectadas en una búsqueda (cada item es un
    # dict-oportunidad, índice 0 = la mejor). Vacía en sesiones de propuesta normales;
    # poblada en sesiones de scouting (se serializa dentro del jsonb `analysis`).
    alternatives: list = field(default_factory=list)

    @property
    def requires_excel(self) -> bool:
        return bool(self.format_requirements.get("requires_excel_budget", False))


@dataclass
class ReviewResult:
    approved: bool
    overall_score: float        # 0-100
    cycle: int

    # Puntajes por criterio (dinámicos según el tipo de documento): {label: score}
    criterion_scores: dict = field(default_factory=dict)
    # Verificación mecánica de formato: word_count, char_count, page_estimate, within_limits, issues[]
    format_check: dict = field(default_factory=dict)

    # Feedback
    strengths: list = field(default_factory=list)
    corrections: list = field(default_factory=list)       # correcciones específicas y accionables
    critical_issues: list = field(default_factory=list)   # problemas críticos que impiden aprobación
    compliance_checklist: list = field(default_factory=list)  # checklist de cumplimiento normativo verificado
    failing_elements: list = field(default_factory=list)  # criterios por debajo del umbral por elemento

    recommendation: str = ""    # texto resumen de la revisión

    def scores_by_criterion(self) -> dict:
        """Devuelve {nombre: puntaje} de todos los criterios evaluados."""
        return dict(self.criterion_scores)


@dataclass
class DocumentBrief:
    """Interfaz UNIVERSAL que consumen el Redactor y el Revisor, sin importar el tipo
    de documento. Se construye desde el Analista (propuestas) o el Clasificador (otros)."""
    doc_type_key: str
    title: str
    language: str = "es"
    personas: list = field(default_factory=list)          # perfiles de experto a adoptar
    sections: list = field(default_factory=list)          # secciones a producir
    format_spec: dict = field(default_factory=dict)       # FormatSpec serializado (tipo/letra/márgenes/límites)
    instructions: str = ""                                # "qué hay que hacer" — brief detallado
    national_guidelines: list = field(default_factory=list)
    international_guidelines: list = field(default_factory=list)
    key_requirements: list = field(default_factory=list)
    quality_markers: list = field(default_factory=list)   # diferenciadores / marcas de excelencia
    source_notes: str = ""                                # hallazgos de investigación / referencias reales
    needs_budget_excel: bool = False
    evaluation_criteria: list = field(default_factory=list)  # ejes que evaluará el revisor
    rigor_notes: str = ""
    raw: str = ""
    # Datos para hoja(s) de estadística en Excel (con fórmulas). {"datasets": [...]}.
    # Se llena tras aprobar el documento (agents/statistics.py); persiste en el jsonb brief.
    statistics: dict = field(default_factory=dict)

    # Nivel académico real (colegio|pregrado|maestria|doctorado|postdoctorado).
    # Hoy solo lo usa doc_type_key="tesis" para escalar min_words y evaluation_criteria
    # (ver models.doc_types.level_requirements) — un colegio y un doctorado no deben
    # juzgarse con la misma exigencia. Vacío = se asume "pregrado".
    academic_level: str = ""


@dataclass
class FinancialPackage:
    """Datos estructurados que alimentan el Excel y se reflejan en el Word.
    Garantiza que los cálculos del Excel y la narrativa del Word coincidan."""
    currency: str = "USD"
    # Presupuesto: lista de dicts con keys:
    #   categoria, actividad, unidad, cantidad, costo_unitario,
    #   fuente_solicitada, contraparte, anio (opcional)
    budget_items: list = field(default_factory=list)
    # Marco lógico: lista de dicts con keys:
    #   nivel (Impacto/Efecto/Producto/Actividad), resumen_narrativo,
    #   indicador, linea_base, meta, fuente_verificacion, supuestos
    logframe_rows: list = field(default_factory=list)
    # Cronograma: lista de dicts con keys: actividad, responsable, meses (list[int]), entregable
    schedule_rows: list = field(default_factory=list)
    # Metas/indicadores resumidos y notas de cofinanciamiento
    cofinancing_notes: str = ""
    budget_narrative: str = ""


@dataclass
class ProjectSession:
    session_id: str
    user_input: str
    input_mode: str             # "search" | "text" | "file"

    analysis: Optional[AnalysisResult] = None
    proposal_versions: list = field(default_factory=list)
    review_results: list = field(default_factory=list)
    current_cycle: int = 0
    final_proposal: Optional[str] = None
    approved: bool = False
    output_path: Optional[str] = None
    financial: Optional["FinancialPackage"] = None
    word_path: Optional[str] = None
    excel_path: Optional[str] = None

    # Trazabilidad de constructores: [{cycle, requested, used}] por ciclo de redacción
    builder_log: list = field(default_factory=list)

    # ── Flujo por fases con gates ≥90 y reinicio al inicio ──────────────────
    # Intento actual del ciclo completo (1..MAX_PIPELINE_RESTARTS).
    attempts: int = 0
    # Auditoría de los gates intermedios: [{attempt, phase, provider, score, passed, issues, strengths}]
    phase_reviews: list = field(default_factory=list)
    # Razón si se entrega inconcluso (no alcanzó 90 tras agotar los intentos).
    inconclusive_reason: str = ""

    # Dueño (usuario que lo creó). None = creado por la clave maestra/admin.
    owner_user_id: Optional[str] = None

    # Tipo de documento, formato y materiales de entrada
    doc_type_key: str = "propuesta"
    brief: Optional["DocumentBrief"] = None
    template_text: str = ""               # plantilla/modelo OPCIONAL a imitar (no obligatorio)
    support_docs: list = field(default_factory=list)  # [(nombre, texto)] documentos de apoyo
    # Análisis estructurado de documentos de entrada (plantilla + docs de apoyo + URLs).
    # Producido por agents/intake.py. Contiene: required_sections, format_overrides,
    # key_constraints, org_refs, url_contents, raw_notes.
    intake_data: dict = field(default_factory=dict)

    # True una vez Gate 1 aprobó la investigación. Se persiste para que, si el
    # usuario pausa y luego continúa, el pipeline no repita la búsqueda web ya
    # aprobada (ver core/pipeline.py: research_approved).
    research_approved: bool = False

    # Checkpoints de grano fino: True cuando el ciclo vigente ya superó Gate 2
    # (redacción) o Gate 3 (paquete). Al reanudar una sesión que murió DESPUÉS de
    # aprobarlos, se salta la redacción multipasada y los gates ya pasados. Se
    # INVALIDAN (→ False) cuando Fase 4 rechaza y fuerza reinicio, porque el
    # borrador aprobado deja de ser válido (ver core/pipeline.py).
    gate2_passed: bool = False
    gate3_passed: bool = False

    # Observaciones NO críticas de gates que igual APROBARON (score ≥ umbral pero con
    # "issues"). Antes se perdían para siempre — solo se capturaba feedback cuando un
    # gate rechazaba. Se inyectan como contexto advisorio (no correcciones forzosas)
    # en el siguiente prompt de redacción/veredicto.
    quality_notes: list = field(default_factory=list)
