"""
AGENTE INTAKE — Análisis profundo de documentos de entrada
──────────────────────────────────────────────────────────
Analiza TODOS los materiales que el usuario entrega (plantilla/formulario, documentos
de apoyo, URLs) para extraer de forma estructurada:
  - Secciones y campos obligatorios del formulario/bases
  - Requisitos de formato que deben respetarse
  - Restricciones críticas (límites, criterios, elegibilidad, deadline)
  - Referencias a datos organizacionales necesarios (RUC, razón social, etc.)

El resultado (IntakeData) se guarda en session.intake_data y se inyecta en el brief
y en el redactor para garantizar que el documento final llene EXACTAMENTE el formato
exigido, especialmente si se trata de un formulario de convocatoria.

Si el usuario proporcionó URLs, se descargan y analizan aquí también.
"""
from __future__ import annotations

import concurrent.futures
import json
import re

import config
from agents import llm
from models.schemas import ProjectSession
from tools.search import execute_fetch_page

_MAX_CHARS = 5000


def _clip(text: str, n: int = _MAX_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "\n[…truncado…]"


def _fetch_urls(user_input: str) -> list[tuple[str, str]]:
    """Descarga contenido de URLs presentes en el texto del usuario."""
    urls = re.findall(r"https?://\S+", user_input or "")[:4]
    docs: list[tuple[str, str]] = []
    if not urls:
        return docs

    def _fetch_one(url: str) -> str | None:
        try:
            content = execute_fetch_page(url)
            return content if content.strip() else None
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {url: executor.submit(_fetch_one, url) for url in urls}
        for url in urls:
            content = futures[url].result()
            if content:
                docs.append((url, content))
    return docs


_SYSTEM = """
Eres un experto en análisis de convocatorias, formularios de financiamiento y bases técnicas.
Tu tarea es extraer de forma estructurada TODOS los requisitos, secciones, campos y restricciones
de los documentos de entrada, para que el redactor los cumpla al pie de la letra.
Respondes ÚNICAMENTE con JSON válido. Sin texto extra.
"""


def analyze(session: ProjectSession) -> dict:
    """Analiza los documentos de entrada y devuelve requisitos estructurados.

    Retorna dict con:
      required_sections  - secciones/campos obligatorios [{name, description, mandatory}]
      format_overrides   - especificaciones de formato detectadas
      key_constraints    - restricciones críticas (límites, criterios, deadlines, elegibilidad)
      org_refs           - datos organizacionales que el formulario exige (RUC, razón social…)
      url_contents       - lista de URLs descargadas
      raw_notes          - observaciones adicionales del análisis
    """
    # Recopilar documentos de entrada
    docs_to_analyze: list[tuple[str, str]] = []

    if session.template_text:
        docs_to_analyze.append(("PLANTILLA/FORMULARIO", session.template_text))

    for name, content in (session.support_docs or []):
        docs_to_analyze.append((name, content))

    url_docs = _fetch_urls(session.user_input or "")
    docs_to_analyze.extend(url_docs)

    if not docs_to_analyze:
        return {
            "required_sections": [],
            "format_overrides": {},
            "key_constraints": [],
            "org_refs": [],
            "url_contents": [],
            "raw_notes": "Sin documentos de entrada adicionales.",
        }

    docs_block = "\n\n".join(
        f"=== {name} ===\n{_clip(content, 4000)}"
        for name, content in docs_to_analyze
    )
    url_list = [u for u, _ in url_docs]

    prompt = f"""
Analiza los documentos de entrada para el proyecto: «{_clip(session.user_input, 400)}»

{docs_block}

Extrae TODOS los requisitos, secciones, campos y restricciones. Devuelve SOLO este JSON:
{{
  "required_sections": [
    {{"name": "<sección o campo>", "description": "<qué debe contener exactamente>", "mandatory": true}}
  ],
  "format_overrides": {{
    "font_name": null, "font_size": null, "max_pages": null, "max_words": null,
    "citation_style": null, "language": null,
    "notes": "<otras restricciones de formato que encontraste>"
  }},
  "key_constraints": [
    "<restricción crítica: límite, criterio de evaluación, deadline, requisito de elegibilidad>"
  ],
  "org_refs": [
    "<campo organizacional requerido: nombre de la organización, RUC, representante legal, etc.>"
  ],
  "url_contents": {json.dumps(url_list, ensure_ascii=False)},
  "raw_notes": "<observaciones generales sobre los documentos analizados>"
}}
"""
    try:
        raw = llm.complete(
            config.ROLE_CLASSIFIER, system=_SYSTEM, prompt=prompt,
            max_tokens=2000, temperature=0.2,
        )
        raw = raw.strip()
        from utils.json_utils import robust_json_loads
        data = robust_json_loads(raw)
        data["url_contents"] = url_list
        return data
    except Exception as ex:
        return {
            "required_sections": [],
            "format_overrides": {},
            "key_constraints": [],
            "org_refs": [],
            "url_contents": url_list,
            "raw_notes": f"Error en análisis de intake: {ex}",
        }


def intake_block(intake_data: dict) -> str:
    """Bloque formateado del intake para insertar en prompts del redactor."""
    if not intake_data or not (
        intake_data.get("required_sections") or intake_data.get("key_constraints")
    ):
        return ""

    lines = [
        "═══════════════════════════════════════════════════════════",
        "ANÁLISIS DE DOCUMENTOS DE ENTRADA (secciones y campos obligatorios)",
        "═══════════════════════════════════════════════════════════",
    ]

    sections = intake_data.get("required_sections") or []
    if sections:
        lines.append("\nCAMPOS Y SECCIONES OBLIGATORIOS — DEBES CUBRIRLOS TODOS:")
        for s in sections:
            mand = "✦ OBLIGATORIO" if s.get("mandatory") else "  opcional"
            lines.append(f"  [{mand}] {s.get('name', '')}: {s.get('description', '')}")

    constraints = intake_data.get("key_constraints") or []
    if constraints:
        lines.append("\nRESTRICCIONES CRÍTICAS:")
        for c in constraints:
            lines.append(f"  • {c}")

    org_refs = intake_data.get("org_refs") or []
    if org_refs:
        lines.append("\nDAtos ORGANIZACIONALES REQUERIDOS (ver carpeta Empresas/):")
        for r in org_refs:
            lines.append(f"  • {r}")

    notes = intake_data.get("raw_notes", "")
    if notes and "sin documentos" not in notes.lower():
        lines.append(f"\nNOTAS: {notes}")

    return "\n".join(lines)
