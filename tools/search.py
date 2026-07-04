"""
MOTOR DE BÚSQUEDA PROFUNDA DE OPORTUNIDADES DE FINANCIAMIENTO
─────────────────────────────────────────────────────────────
Capas de búsqueda:
  1. web_search   — búsqueda de texto (DuckDuckGo) por query individual.
  2. news_search  — noticias recientes (convocatorias anunciadas).
  3. deep_search  — orquesta MÚLTIPLES queries + descarga el contenido real
                    de las páginas más relevantes (bases de convocatoria),
                    deduplica por dominio/URL y devuelve evidencia rica.
  4. fetch_page   — descarga y limpia el texto de una URL concreta para que el
                    agente lea los requisitos formales reales del financiador.
"""
import json
import re
import time
import html
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from config import (
    SEARCH_MAX_RESULTS, SEARCH_SAFE, SEARCH_MAX_QUERIES,
    SEARCH_FETCH_PAGES, SEARCH_FETCH_CHARS, SEARCH_TIMEOUT,
)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Dominios con alta señal de convocatorias reales (se priorizan al deduplicar)
HIGH_SIGNAL_DOMAINS = (
    # Multilaterales y bancos de desarrollo
    "iadb.org", "caf.com", "worldbank.org", "ifad.org", "thegef.org",
    "greenclimate.fund", "adb.org", "bcie.org", "fonplata.org",
    # Sistema ONU
    "undp.org", "un.org", "unicef.org", "paho.org", "fao.org", "unesco.org",
    "unfpa.org", "unwomen.org", "unhcr.org", "wfp.org", "ilo.org", "unep.org",
    # Unión Europea
    "europa.eu", "ec.europa.eu", "eeas.europa.eu",
    # Cooperación bilateral
    "giz.de", "usaid.gov", "aecid.es", "afd.fr", "jica.go.jp", "ukaid.gov.uk",
    "kfw-entwicklungsbank.de", "sida.se", "norad.no", "dfat.gov.au",
    "international.gc.ca", "global-affairs.canada.ca",
    # Fundaciones grandes
    "fordfoundation.org", "gatesfoundation.org", "rockefellerfoundation.org",
    "macfound.org", "open-society-foundations.org", "wellcome.org",
    "wkkf.org", "hewlett.org", "fundacionavina.org", "tinkerfoundation.org",
    # Plataformas/agregadores de convocatorias
    "reliefweb.int", "devex.com", "ungm.org", "grants.gov", "fundsforngos.org",
    "philanthropynewsdigest.org", "candid.org", "fundforpeace.org",
    "opportunitiesforafricans.com", "globalgiving.org",
    # Ecuador (gobierno + agencias)
    "gob.ec", "senescyt.gob.ec", "ambiente.gob.ec", "agenciaregulacion",
    "ministeriodegobierno.gob.ec", "iniap.gob.ec",
    # LinkedIn (posts y artículos públicos suelen tener convocatorias anunciadas)
    "linkedin.com/pulse", "linkedin.com/posts", "linkedin.com/company",
)


# ── Tool definitions for Claude API ────────────────────────────────────────
WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Búsqueda web puntual de convocatorias de financiamiento no reembolsable, "
        "programas de cooperación y proyectos similares. Usa una sola query. "
        "Para una exploración amplia y profunda usa 'deep_search' en su lugar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Consulta (es/en/fr/pt)."},
            "max_results": {"type": "integer", "default": 8},
        },
        "required": ["query"],
    },
}

DEEP_SEARCH_TOOL = {
    "name": "deep_search",
    "description": (
        "BÚSQUEDA PROFUNDA. Ejecuta varias queries a la vez, combina resultados de "
        "texto y noticias, DESCARGA el contenido real de las páginas más relevantes "
        "(bases de la convocatoria, formularios, requisitos) y devuelve evidencia "
        "consolidada y deduplicada. Úsalo como herramienta principal para investigar "
        "a fondo una oportunidad: pasa 4-10 queries en español e inglés."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lista de 4 a 10 consultas variadas (es/en/fr/pt).",
            },
            "fetch_pages": {
                "type": "integer",
                "description": "Cuántas páginas top descargar y leer (por defecto 6).",
                "default": SEARCH_FETCH_PAGES,
            },
        },
        "required": ["queries"],
    },
}

FETCH_PAGE_TOOL = {
    "name": "fetch_page",
    "description": (
        "Descarga y limpia el texto de una URL concreta (p. ej. la página de bases "
        "de una convocatoria) para leer requisitos formales, fechas, montos, criterios "
        "de elegibilidad y formato exigido. Úsalo cuando deep_search devuelva una URL "
        "prometedora que necesites leer a fondo."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL completa a descargar."},
        },
        "required": ["url"],
    },
}


