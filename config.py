import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ── API ────────────────────────────────────────────────────────────────────
# Revisor / Analista / Clasificador → Anthropic (usan tool-use de búsqueda web).
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ── Constructores no-Claude (redactor + financiero) ─────────────────────────
# Mistral, Codestral y DeepSeek exponen una API compatible con OpenAI
# (chat/completions), por lo que comparten cliente HTTP en agents/llm.py.
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
CODESTRAL_API_KEY = os.getenv("CODESTRAL_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-large-latest")
CODESTRAL_MODEL = os.getenv("CODESTRAL_MODEL", "codestral-latest")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

MISTRAL_BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
CODESTRAL_BASE_URL = os.getenv("CODESTRAL_BASE_URL", "https://codestral.mistral.ai/v1")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

LLM_TIMEOUT_SEC = float(os.getenv("LLM_TIMEOUT_SEC", "600"))

# Rotación de constructores por ciclo de redacción:
#   ciclo 1 → Mistral, ciclo 2 → Codestral, ciclo 3 → DeepSeek, ciclo 4 → Mistral…
# El corrector (revisor) SIEMPRE es Claude.
BUILDER_ROTATION = [
    p.strip() for p in os.getenv(
        "BUILDER_ROTATION", "mistral,codestral,deepseek"
    ).split(",") if p.strip()
]


def builder_for_cycle(cycle: int) -> str:
    """Devuelve el proveedor constructor que toca en este ciclo (1-indexed)."""
    if not BUILDER_ROTATION:
        return "anthropic"
    return BUILDER_ROTATION[(max(1, cycle) - 1) % len(BUILDER_ROTATION)]

# ── Asignación de IA por ROL (flujo por fases) ───────────────────────────────
# Cada fase la produce una IA y la revisa OTRA distinta. Si un gate da <90, el
# pipeline vuelve al inicio (reinvestiga). Todo es configurable por entorno.
#
#   FASE 0   Clasificación del tipo de doc  → Mistral
#   FASE 1   Investigación web + análisis   → Mistral    (revisa Codestral)
#   FASE 2   Redacción del documento        → Codestral  (revisa Mistral)
#   FASE 3   Estructuración financiera      → Codestral
#   FASE 3.5 Revisión completa del paquete  → DeepSeek   (si no pasa → reinicia)
#   FASE 4   Veredicto final 90/90          → Claude     (si no aprueba → reinicia)
#
#   Fallback automático (complete_builder): si el proveedor primario falla,
#   prueba el siguiente en BUILDER_ROTATION → DeepSeek es la red de seguridad.
ROLE_CLASSIFIER      = os.getenv("ROLE_CLASSIFIER",      "mistral")
ROLE_RESEARCH        = os.getenv("ROLE_RESEARCH",        "mistral")
ROLE_REVIEW_RESEARCH = os.getenv("ROLE_REVIEW_RESEARCH", "codestral")
ROLE_WRITER          = os.getenv("ROLE_WRITER",          "codestral")
ROLE_REVIEW_WRITER   = os.getenv("ROLE_REVIEW_WRITER",   "mistral")
ROLE_FINANCIAL       = os.getenv("ROLE_FINANCIAL",       "codestral")
ROLE_PACKAGE_REVIEW  = os.getenv("ROLE_PACKAGE_REVIEW",  "deepseek")

# Directorio con documentos organizacionales (empresas, CVs, estatutos…).
# Se carga automáticamente como contexto para investigador y redactor.
EMPRESAS_DIR = BASE_DIR / "Empresas"

# Reinicios del ciclo completo cuando un gate reprueba (<90). Tras agotarlos se
# entrega la mejor versión lograda marcada como inconclusa.
MAX_PIPELINE_RESTARTS = int(os.getenv("MAX_PIPELINE_RESTARTS", "5"))
# Umbral mínimo (0-100) que debe alcanzar cada fase en su gate intermedio.
PHASE_REVIEW_THRESHOLD = int(os.getenv("PHASE_REVIEW_THRESHOLD", "90"))

