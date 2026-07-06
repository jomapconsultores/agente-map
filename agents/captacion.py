"""
AGENTE CAPTACIÓN ACTIVA — Búsqueda de mercado por producto/servicio
────────────────────────────────────────────────────────────────────
Dado un producto o servicio, busca prospectos potenciales en directorios
y fuentes PÚBLICAS, expandiendo desde Cuenca hacia el resto del Ecuador.

CUMPLIMIENTO LOPDP (Ley Orgánica de Protección de Datos Personales, RO 459,
26-may-2021, vigente desde 2023):
  - Solo se usan datos de acceso público (directorios empresariales, cámaras,
    Supercias, páginas web corporativas, Google Maps Business).
  - NO se extraen datos de perfiles personales de redes sociales.
  - Cada prospecto incluye la fuente pública de origen.
  - El sistema informa que los datos son de fuentes públicas y pueden
    eliminarse a solicitud del titular (derecho ARSOP).
  - Se excluyen categorías sensibles (salud, religión, política, etc.).
"""
from __future__ import annotations

import concurrent.futures
import json
import config
from agents import llm
from tools.search import execute_web_search, execute_fetch_page

# ── Zonas geográficas en orden de expansión desde Cuenca ─────────────────────
ZONAS_ECUADOR = [
    {"zona": "Cuenca", "provincia": "Azuay", "nivel": 1},
    {"zona": "Azuay (excl. Cuenca)", "provincia": "Azuay", "nivel": 2},
    {"zona": "Cañar", "provincia": "Cañar", "nivel": 3},
    {"zona": "Loja", "provincia": "Loja", "nivel": 3},
    {"zona": "El Oro", "provincia": "El Oro", "nivel": 4},
    {"zona": "Morona Santiago", "provincia": "Morona Santiago", "nivel": 4},
    {"zona": "Zamora Chinchipe", "provincia": "Zamora Chinchipe", "nivel": 4},
    {"zona": "Chimborazo", "provincia": "Chimborazo", "nivel": 5},
    {"zona": "Tungurahua", "provincia": "Tungurahua", "nivel": 5},
    {"zona": "Guayas", "provincia": "Guayas", "nivel": 6},
    {"zona": "Manabí", "provincia": "Manabí", "nivel": 6},
    {"zona": "Pichincha", "provincia": "Pichincha", "nivel": 7},
    {"zona": "Resto del Ecuador", "provincia": "Nacional", "nivel": 8},
]

_SYSTEM = """
Eres un especialista en prospección comercial B2B y B2C para Ecuador.
Extraes de los resultados de búsqueda SOLO información de fuentes PÚBLICAS
(directorios empresariales, cámaras de comercio, páginas web corporativas,
Google Maps Business, Supercias). NUNCA extraes datos de perfiles personales
de redes sociales ni datos sensibles. Respondes SOLO con JSON válido.
"""


def _queries_para_zona(producto: str, zona: str) -> list[str]:
    """Genera queries de búsqueda orientados a directorios públicos."""
    base = f'"{zona}" Ecuador'
    return [
        f'empresas {producto} {base} directorio',
        f'proveedores {producto} {base} site:paginasamarillas.com.ec OR site:supercias.gob.ec',
        f'{producto} {base} "contacto" OR "teléfono" OR "correo"',
        f'negocios {producto} {zona} Ecuador site:google.com/maps OR site:infonegocios.ec',
    ]


