"""
Verificación fehaciente de URLs: comprueba que cada enlace citado como fuente
responde realmente con HTTP 200 antes de marcarlo como 'verificado'.

Reglas:
  - HEAD request con timeout corto (8 s) para no bloquear el pipeline.
  - Si HEAD falla o devuelve 4xx, intenta GET (algunos servidores bloquean HEAD).
  - Resultado:
      "activo"      → 2xx o 3xx (redirección válida)
      "no_responde" → timeout, error de red, DNS falla
      "url_muerta"  → 4xx (404, 410, etc.)
      "acceso_restringido" → 401, 403, 429 (existe pero requiere auth/pago)
  - Nunca lanza excepción: si algo falla, devuelve "no_responde".
  - Respeta robots.txt implícitamente usando un User-Agent descriptivo.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

_TIMEOUT = 8
_MAX_PARALLEL = 6
_UA = "Mozilla/5.0 (compatible; AgenteMAP-URLVerifier/1.0; +https://jomapconsultores.com)"
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _normalize_url(url: str) -> Optional[str]:
    """Intenta llevar una URL sin esquema ('www.x.com/y') a una forma verificable
    ('https://www.x.com/y'). Devuelve None si no es una URL web normalizable
    (otro esquema como 'doi:'/'mailto:', o texto sin forma de dominio)."""
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if _SCHEME_RE.match(url):
        return None  # otro esquema (doi:, mailto:, isbn:, ...): no es una URL web
    if "." in url and " " not in url:
        return "https://" + url
    return None


def _check_one(url: str) -> str:
    """Devuelve el estado de una URL."""
    if not url or not url.startswith("http"):
        return "url_invalida"
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
        if code in (405, 501):
            # HEAD no soportado: intentar GET con range para no descargar todo
            try:
                req2 = urllib.request.Request(
                    url, headers={"User-Agent": _UA, "Range": "bytes=0-0"})
                with urllib.request.urlopen(req2, timeout=_TIMEOUT) as r:
                    code = r.status
            except urllib.error.HTTPError as e2:
                code = e2.code
            except Exception:
                return "no_responde"
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return "no_responde"
    except Exception:
        return "no_responde"

    if 200 <= code < 400:
        return "activo"
    if code in (401, 403, 429):
        return "acceso_restringido"
    if 400 <= code < 500:
        return "url_muerta"
    return "no_responde"


def verify_urls(urls: list[str]) -> dict[str, str]:
    """Verifica en paralelo una lista de URLs. Devuelve {url: estado}."""
    if not urls:
        return {}
    unique = list(dict.fromkeys(u for u in urls if u))
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=_MAX_PARALLEL) as pool:
        futures = {pool.submit(_check_one, u): u for u in unique}
        for f in as_completed(futures):
            u = futures[f]
            try:
                results[u] = f.result()
            except Exception:
                results[u] = "no_responde"
    return results


def enrich_evidence_sources(evidence_sources: list[dict]) -> list[dict]:
    """
    Recorre la lista de evidence_sources, verifica cada source_url en paralelo
    y actualiza el campo 'verification' con el resultado real:
      - activo             → "verificado"
      - acceso_restringido → "restringido"  (existe pero no es de libre acceso)
      - url_muerta         → "url_muerta"
      - no_responde        → "no_responde"
      - URL no vacía pero no normalizable como URL web → "no_verificable"
      - vacía → deja el valor que tenía el LLM (no hay nada que verificar)

    Nunca hereda silenciosamente el "verification" autoreportado por el LLM para una
    URL no vacía: o se verifica de verdad, o se marca explícitamente como no verificable.
    """
    if not evidence_sources:
        return evidence_sources

    urls = [s.get("source_url") or "" for s in evidence_sources]
    normalized = {u: _normalize_url(u) for u in urls if u}
    url_states = verify_urls([nu for nu in normalized.values() if nu])

    enriched = []
    for s in evidence_sources:
        s = dict(s)
        url = s.get("source_url") or ""
        norm = normalized.get(url)
        state = url_states.get(norm) if norm else None
        if state == "activo":
            s["verification"] = "verificado"
            s["url_status"] = "activo"
        elif state == "acceso_restringido":
            s["verification"] = "restringido"
            s["url_status"] = "acceso_restringido"
        elif state == "url_muerta":
            s["verification"] = "url_muerta"
            s["url_status"] = "url_muerta"
        elif state == "no_responde":
            s["verification"] = "no_responde"
            s["url_status"] = "no_responde"
        elif url:
            # URL no vacía pero no normalizable/verificable: nunca confiar en la
            # autoevaluación del LLM que generó el propio contenido.
            s["verification"] = "no_verificable"
            s["url_status"] = "no_verificable"
        # url vacía: dejamos el valor original del LLM
        enriched.append(s)
    return enriched


def verify_single(url: str) -> str:
    """Verifica una URL individual. Útil para el funder_url."""
    return _check_one(url) if url else "url_invalida"
