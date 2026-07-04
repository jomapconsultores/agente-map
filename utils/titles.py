"""Título automático para sesiones sin título propio aún (compartido por
api/main.py y db/queries.py — antes cada uno tenía su propia copia, que ya
divergía en el formato de fecha: api/main.py usaba `strftime("%-d %b %Y")`
(no portable a Windows: %-d es una extensión de glibc/macOS) y db/queries.py
una lista manual de meses en español; esta es la única fuente de verdad."""
from __future__ import annotations

import datetime

DOC_TYPE_LABELS: dict[str, str] = {
    "propuesta":           "Propuesta",
    "articulo_cientifico": "Artículo científico",
    "tesis":               "Tesis",
    "tdr":                 "TDR",
    "informe":             "Informe",
    "peer_review":         "Revisión de pares",
    "legal_tecnico":       "Doc. legal/técnico",
    "auto":                "Documento",
}

_MESES_ES = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]


def auto_title(row: dict, is_scouting: bool = False) -> str:
    """Genera un título descriptivo cuando el pipeline aún no tiene uno."""
    created_raw = row.get("created_at")
    try:
        if created_raw:
            dt = datetime.datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        else:
            dt = datetime.datetime.now()
        fecha = f"{dt.day} {_MESES_ES[dt.month - 1]} {dt.year}"
    except Exception:
        fecha = str(created_raw or "")[:10]

    key = (row.get("doc_type_key") or "auto").lower()
    tipo = "Búsqueda de oportunidades" if is_scouting else DOC_TYPE_LABELS.get(key, key.replace("_", " ").title())

    inp = (row.get("user_input") or "").strip().replace("\n", " ").replace("\r", "")
    if inp:
        if len(inp) > 55:
            cut = inp[:55]
            space = cut.rfind(" ")
            inp = (cut[:space] if space > 20 else cut) + "…"
        return f"{tipo} — {inp} ({fecha})"
    return f"{tipo} ({fecha})"