def _extract_prospectos(raw_results: str, producto: str, zona: str, api_key: str | None) -> list[dict]:
    """Usa el LLM para extraer prospectos estructurados de los resultados de búsqueda."""
    prompt = f"""
Producto/servicio a promocionar: {producto}
Zona de búsqueda: {zona}, Ecuador

De los siguientes resultados de búsqueda web, extrae únicamente empresas o negocios
que podrían ser clientes potenciales para "{producto}". SOLO datos de fuentes públicas.

RESULTADOS:
{raw_results[:6000]}

Devuelve SOLO este JSON (sin texto extra). Si no hay prospectos claros: {{"prospectos": []}}

{{
  "prospectos": [
    {{
      "nombre": "<nombre de la empresa o negocio>",
      "sector": "<sector o tipo de negocio>",
      "zona": "{zona}",
      "contacto_web": "<URL del sitio web si aparece>",
      "contacto_email": "<email público si aparece, si no: ''>",
      "contacto_telefono": "<teléfono público si aparece, si no: ''>",
      "fuente_publica": "<nombre del directorio o sitio donde se encontró>",
      "fuente_url": "<URL de la fuente>",
      "relevancia": <número 1-10 qué tan relevante es como prospecto>,
      "razon": "<por qué sería cliente potencial de este producto>"
    }}
  ]
}}

RESTRICCIONES IMPORTANTES:
- Solo empresas/negocios, NO personas naturales en redes sociales
- Solo datos visibles públicamente en directorios o webs corporativas
- No inventar datos de contacto que no aparezcan en los resultados
"""
    try:
        raw = llm.complete(
            config.ROLE_CLASSIFIER,
            system=_SYSTEM,
            prompt=prompt,
            max_tokens=3000,
            temperature=0.2,
        )
        from utils.json_utils import robust_json_loads
        data = robust_json_loads(raw)
        return data.get("prospectos") or []
    except Exception:
        return []


def buscar_mercado(
    producto: str,
    zonas_solicitadas: list[int] | None = None,
    max_por_zona: int = 8,
    api_key: str | None = None,
) -> dict:
    """
    Busca prospectos para el producto dado, expandiendo desde Cuenca hacia Ecuador.

    Args:
        producto: descripción del producto o servicio a promocionar
        zonas_solicitadas: niveles 1-8 a buscar (None = solo nivel 1 y 2, Cuenca+Azuay)
        max_por_zona: máximo de prospectos a extraer por zona
        api_key: clave Anthropic opcional

    Returns:
        {
          "producto": str,
          "total": int,
          "zonas_buscadas": [...],
          "prospectos": [...],
          "aviso_lopdp": str,
        }
    """
    if zonas_solicitadas is None:
        zonas_solicitadas = [1, 2]  # por defecto: solo Cuenca y Azuay

    zonas_activas = [z for z in ZONAS_ECUADOR if z["nivel"] in zonas_solicitadas]
    todos_prospectos: list[dict] = []
    zonas_buscadas: list[str] = [z["zona"] for z in zonas_activas]

    def _buscar_zona(zona_info: dict) -> tuple[dict, list[dict]]:
        zona = zona_info["zona"]
        queries = _queries_para_zona(producto, zona)

        # Ejecutar las primeras 2 queries por zona (no sobrecargar)
        raw_results_parts = []
        for q in queries[:2]:
            try:
                res = execute_web_search(q, max_results=6)
                raw_results_parts.append(res)
            except Exception:
                pass

        if not raw_results_parts:
            return zona_info, []

        raw_combined = "\n\n---\n\n".join(raw_results_parts)
        prospectos = _extract_prospectos(raw_combined, producto, zona, api_key)
        return zona_info, prospectos

    if zonas_activas:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(6, max(1, len(zonas_activas)))
        ) as executor:
            resultados_por_zona = list(executor.map(_buscar_zona, zonas_activas))
    else:
        resultados_por_zona = []

    # Normalizar y limitar (mismo orden de zonas que antes, ya que executor.map
    # preserva el orden de zonas_activas)
    seen = set()
    for zona_info, prospectos in resultados_por_zona:
        for p in prospectos:
            key = (p.get("nombre") or "").lower().strip()
            if key and key not in seen and len(todos_prospectos) < max_por_zona * len(zonas_activas):
                seen.add(key)
                p["zona_nivel"] = zona_info["nivel"]
                p["provincia"] = zona_info["provincia"]
                todos_prospectos.append(p)

    # Ordenar por relevancia descendente
    todos_prospectos.sort(key=lambda x: float(x.get("relevancia", 0) or 0), reverse=True)

    return {
        "producto": producto,
        "total": len(todos_prospectos),
        "zonas_buscadas": zonas_buscadas,
        "prospectos": todos_prospectos,
        "aviso_lopdp": (
            "Información obtenida exclusivamente de directorios y fuentes de acceso público "
            "(Supercias, Cámaras de Comercio, directorios empresariales, sitios web corporativos). "
            "Conforme a la Ley Orgánica de Protección de Datos Personales del Ecuador (RO 459, 2021), "
            "el titular tiene derecho a solicitar la rectificación o eliminación de sus datos "
            "contactando a jomapconsultores@gmail.com."
        ),
    }
