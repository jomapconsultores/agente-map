"""
AGENTE 5 — EXTRACTOR DE DATOS ESTADÍSTICOS
───────────────────────────────────────────
Si el documento contiene datos numéricos analizables (indicadores, metas, series,
mediciones, comparaciones entre grupos, pares x-y), los extrae en JSON para que el
Excel genere estadística DESCRIPTIVA y AVANZADA con fórmulas vivas.

NO inventa datos: si el documento no tiene datos numéricos reales, devuelve
{"datasets": []} y no se crea hoja de estadística.
"""
from __future__ import annotations

import json

import config
from config import MAX_TOKENS_FINANCIAL
from models.schemas import DocumentBrief


SYSTEM_PROMPT = """
Eres un estadístico. Extraes ÚNICAMENTE datos numéricos REALES presentes en el documento y los
organizas para análisis. NO inventes valores. Si no hay datos numéricos analizables, devuelves
{"datasets": []}. Respondes SOLO con JSON válido, sin texto adicional.
"""


def _clip_preserve_tail(text: str, n: int = 60000, tail_chars: int = 18000) -> str:
    """Trunca preservando el inicio y el tramo final del documento (tablas de datos,
    series y anexos suelen ir al final). El truncado ciego por el frente
    (proposal[:40000]) dejaba invisible todo lo posterior al corte — para una tesis
    de 25-45k palabras eso omitía la mayor parte de los datos numéricos reales."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    head_budget = n - tail_chars - 200
    if head_budget <= 0:
        return text[:n]
    return (text[:head_budget]
            + "\n\n[…tramo intermedio omitido por longitud…]\n\n"
            + text[-tail_chars:])


def _build_prompt(brief: DocumentBrief, proposal: str) -> str:
    return f"""
Documento: {brief.title}

A partir del DOCUMENTO de abajo, extrae los conjuntos de datos numéricos que aparezcan
(indicadores con valores, metas/línea base, series temporales, mediciones, comparaciones entre
grupos, relaciones entre dos variables). Para cada conjunto elige el "type" adecuado:
- "series": una lista de valores → estadística descriptiva (media, mediana, desv., etc.).
- "xy": dos variables emparejadas → correlación y regresión lineal.
- "groups": dos o más grupos de valores → comparación (t-test / ANOVA).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DOCUMENTO:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_clip_preserve_tail(proposal)}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Responde SOLO con este JSON (sin texto antes ni después). Si no hay datos reales: {{"datasets": []}}.

{{
  "datasets": [
    {{
      "name": "<nombre del conjunto>",
      "type": "series|xy|groups",
      "unit": "<unidad o ''>",
      "values": [<números>],                          // solo si type=series
      "x": [<números>], "y": [<números>],             // solo si type=xy
      "x_label": "<etiqueta x>", "y_label": "<etiqueta y>",
      "groups": {{"<grupo1>": [<números>], "<grupo2>": [<números>]}},  // solo si type=groups
      "notes": "<qué representa / fuente en el documento>"
    }}
  ]
}}
"""


def _parse(raw: str) -> dict:
    from utils.json_utils import robust_json_loads
    return robust_json_loads(raw)


def _nums(v) -> list[float]:
    out = []
    for x in (v or []):
        try:
            if isinstance(x, str):
                x = x.replace(",", "").replace("%", "").strip()
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


def run(session, proposal: str, api_key: str | None = None) -> dict:
    """Extrae datasets numéricos del documento. Devuelve {"datasets": [...]} normalizado.
    Resiliente: ante cualquier fallo devuelve {"datasets": []} (no bloquea el pipeline)."""
    from agents import llm
    brief: DocumentBrief = session.brief
    try:
        raw, used = llm.complete_builder(
            config.ROLE_FINANCIAL, system=SYSTEM_PROMPT,
            prompt=_build_prompt(brief, proposal), max_tokens=MAX_TOKENS_FINANCIAL,
            anthropic_key=api_key, temperature=0.1)
        data = _parse(raw)
    except Exception:
        return {"datasets": []}

    clean: list[dict] = []
    for d in (data.get("datasets") or []):
        if not isinstance(d, dict):
            continue
        t = (d.get("type") or "series").lower()
        item = {"name": str(d.get("name", "Datos")), "type": t,
                "unit": str(d.get("unit", "")), "notes": str(d.get("notes", ""))}
        if t == "series":
            vals = _nums(d.get("values"))
            if len(vals) < 2:
                continue
            item["values"] = vals
        elif t == "xy":
            x, y = _nums(d.get("x")), _nums(d.get("y"))
            n = min(len(x), len(y))
            if n < 3:
                continue
            item["x"], item["y"] = x[:n], y[:n]
            item["x_label"] = str(d.get("x_label", "X"))
            item["y_label"] = str(d.get("y_label", "Y"))
        elif t == "groups":
            groups = {}
            for k, v in (d.get("groups") or {}).items():
                gv = _nums(v)
                if len(gv) >= 2:
                    groups[str(k)] = gv
            if len(groups) < 2:
                continue
            item["groups"] = groups
        else:
            continue
        clean.append(item)

    session.builder_log.append({"phase": "statistics", "datasets": len(clean)})
    return {"datasets": clean}