# ── Low-level helpers ──────────────────────────────────────────────────────
def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)/?", url or "")
    return m.group(1).lower().replace("www.", "") if m else ""


def _is_high_signal(url: str) -> bool:
    d = _domain(url)
    return any(sig in d for sig in HIGH_SIGNAL_DOMAINS)


def _strip_html(raw: str, max_chars: int) -> str:
    """Convierte HTML en texto legible sin dependencias externas."""
    raw = re.sub(r"(?is)<script.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style.*?</style>", " ", raw)
    raw = re.sub(r"(?is)<noscript.*?</noscript>", " ", raw)
    raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
    # Saltos en bloques
    raw = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article)>", "\n", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + " […]"
    return text


# ── Search executors ───────────────────────────────────────────────────────
def execute_web_search(query: str, max_results: int = SEARCH_MAX_RESULTS) -> str:
    try:
        from ddgs import DDGS
        time.sleep(0.4)  # evitar rate limit
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results, safesearch=SEARCH_SAFE))
        if not results:
            return json.dumps({"error": "Sin resultados", "query": query}, ensure_ascii=False)
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)


def execute_news_search(query: str, max_results: int = 6) -> str:
    try:
        from ddgs import DDGS
        time.sleep(0.4)
        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=max_results))
        if not results:
            return json.dumps({"error": "Sin noticias", "query": query}, ensure_ascii=False)
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)


_PDF_MAX_PAGES = 15


def _extract_pdf_text(raw_bytes: bytes, max_chars: int) -> str | None:
    """Extrae texto de un PDF (bases de convocatoria/TDR casi siempre vienen así).
    Devuelve None si la extracción falla o no produce texto útil (p.ej. PDF escaneado)."""
    try:
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw_bytes))
        parts = []
        for page in reader.pages[:_PDF_MAX_PAGES]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts).strip()
        if len(text) < 200:  # probable PDF escaneado (imagen), sin capa de texto
            return None
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        if len(text) > max_chars:
            text = text[:max_chars] + " […]"
        return text
    except Exception:
        return None


