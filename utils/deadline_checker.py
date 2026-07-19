"""
Verificación fehaciente de la fecha de cierre de una convocatoria.

Estrategia:
1. Parsear la fecha que trajo el LLM (texto libre → datetime).
2. Intentar buscar en la web la fecha oficial actualizada desde la URL de la convocatoria.
3. Calcular días restantes y estado: abierta / por_cerrar / cerrada / sin_fecha.

No lanza excepciones: todos los fallos devuelven estado degradado.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional


# ── Patrones de fecha ──────────────────────────────────────────────────────────
_MESES_ES = {
    "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
    "julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
    # Nombres completos en inglés (no solo abreviaturas de 3 letras): sin esto,
    # "March 31, 2026" — el propio ejemplo del docstring de _MESDY_RE — no se reconocía.
    "january":1,"february":2,"march":3,"april":4,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

_ISO_RE   = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_DMY_RE   = re.compile(r"(\d{1,2})[-/\s](\d{1,2})[-/\s](\d{2,4})")
_DMES_RE  = re.compile(
    r"(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)\s+(?:de\s+)?(\d{4})", re.IGNORECASE)
_MESDY_RE = re.compile(
    r"([a-záéíóúñ]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.IGNORECASE)


def parse_deadline_text(text: str) -> Optional[date]:
    """Intenta convertir texto de fecha en un objeto date. Devuelve None si falla."""
    if not text:
        return None
    t = text.strip()

    # ISO: 2026-03-31
    m = _ISO_RE.search(t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass

    # DD/MM/YYYY o DD-MM-YYYY
    m = _DMY_RE.search(t)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d)
        except ValueError:
            pass

    # "31 de marzo de 2026" / "31 marzo 2026"
    m = _DMES_RE.search(t)
    if m:
        mes = _MESES_ES.get(m[2].lower())
        if mes:
            try:
                return date(int(m[3]), mes, int(m[1]))
            except ValueError:
                pass

    # "March 31, 2026"
    m = _MESDY_RE.search(t)
    if m:
        mes = _MESES_ES.get(m[1].lower())
        if mes:
            try:
                return date(int(m[3]), mes, int(m[2]))
            except ValueError:
                pass

    return None


def deadline_status(dl: Optional[date]) -> dict:
    """Calcula el estado de la convocatoria a partir de la fecha de cierre."""
    today = date.today()
    if dl is None:
        return {"status": "sin_fecha", "dias_restantes": None,
                "label": "Fecha de cierre no determinada", "color": "muted"}
    dias = (dl - today).days
    if dias < 0:
        return {"status": "cerrada", "dias_restantes": dias,
                "label": f"Cerrada hace {-dias} día{'s' if -dias!=1 else ''}",
                "color": "err"}
    if dias == 0:
        return {"status": "cierra_hoy", "dias_restantes": 0,
                "label": "¡Cierra HOY!", "color": "err"}
    if dias <= 14:
        return {"status": "por_cerrar", "dias_restantes": dias,
                "label": f"⚠️ Cierra en {dias} día{'s' if dias!=1 else ''}",
                "color": "warn"}
    if dias <= 60:
        return {"status": "abierta_proxima", "dias_restantes": dias,
                "label": f"Abierta — cierra en {dias} días",
                "color": "ok"}
    return {"status": "abierta", "dias_restantes": dias,
            "label": f"Abierta — cierra en {dias} días ({dl.strftime('%d/%m/%Y')})",
            "color": "ok"}


def _search_deadline_in_page(url: str) -> Optional[date]:
    """Descarga la página de la convocatoria y busca la fecha de cierre."""
    if not url or not url.startswith("http"):
        return None
    try:
        import json
        from tools.search import execute_fetch_page
        raw = execute_fetch_page(url)
        if not raw:
            return None
        # execute_fetch_page SIEMPRE devuelve un JSON serializado
        # ({"url","domain","text"} o {"url","error"/"note"}), nunca el texto crudo.
        # Antes se escaneaba ese JSON como si fuera el texto de la página (con url/
        # domain al frente y los saltos de línea escapados como '\n' literales), lo
        # que degradaba la extracción de la fecha. Se parsea y se usa el campo 'text'.
        try:
            page = json.loads(raw)
        except (ValueError, TypeError):
            return None
        content = page.get("text") or ""
        if not content:
            return None  # respuesta de error/note (PDF no extraíble, 403…): sin texto

        # Buscar patrones de fecha cerca de palabras clave de CIERRE de postulación.
        # "plazo" a secas es demasiado genérico (coincide con "plazo de ejecución",
        # que es la duración del proyecto, no el cierre de la convocatoria).
        keywords = [
            "fecha de cierre", "fecha límite", "plazo de presentación",
            "plazo de postulación", "deadline", "closing date",
            "fecha de presentación", "hasta el", "recepción de propuestas",
            "convocatoria cierra", "postulaciones hasta", "application deadline",
        ]
        content_lower = content.lower()
        for kw in keywords:
            idx = content_lower.find(kw)
            if idx == -1:
                continue
            # La fecha puede preceder a la keyword ("31 de marzo... es la fecha
            # límite") o seguirla — buscar en ambas direcciones, no solo hacia adelante.
            fragment = content[max(0, idx - 100): idx + 200]
            d = parse_deadline_text(fragment)
            if d:
                return d
        # Si no encontró cerca de keywords, buscar cualquier fecha en el documento
        d = parse_deadline_text(content[:5000])
        return d
    except Exception:
        return None


def verify_deadline(funder_url: str, llm_deadline_text: str) -> dict:
    """
    Verifica la fecha de cierre de una convocatoria.

    1. Parsea el texto del LLM.
    2. Intenta confirmar/actualizar desde la URL oficial.
    3. Devuelve el resultado completo con estado.

    Returns:
        {
          "deadline_text": str,         # texto normalizado
          "deadline_iso": str | None,   # "YYYY-MM-DD" o None
          "status": str,                # abierta|por_cerrar|cerrada|sin_fecha
          "dias_restantes": int | None,
          "label": str,                 # texto para mostrar al usuario
          "color": str,                 # ok|warn|err|muted
          "fuente": str,                # "llm"|"web"|"no_determinada"
        }
    """
    # 1. Parsear fecha del LLM
    dl_llm = parse_deadline_text(llm_deadline_text or "")

    # 2. Intentar obtener fecha desde la web
    dl_web = None
    if funder_url:
        try:
            dl_web = _search_deadline_in_page(funder_url)
        except Exception:
            dl_web = None

    # 3. La fecha de la página oficial, recién descargada, es la fuente de verdad:
    #    se prefiere SIEMPRE que exista, sin importar si es anterior o posterior a
    #    la del LLM. Preferir la más tardía ("optimista") escondía silenciosamente
    #    casos reales de convocatorias que acortaron su plazo.
    discrepancia = bool(dl_web and dl_llm and dl_web != dl_llm)
    if dl_web:
        dl_final = dl_web
        fuente = "web"
    elif dl_llm:
        dl_final = dl_llm
        fuente = "llm"
    else:
        dl_final = None
        fuente = "no_determinada"

    st = deadline_status(dl_final)
    return {
        "deadline_text": dl_final.strftime("%d/%m/%Y") if dl_final else (llm_deadline_text or "A determinar"),
        "deadline_iso": dl_final.isoformat() if dl_final else None,
        "fuente": fuente,
        "discrepancia": discrepancia,
        "deadline_llm_iso": dl_llm.isoformat() if dl_llm else None,
        **st,
    }