# ── Scouting de oportunidades (búsqueda → reporte con calificación ponderada) ─
# "Buscar proyectos" detecta varias convocatorias y entrega un reporte por cada
# una (resumen + datos + calificación ponderada). Luego el usuario elige una o
# varias y se generan las propuestas completas automáticamente.
SCOUT_TOP_N = int(os.getenv("SCOUT_TOP_N", "5"))

# Pesos de la CALIFICACIÓN PONDERADA (0-100). Deben sumar 1.0; configurable por
# entorno (coma-separado: funder,geo,inst,budget,deadline,winprob).
def _parse_weights() -> dict:
    raw = os.getenv("SCORE_WEIGHTS", "")
    keys = ["funder_match", "geographic_eligibility", "institutional_fit",
            "budget_fit", "deadline_feasibility", "winning_probability"]
    defaults = [0.25, 0.15, 0.20, 0.15, 0.10, 0.15]
    if raw:
        try:
            vals = [float(x) for x in raw.split(",")]
            if len(vals) == len(keys) and abs(sum(vals) - 1.0) < 0.01:
                return dict(zip(keys, vals))
        except ValueError:
            pass
    return dict(zip(keys, defaults))


SCORE_WEIGHTS = _parse_weights()


def weighted_score(feasibility_breakdown: dict, winning_probability: float) -> float:
    """Calificación ponderada 0-100 a partir del desglose de viabilidad por dimensión
    y la probabilidad de ganar. Los pesos están en SCORE_WEIGHTS (suman 1.0)."""
    fb = feasibility_breakdown or {}

    def _dim(name: str) -> float:
        d = fb.get(name) or {}
        try:
            return float(d.get("score", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    w = SCORE_WEIGHTS
    total = (
        w["funder_match"] * _dim("funder_match")
        + w["geographic_eligibility"] * _dim("geographic_eligibility")
        + w["institutional_fit"] * _dim("institutional_fit")
        + w["budget_fit"] * _dim("budget_fit")
        + w["deadline_feasibility"] * _dim("deadline_feasibility")
        + w["winning_probability"] * float(winning_probability or 0)
    )
    return round(total, 1)

# ── Auth multiusuario ───────────────────────────────────────────────────────
# Secreto para firmar tokens de sesión. Si no se define, cae en AGENTE_MAP_API_KEY.
AUTH_SECRET = os.getenv("AUTH_SECRET", "") or os.getenv("AGENTE_MAP_API_KEY", "")
AUTH_TOKEN_TTL_HOURS = int(os.getenv("AUTH_TOKEN_TTL_HOURS", "168"))  # 7 días
# La X-API-Key maestra (AGENTE_MAP_API_KEY) entra como administrador (bootstrap).

# ── Supabase ───────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
SUPABASE_SERVICE_ROLE_JWT = os.getenv("SUPABASE_SERVICE_ROLE_JWT", "")

MAX_TOKENS_ANALYST = 16000   # 8000→14000→16000: análisis exhaustivos sin truncar
MAX_TOKENS_WRITER = 16000    # límite de salida del redactor; subir solo si tu proveedor
                             # (Mistral/Codestral/DeepSeek) admite más tokens de completion
MAX_TOKENS_REVIEWER = 10000  # 8000→10000: revisiones más detalladas y accionables
MAX_TOKENS_FINANCIAL = 8000

# Autocrítica interna del redactor: antes de exponer el borrador al gate externo, el
# propio redactor lo audita contra quality_markers/evaluation_criteria y reescribe
# lo débil — para los tipos de mayor exigencia, donde el costo de una llamada extra
# está claramente justificado por la calidad ganada.
WRITER_SELF_REVIEW = os.getenv("WRITER_SELF_REVIEW", "true").lower() in ("1", "true", "yes")
WRITER_SELF_REVIEW_DOC_TYPES = {"tesis", "articulo_cientifico", "tdr"}

# Generación MULTI-PASADA: MAX_TOKENS_WRITER (16000) rinde en español unas
# 9000-11000 palabras como techo duro POR LLAMADA — una tesis doctoral real (25-45k
# palabras) es matemáticamente imposible de producir sin truncar en una sola llamada.
# Por encima de este umbral, writer.py arma primero un plan sección-por-sección y
# redacta cada sección con su propio presupuesto de tokens completo.
MULTIPASS_MIN_WORDS = int(os.getenv("MULTIPASS_MIN_WORDS", "6000"))

# Consenso de 2 revisores independientes: el veredicto final hoy es una sola
# llamada a un único modelo (Claude). Ningún jurado doctoral ni comité editorial
# real aprueba con un solo árbitro — para los tipos de mayor riesgo reputacional,
# un segundo modelo distinto (proveedor no-Claude) evalúa de forma independiente y
# solo se aprueba si AMBOS lo hacen.
SECOND_OPINION_DOC_TYPES = {"tesis", "articulo_cientifico", "peer_review", "tdr"}
SECOND_OPINION_PROVIDER = os.getenv("SECOND_OPINION_PROVIDER", "deepseek")

# ── Pipeline ───────────────────────────────────────────────────────────────
MAX_REVIEW_CYCLES = 5
# Subciclos de consenso del equipo constructor (1°,2°,3°) antes de pasar a Claude.
# En cada subciclo los 3 revisan; si hay faltantes, el redactor mejora y se revisa otra vez.
MAX_PEER_SUBCYCLES = int(os.getenv("MAX_PEER_SUBCYCLES", "3"))
# Verificación exigente: se requiere ≥90 en CADA elemento Y ≥90 global.
APPROVAL_THRESHOLD = 90       # Score global mínimo para aprobar propuesta (0-100)
ELEMENT_THRESHOLD = 90        # Score mínimo EXIGIDO en cada criterio individual (0-100)
VIABILITY_THRESHOLD = 55      # Score mínimo de viabilidad para continuar (0-100)

# ── Search ─────────────────────────────────────────────────────────────────
# Búsqueda muy exhaustiva por defecto: más queries, más páginas leídas y más
# texto por página = más evidencia real para investigar a fondo. Todo paralelo,
# así que el impacto en tiempo es moderado. Ajustable por entorno si se necesita
# priorizar velocidad sobre profundidad.
SEARCH_MAX_RESULTS = 15       # 12→15: resultados por query individual
SEARCH_SAFE = "moderate"
SEARCH_MAX_QUERIES = 32       # 20→32: nº máximo de queries en una búsqueda profunda.
                               # opportunity_queries() ahora entrelaza categorías (round-robin)
                               # antes de este corte, así que subirlo SÍ amplía cobertura real
                               # (antes del entrelazado, cortar en 20 sobre ~40 queries en orden
                               # fijo dejaba fuera SIEMPRE fundaciones/agregadores/LinkedIn).
SEARCH_FETCH_PAGES = 14       # 10→14: páginas reales descargadas y leídas (descarga paralela)
SEARCH_FETCH_CHARS = 14000    # 10000→14000: lee requisitos completos del financiador
SEARCH_TIMEOUT = 12           # timeout (s) al descargar una página

# ── Documentos de salida ─────────────────────────────────────────────────────
GENERATE_WORD = True          # genera la propuesta final en .docx
GENERATE_EXCEL = True         # genera cálculos (presupuesto/marco lógico) en .xlsx

# ── Major funders tracked ──────────────────────────────────────────────────
FUNDERS = [
    # Multilateral
    "BID", "CAF", "Banco Mundial", "BCIE", "FIDA", "FONPLATA",
    # UN System
    "PNUD", "UNICEF", "OPS", "FAO", "UNESCO", "PNUMA", "ONU Mujeres",
    "UNFPA", "ACNUR", "PMA",
    # Bilateral
    "USAID", "GIZ", "AECID", "JICA", "AFD", "COSUDE", "UKAID",
    "Países Bajos", "Canadá IDRC", "Korea KOICA",
    # EU
    "Unión Europea", "Horizon Europe", "DEVCO", "DG CLIMA",
    # Foundations
    "Gates Foundation", "Ford Foundation", "Avina", "Kellogg",
    "Wellcome Trust", "Bloomberg Philanthropies",
    # Ecuador
    "SENESCYT", "INIAP", "MAG Ecuador", "MAATE",
    # Regional
    "OTCA", "IUCN", "IICA",
]