def execute_fetch_page(url: str) -> str:
    try:
        req = Request(url, headers={"User-Agent": _UA, "Accept-Language": "es,en;q=0.8"})
        with urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            is_pdf = "application/pdf" in ctype or url.lower().split("?")[0].endswith(".pdf")
            if is_pdf:
                raw_bytes = resp.read(15_000_000)  # las bases oficiales suelen ser PDF pesados
                text = _extract_pdf_text(raw_bytes, SEARCH_FETCH_CHARS)
                if text is None:
                    return json.dumps(
                        {"url": url, "note": "PDF no extraíble (posible escaneo sin capa de texto)."},
                        ensure_ascii=False,
                    )
                return json.dumps({"url": url, "domain": _domain(url), "text": text,
                                   "source_format": "pdf"}, ensure_ascii=False)
            if "html" not in ctype and "text" not in ctype and ctype:
                return json.dumps(
                    {"url": url, "note": f"Contenido no textual ({ctype}). No descargado."},
                    ensure_ascii=False,
                )
            charset = "utf-8"
            m = re.search(r"charset=([\w-]+)", ctype)
            if m:
                charset = m.group(1)
            raw = resp.read(2_000_000).decode(charset, errors="replace")
        text = _strip_html(raw, SEARCH_FETCH_CHARS)
        return json.dumps({"url": url, "domain": _domain(url), "text": text}, ensure_ascii=False)
    except (HTTPError, URLError) as e:
        return json.dumps({"url": url, "error": f"No se pudo descargar: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"url": url, "error": str(e)}, ensure_ascii=False)


def execute_deep_search(queries: list, fetch_pages: int = SEARCH_FETCH_PAGES) -> str:
    """Orquesta queries EN PARALELO, descarga páginas top EN PARALELO y deduplica.

    Mejoras vs versión anterior:
    - Queries DuckDuckGo corren en paralelo (hasta 4 workers) en vez de secuencial.
    - Descarga de páginas corre en paralelo (hasta fetch_pages workers).
    - Ambos pasos eran secuenciales antes: el cambio recorta 60-80 % del tiempo de búsqueda.
    """
    if isinstance(queries, str):
        queries = [queries]
    queries = [q for q in queries if q and q.strip()][:SEARCH_MAX_QUERIES]

    seen_urls: set = set()
    seen_lock = threading.Lock()
    hits: list = []
    hits_lock = threading.Lock()

    def _search_one(q: str) -> None:
        local = []
        for kind, raw in (("web", execute_web_search(q, SEARCH_MAX_RESULTS)),
                          ("news", execute_news_search(q, 5))):
            try:
                items = json.loads(raw)
            except Exception:
                continue
            if isinstance(items, dict):
                continue
            for it in items:
                url = it.get("href") or it.get("url") or it.get("link") or ""
                if not url:
                    continue
                local.append({
                    "query": q, "kind": kind,
                    "title": it.get("title") or it.get("source") or "",
                    "url": url,
                    "snippet": (it.get("body") or it.get("excerpt") or
                                it.get("description") or "")[:400],
                    "date": it.get("date", ""),
                    "high_signal": _is_high_signal(url),
                })
        with hits_lock:
            for item in local:
                with seen_lock:
                    if item["url"] in seen_urls:
                        continue
                    seen_urls.add(item["url"])
                hits.append(item)

    # ── Fase 1: queries en paralelo (max 4 workers para no saturar DuckDuckGo) ──
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_search_one, queries))  # consume todos los futures

    # Priorizar alta señal y tipo web para descarga
    hits.sort(key=lambda h: (not h["high_signal"], h["kind"] != "web"))

    n_fetch = max(0, int(fetch_pages))
    # Candidatos: alta señal primero, luego resto — pool 3× para compensar 403s.
    # Muchos sitios institucionales (undp.org, gob.ec) bloquean bots con 403;
    # descargamos un pool amplio en paralelo y nos quedamos con los n_fetch primeros
    # que respondan con contenido real.
    pool_size = min(n_fetch * 3, len(hits))
    hs_hits = [h for h in hits if h["high_signal"]]
    other_hits = [h for h in hits if not h["high_signal"]]
    pool = (hs_hits + other_hits)[:pool_size]

    # ── Fase 2: descarga del pool en paralelo ─────────────────────────────────
    url_to_hit = {h["url"]: h for h in pool}

    def _fetch_one(url: str) -> tuple[str, str | None]:
        page = json.loads(execute_fetch_page(url))
        text = page.get("text") if "text" in page and len(page.get("text", "")) > 200 else None
        return url, text

    fetched = 0
    if pool_size > 0 and n_fetch > 0:
        with ThreadPoolExecutor(max_workers=min(pool_size, 10)) as ex:
            for url, text in ex.map(_fetch_one, list(url_to_hit)):
                if text and fetched < n_fetch:
                    url_to_hit[url]["page_content"] = text
                    fetched += 1

    summary = {
        "queries_ejecutadas": queries,
        "total_resultados_unicos": len(hits),
        "paginas_descargadas": fetched,
        "resultados": hits[:40],
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


# ── Registry exposed to the agent ──────────────────────────────────────────
TOOL_HANDLERS = {
    "web_search": execute_web_search,
    "deep_search": execute_deep_search,
    "fetch_page": execute_fetch_page,
}

ALL_TOOLS = [DEEP_SEARCH_TOOL, WEB_SEARCH_TOOL, FETCH_PAGE_TOOL]


# ── Pre-built deep query packs for opportunity hunting ─────────────────────
def opportunity_queries_by_category(topic: str) -> dict:
    """Genera el paquete de queries de búsqueda profunda, ETIQUETADO por categoría.

    Cubre: portales oficiales, multilaterales, bilaterales, ONU, UE, fundaciones,
    consultoría/OSC (perfil JOMAP/CMAJ), agregadores y LinkedIn.

    Se expone por categoría (en vez de una sola lista plana) para que quien
    trunque la lista a un máximo de queries (SEARCH_MAX_QUERIES) pueda tomar una
    muestra representativa de CADA categoría en vez de cortar en orden fijo —
    con el orden fijo anterior, cualquier corte por debajo de ~35 queries dejaba
    SIEMPRE fuera fundaciones y agregadores, sin importar el tema buscado.
    """
    t = topic.strip()
    year = "2025 2026 2027"
    cats: dict[str, list[str]] = {
        "portales": [
            f"convocatoria financiamiento no reembolsable Ecuador {t} {year}",
            f"fondos concursables {t} Ecuador organizaciones sociedad civil OSC {year}",
            f"grants non-refundable funding Ecuador {t} {year} open call NGO",
            f"call for proposals {t} Ecuador Latin America South America {year}",
            f"requisitos bases convocatoria {t} Ecuador elegibilidad presupuesto formulario",
        ],
        "multilateral": [
            f"BID IADB BID-LAB FOMIN convocatoria {t} Ecuador {year}",
            f"CAF cooperación técnica {t} Ecuador Azuay Cuenca {year}",
            f"Banco Mundial World Bank {t} Ecuador consultoría contrato {year}",
            f"GEF FMAM fondo global medio ambiente {t} Ecuador {year}",
            f"BCIE FONPLATA {t} Ecuador financiamiento {year}",
        ],
        "onu": [
            f"PNUD UNDP {t} Ecuador convocatoria sociedad civil consultoría {year}",
            f"UNICEF OPS UNFPA {t} Ecuador propuesta organizacion {year}",
            f"FAO OIT ILO UNESCO {t} Ecuador grant convocatoria {year}",
            f"ONU Mujeres UN Women {t} Ecuador organización género {year}",
        ],
        "ue_bilateral": [
            f"Unión Europea EU cooperación internacional {t} Ecuador grant {year}",
            f"GIZ cooperación alemana {t} Ecuador Azuay {year}",
            f"USAID {t} Ecuador convocatoria ONG {year}",
            f"AECID cooperación española {t} Ecuador {year}",
            f"COSUDE Suiza {t} Ecuador convocatoria {year}",
            f"AFD Agence Française Développement {t} Équateur {year}",
            f"JICA cooperación japonesa {t} Ecuador {year}",
        ],
        "fundaciones": [
            f"Gates Foundation Ford Foundation {t} Ecuador {year}",
            f"Fundación Avina Kellogg {t} Ecuador Latinoamérica {year}",
            f"Bloomberg Philanthropies {t} Ecuador municipalities cities {year}",
            f"Wellcome Trust {t} Ecuador Latin America grant {year}",
        ],
        "consultoria": [
            f"consultoría técnica arquitectura ingeniería Ecuador Azuay convocatoria {year}",
            f"licitación consultoría planificación urbana ordenamiento territorial Ecuador {year}",
            f"capacitación formación profesional Ecuador {t} financiamiento {year}",
            f"fortalecimiento capacidades organizaciones Ecuador {t} cooperación {year}",
        ],
        "osc": [
            f"convocatoria fundaciones organizaciones sin fines de lucro Ecuador {t} {year}",
            f"emprendimiento MIPYMES desarrollo empresarial Ecuador {t} fondos {year}",
            f"fortalecimiento organizaciones gremiales Ecuador {t} cooperacion internacional {year}",
        ],
        "agregadores": [
            f"site:reliefweb.int call for proposals {t} Ecuador",
            f"site:devex.com {t} Ecuador funding opportunity {year}",
            f"site:fundsforngos.org {t} Ecuador {year}",
            f"site:ungm.org {t} Ecuador RFP contract",
        ],
        "linkedin": [
            f"site:linkedin.com/posts {t} grant call proposals Ecuador {year}",
            f"site:linkedin.com/pulse {t} funding Ecuador convocatoria {year}",
            f"site:linkedin.com/posts convocatoria {t} Azuay Cuenca {year}",
        ],
        "tematicas": [],
    }

    tl = t.lower()
    if any(k in tl for k in ("ambient", "clima", "agua", "biodivers", "bosque", "carbono", "hidric")):
        cats["tematicas"] += [
            f"GEF GCF Fondo Verde Clima {t} Ecuador {year} propuesta",
            f"Green Climate Fund {t} Ecuador accreditated entity {year}",
        ]
    if any(k in tl for k in ("urban", "ciudad", "vivienda", "infraestructura", "territorio")):
        cats["tematicas"] += [
            f"ONU-Habitat UN-Habitat {t} Ecuador Cuenca {year}",
            f"BID ciudades sostenibles infraestructura {t} Ecuador {year}",
        ]
    if any(k in tl for k in ("género", "mujer", "mujeres", "equidad", "gender")):
        cats["tematicas"].append(f"ONU Mujeres UNFPA {t} Ecuador organización {year}")
    if any(k in tl for k in ("educac", "capacitac", "formac", "docente")):
        cats["tematicas"].append(f"UNESCO UNICEF educación capacitación {t} Ecuador {year}")

    return cats


def opportunity_queries(topic: str) -> list:
    """Devuelve el paquete de queries ENTRELAZADO (round-robin) por categoría.

    El entrelazado garantiza que un corte `[:N]` tomado desde el frente de esta
    lista (como hace execute_deep_search con SEARCH_MAX_QUERIES) incluya SIEMPRE
    una muestra de cada categoría — incluidas fundaciones y agregadores — en vez
    de agotar solo las primeras categorías del orden fijo original.
    """
    cats = opportunity_queries_by_category(topic)
    iterators = [iter(v) for v in cats.values() if v]
    out: list[str] = []
    while iterators:
        still_going = []
        for it in iterators:
            q = next(it, None)
            if q is not None:
                out.append(q)
                still_going.append(it)
        iterators = still_going
    # Dedup conservando el orden entrelazado
    seen, deduped = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped


def linkedin_queries(topic: str) -> list:
    """Pack específico de queries para LinkedIn. Útil cuando el agente quiere
    cazar anuncios recientes de program officers o páginas de fundaciones."""
    t = topic.strip()
    return [
        f"site:linkedin.com/posts {t} grant call proposals",
        f"site:linkedin.com/posts convocatoria {t}",
        f"site:linkedin.com/pulse {t} Ecuador funding",
        f"site:linkedin.com/company {t} foundation grant",
    ]


# Compatibilidad retro
OPPORTUNITY_QUERIES = opportunity_queries("desarrollo sostenible Ecuador")
