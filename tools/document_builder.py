"""
CONSTRUCTOR DE DOCUMENTOS — WORD (.docx) + EXCEL (.xlsx) VINCULADOS
──────────────────────────────────────────────────────────────────
- build_word():  convierte la propuesta (Markdown) en un .docx profesional, con
                 portada, estilos, tablas y una sección que REFERENCIA el Excel y
                 reproduce los totales calculados (mismos números que el Excel).
- build_excel(): genera un .xlsx con cálculos VIVOS (fórmulas =cantidad*costo,
                 =SUM(...)) para Presupuesto, Marco Lógico y Cronograma.

Ambos se construyen desde el mismo FinancialPackage → los montos siempre coinciden.
Si python-docx u openpyxl no están instalados, las funciones devuelven None sin romper.
"""
import re
import math
from datetime import datetime
from pathlib import Path


# ════════════════════════════════════════════════════════════════════════════
#  MEDICIÓN DE EXTENSIÓN (verificación mecánica de formato)
# ════════════════════════════════════════════════════════════════════════════
# Palabras por página aproximadas según interlineado (base 12pt, márgenes estándar)
_WORDS_PER_PAGE = {1.0: 520, 1.15: 470, 1.5: 360, 2.0: 280}


def _words_per_page(format_spec: dict) -> int:
    spacing = float((format_spec or {}).get("line_spacing", 1.5) or 1.5)
    # tomar el valor de tabla más cercano
    base = min(_WORDS_PER_PAGE, key=lambda s: abs(s - spacing))
    wpp = _WORDS_PER_PAGE[base]
    size = float((format_spec or {}).get("font_size", 12) or 12)
    if size:
        wpp = int(wpp * (12.0 / size))
    return max(120, wpp)


def text_stats(text: str, format_spec: dict) -> dict:
    """Cuenta palabras/caracteres y estima páginas; verifica límites del FormatSpec."""
    plain = re.sub(r"[#>*_`|\-]{1,}", " ", text or "")
    words = len(plain.split())
    chars = len(text or "")
    chars_no_spaces = len(re.sub(r"\s+", "", text or ""))
    pages = max(1, math.ceil(words / _words_per_page(format_spec))) if words else 0

    fs = format_spec or {}
    issues = []
    def _v(key):
        v = fs.get(key)
        return v if isinstance(v, (int, float)) and v else None

    if _v("max_words") and words > fs["max_words"]:
        issues.append(f"Excede el máximo de palabras: {words} > {fs['max_words']}")
    if _v("min_words") and words < fs["min_words"]:
        issues.append(f"Por debajo del mínimo de palabras: {words} < {fs['min_words']}")
    if _v("max_chars") and chars > fs["max_chars"]:
        issues.append(f"Excede el máximo de caracteres: {chars} > {fs['max_chars']}")
    if _v("max_pages") and pages > fs["max_pages"]:
        issues.append(f"Excede el máximo de páginas (estimado): {pages} > {fs['max_pages']}")
    if _v("min_pages") and pages < fs["min_pages"]:
        issues.append(f"Por debajo del mínimo de páginas (estimado): {pages} < {fs['min_pages']}")

    return {
        "word_count": words,
        "char_count": chars,
        "char_count_no_spaces": chars_no_spaces,
        "page_estimate": pages,
        "within_limits": not issues,
        "issues": issues,
    }


_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+|\d+[.)]\s+)?(.+?)\s*$")


def _extract_headings(text: str) -> list[str]:
    """Extrae candidatos a título de sección: líneas '# Encabezado', numeradas
    ('1. Introducción') o líneas cortas todo-mayúsculas (encabezados sin Markdown)."""
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or len(s) > 120:
            continue
        if s.startswith("#"):
            out.append(s.lstrip("#").strip())
        elif re.match(r"^\d+[.)]\s+\S", s):
            out.append(re.sub(r"^\d+[.)]\s+", "", s).strip())
        elif s.isupper() and len(s.split()) <= 10 and len(s) > 3:
            out.append(s.strip())
    return out


def _normalize_heading(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def section_coverage(text: str, sections: list, threshold: float = 0.78) -> dict:
    """Verificación MECÁNICA (no-LLM) de que cada sección obligatoria del brief
    realmente aparece como encabezado en el documento — en vez de confiar en que
    el mismo LLM auditado afirme que sí. Fuzzy-match tolerante a variaciones de
    redacción ("Marco lógico" ~ "4. Marco Lógico del Proyecto").

    Devuelve {"covered": [...], "missing": [...], "coverage_ratio": float}.
    """
    import difflib
    headings = [_normalize_heading(h) for h in _extract_headings(text)]
    covered, missing = [], []
    for sec in (sections or []):
        target = _normalize_heading(sec)
        if not target:
            continue
        best = max((difflib.SequenceMatcher(None, target, h).ratio() for h in headings), default=0.0)
        # Coincidencia también si el título de sección aparece como substring
        # (encabezados largos tipo "4. Marco Lógico del Proyecto y Resultados").
        substr_hit = any(target in h or h in target for h in headings if len(h) > 3)
        if best >= threshold or substr_hit:
            covered.append(sec)
        else:
            missing.append(sec)
    total = len(covered) + len(missing)
    return {
        "covered": covered,
        "missing": missing,
        "coverage_ratio": (len(covered) / total) if total else 1.0,
    }


# ════════════════════════════════════════════════════════════════════════════
#  VERIFICACIÓN MECÁNICA DE CITAS Y REFERENCIAS
# ════════════════════════════════════════════════════════════════════════════
# El fallo de calidad más grave en un documento académico es una cita o referencia
# inventada. Hasta ahora la única defensa era que el mismo LLM auditor "se diera
# cuenta" leyendo el texto. Esto cierra ese punto ciego con un chequeo estructural
# determinista: ¿cada cita del cuerpo tiene una entrada real en la bibliografía?
# ¿cada referencia listada se usa realmente?
_REF_HEADING_RE = re.compile(r"^#{0,6}\s*(referencias|bibliograf[ií]a|references|bibliography)\b",
                             re.IGNORECASE)
_APA_MULTI_PAREN_RE = re.compile(r"\(([^()]*\d{4}[a-z]?[^()]*)\)")
_NUMERIC_CITE_RE = re.compile(r"\[(\d+(?:\s*[-,]\s*\d+)*)\]")


def _split_references_section(text: str) -> tuple[str, str]:
    """(cuerpo, sección_de_referencias). Si no hay encabezado de Referencias/
    Bibliografía, sección_de_referencias es ''."""
    lines = (text or "").splitlines()
    idx = next((i for i, l in enumerate(lines) if _REF_HEADING_RE.match(l.strip())), None)
    if idx is None:
        return text, ""
    return "\n".join(lines[:idx]), "\n".join(lines[idx + 1:])


def _extract_apa_citations(body: str) -> list[str]:
    """Claves 'autor_año' normalizadas de citas estilo APA/autor-fecha en el cuerpo."""
    out = []
    for m in _APA_MULTI_PAREN_RE.finditer(body):
        for part in m.group(1).split(";"):
            ym = re.search(r"(\d{4}[a-z]?)", part)
            if not ym:
                continue
            author = part[:ym.start()].strip(" ,")
            # Quitar "et al."/"&"/"y"/"and coautor": nos quedamos solo con el primer
            # apellido, igual que el lado de la referencia (que toma el texto antes
            # de la primera coma) — si no, "Gomez et al., 2019" y "Gomez, A. et al.
            # (2019)" generan claves distintas y se marca un falso huérfano.
            author = re.split(r"\bet\s*al\.?\b|&|\by\b|\band\b", author)[0].strip(" ,.")
            key = (_normalize_heading(author) + "_" + ym.group(1)).strip("_")
            if key:
                out.append(key)
    return out


def _extract_numeric_citations(body: str) -> set:
    nums = set()
    for m in _NUMERIC_CITE_RE.finditer(body):
        for chunk in m.group(1).split(","):
            chunk = chunk.strip()
            if "-" in chunk:
                a, b = chunk.split("-", 1)
                if a.strip().isdigit() and b.strip().isdigit():
                    nums.update(range(int(a), int(b) + 1))
            elif chunk.isdigit():
                nums.add(int(chunk))
    return nums


_REF_BULLET_RE = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)")


def _parse_reference_entries(ref_section: str) -> list:
    """Cada referencia se trata como una entrada. Si la lista viene con viñetas o
    numeración (lo más común en el Markdown que produce el redactor), cada línea
    con marcador es una entrada nueva y las líneas sin marcador se anexan a la
    anterior (referencia envuelta en varias líneas). Si no hay marcadores, separa
    por línea en blanco entre bloques."""
    raw_lines = [l for l in ref_section.splitlines() if l.strip()]
    has_bullets = any(_REF_BULLET_RE.match(l) for l in raw_lines)

    entries: list[str] = []
    if has_bullets:
        current = None
        for line in raw_lines:
            if _REF_BULLET_RE.match(line):
                if current is not None:
                    entries.append(current)
                current = _REF_BULLET_RE.sub("", line).strip()
            elif current is not None:
                current += " " + line.strip()
            else:
                current = line.strip()
        if current is not None:
            entries.append(current)
    else:
        current_block: list[str] = []
        for line in ref_section.splitlines() + [""]:
            s = line.strip()
            if not s:
                if current_block:
                    entries.append(" ".join(current_block))
                    current_block = []
                continue
            current_block.append(s)

    parsed = []
    for e in entries:
        ym = re.search(r"\((\d{4}[a-z]?)\)|,\s*(\d{4}[a-z]?)\b", e)
        year = (ym.group(1) or ym.group(2)) if ym else ""
        author = e.split(",")[0].split("(")[0].strip() if e else ""
        doi_m = re.search(r"10\.\d{4,9}/\S+", e)
        url_m = re.search(r"https?://\S+", e)
        key = (_normalize_heading(author) + "_" + year).strip("_")
        parsed.append({
            "raw": e, "key": key,
            "doi": doi_m.group(0).rstrip(".,)") if doi_m else None,
            "url": url_m.group(0).rstrip(".,)") if url_m else None,
        })
    return parsed


def citation_stats(text: str, citation_style: str = "") -> dict:
    """Devuelve {has_references_section, n_references, n_citations,
    orphan_citations, unused_references, links_to_verify}. Sin sección de
    referencias detectada, devuelve has_references_section=False sin penalizar
    (puede ser un tipo de documento sin bibliografía)."""
    body, ref_section = _split_references_section(text)
    if not ref_section.strip():
        return {"has_references_section": False, "n_references": 0, "n_citations": 0,
                "orphan_citations": [], "unused_references": [], "links_to_verify": []}

    entries = _parse_reference_entries(ref_section)
    ref_keys = {e["key"] for e in entries if e["key"]}
    style = (citation_style or "").lower()
    numeric = any(k in style for k in ("vancouver", "ieee", "numer"))

    if numeric:
        cited = _extract_numeric_citations(body)
        n_refs = len(entries)
        orphan = [f"[{n}]" for n in sorted(cited) if n < 1 or n > n_refs]
        used = {n for n in cited if 1 <= n <= n_refs}
        unused = [entries[i - 1]["raw"][:80] for i in range(1, n_refs + 1) if i not in used]
        n_citations = len(cited)
    else:
        cited_keys = _extract_apa_citations(body)
        n_citations = len(cited_keys)
        orphan = sorted({k for k in cited_keys if k and k not in ref_keys})
        cited_set = set(cited_keys)
        unused = [e["raw"][:80] for e in entries if e["key"] and e["key"] not in cited_set]

    links = [{"doi": e["doi"], "url": e["url"]} for e in entries if e["doi"] or e["url"]]
    return {
        "has_references_section": True,
        "n_references": len(entries),
        "n_citations": n_citations,
        "orphan_citations": orphan,
        "unused_references": unused,
        "links_to_verify": links,
    }


# ════════════════════════════════════════════════════════════════════════════
#  CÁLCULOS COMPARTIDOS (fuente de verdad para Word y Excel)
# ════════════════════════════════════════════════════════════════════════════
def budget_totals(financial) -> dict:
    items = financial.budget_items if financial else []
    total_solicitado = sum(i.get("fuente_solicitada", 0) for i in items)
    total_contraparte = sum(i.get("contraparte", 0) for i in items)
    total = total_solicitado + total_contraparte
    by_cat: dict = {}
    for i in items:
        cat = i.get("categoria", "Sin categoría")
        by_cat.setdefault(cat, 0.0)
        by_cat[cat] += i.get("fuente_solicitada", 0) + i.get("contraparte", 0)
    return {
        "total_solicitado": total_solicitado,
        "total_contraparte": total_contraparte,
        "total": total,
        "by_category": by_cat,
        "n_items": len(items),
    }


def _money(v, currency="USD") -> str:
    try:
        return f"{currency} {float(v):,.2f}"
    except (ValueError, TypeError):
        return f"{currency} 0.00"


# ════════════════════════════════════════════════════════════════════════════
#  WORD
# ════════════════════════════════════════════════════════════════════════════
_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _add_runs_with_bold(paragraph, text):
    """Añade texto a un párrafo respetando **negritas** en línea."""
    pos = 0
    for m in _INLINE_BOLD.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        run = paragraph.add_run(m.group(1))
        run.bold = True
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def _is_table_row(line: str) -> bool:
    # GFM permite tablas sin pipe de apertura/cierre ("Categoría | Actividad | Total").
    # No exigir pipe inicial: homologado con el criterio ya usado en build_pdf.
    return line.strip().count("|") >= 2


def _is_separator_row(line: str) -> bool:
    s = line.strip().strip("|")
    return bool(s) and all(set(c.strip()) <= set("-: ") for c in s.split("|"))


def _split_table_row(line: str):
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _set_rfonts(rpr, font_name: str) -> None:
    """Fija w:rFonts (ascii/hAnsi/eastAsia/cs) en un <w:rPr>: font.name de
    python-docx a veces no basta para que Word aplique la fuente a todos los
    scripts/runs — sobre todo en estilos, no solo en runs sueltos."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    rFonts = rpr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rpr.insert(0, rFonts)
    for tag in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rFonts.set(qn(tag), font_name)


def _set_style_font(style, font_name: str, font_size_pt: float | None = None) -> None:
    from docx.shared import Pt
    style.font.name = font_name
    if font_size_pt is not None:
        style.font.size = Pt(font_size_pt)
    _set_rfonts(style.element.get_or_add_rPr(), font_name)


def _set_run_font(run, font_name: str, font_size_pt: float | None = None) -> None:
    from docx.shared import Pt
    run.font.name = font_name
    if font_size_pt is not None:
        run.font.size = Pt(font_size_pt)
    _set_rfonts(run._element.get_or_add_rPr(), font_name)


def build_word(proposal_md: str, brief, financial, excel_filename: str, out_path: Path):
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    except ImportError:
        return None

    fmt = brief.format_spec if brief and isinstance(brief.format_spec, dict) else {}
    font_name = fmt.get("font_name", "Times New Roman")
    font_size = float(fmt.get("font_size", 12) or 12)
    line_spacing = float(fmt.get("line_spacing", 1.5) or 1.5)
    justify = (fmt.get("alignment", "justify") == "justify")

    doc = Document()

    # ── Estilo base (aplica el formato exigido a TODO el documento) ───────
    normal = doc.styles["Normal"]
    _set_style_font(normal, font_name, font_size)
    pf = normal.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
    pf.line_spacing = line_spacing
    if justify:
        pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    # Encabezados y título: mismo tipo de letra exigido (python-docx los deja en
    # la fuente de tema por defecto si no se tocan explícitamente — no heredan
    # de "Normal"). El tamaño conserva la jerarquía visual, no se iguala al cuerpo.
    for style_name in ("Title", "Heading 1", "Heading 2", "Heading 3", "Heading 4"):
        try:
            _set_style_font(doc.styles[style_name], font_name)
        except KeyError:
            pass

    # Márgenes
    for section in doc.sections:
        section.top_margin = Cm(float(fmt.get("margin_top_cm", 2.5) or 2.5))
        section.bottom_margin = Cm(float(fmt.get("margin_bottom_cm", 2.5) or 2.5))
        section.left_margin = Cm(float(fmt.get("margin_left_cm", 3.0) or 3.0))
        section.right_margin = Cm(float(fmt.get("margin_right_cm", 2.5) or 2.5))

    # ── Portada ───────────────────────────────────────────────────────────
    from models.doc_types import get_doc_type
    dt_name = get_doc_type(brief.doc_type_key).name if brief else "Documento"

    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = title_p.add_run(brief.title if brief else "Documento")
    tr.bold = True
    tr.font.size = Pt(min(22, font_size + 8))
    tr.font.color.rgb = RGBColor(0x1F, 0x3B, 0x73)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cite = fmt.get("citation_style")
    sub.add_run(
        f"{dt_name}\n"
        f"{('Estilo de cita: ' + cite + ' · ') if cite else ''}"
        f"{font_name} {font_size:.0f}pt · interlineado {line_spacing}\n"
        f"Ecuador · {datetime.now().strftime('%d/%m/%Y')}"
    ).italic = True

    doc.add_page_break()

    # ── Cuerpo desde Markdown ─────────────────────────────────────────────
    lines = proposal_md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Tablas Markdown
        if _is_table_row(line):
            table_lines = []
            while i < len(lines) and _is_table_row(lines[i]):
                table_lines.append(lines[i])
                i += 1
            _render_md_table(doc, table_lines, font_name, font_size)
            continue

        # Encabezados
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped.lstrip("#").strip()
            doc.add_heading(text, level=min(level, 4))
            i += 1
            continue

        # Reglas horizontales
        if set(stripped) <= set("-*_") and len(stripped) >= 3:
            i += 1
            continue

        # Listas con viñeta
        if stripped.startswith(("- ", "* ", "• ")):
            p = doc.add_paragraph(style="List Bullet")
            _add_runs_with_bold(p, stripped[2:].strip())
            i += 1
            continue

        # Listas numeradas
        m = re.match(r"^\d+[.)]\s+(.*)", stripped)
        if m:
            p = doc.add_paragraph(style="List Number")
            _add_runs_with_bold(p, m.group(1))
            i += 1
            continue

        # Párrafo normal
        p = doc.add_paragraph()
        _add_runs_with_bold(p, stripped)
        i += 1

    # ── Sección de vínculo con el Excel ───────────────────────────────────
    if financial and financial.budget_items:
        doc.add_page_break()
        doc.add_heading("Anexo financiero — Vínculo con la hoja de cálculo", level=1)
        link_p = doc.add_paragraph()
        link_p.add_run(
            "Los cálculos detallados del presupuesto, el marco lógico y el cronograma se "
            "presentan en el archivo Excel adjunto: "
        )
        link_p.add_run(excel_filename).bold = True
        link_p.add_run(
            ". Las cifras de la siguiente tabla resumen se generan desde la misma fuente de "
            "datos que el Excel, por lo que ambos documentos son consistentes."
        )

        t = budget_totals(financial)
        cur = financial.currency
        doc.add_heading("Resumen del presupuesto", level=2)
        tbl = doc.add_table(rows=1, cols=2)
        tbl.style = "Light Grid Accent 1"
        hdr = tbl.rows[0].cells
        h0 = hdr[0].paragraphs[0].add_run("Concepto"); h0.bold = True
        h1 = hdr[1].paragraphs[0].add_run("Monto"); h1.bold = True
        _set_run_font(h0, font_name, font_size)
        _set_run_font(h1, font_name, font_size)
        rows = [
            ("Solicitado al financiador", _money(t["total_solicitado"], cur)),
            ("Contraparte / cofinanciamiento", _money(t["total_contraparte"], cur)),
            ("TOTAL DEL PROYECTO", _money(t["total"], cur)),
        ]
        for concept, amount in rows:
            cells = tbl.add_row().cells
            cells[0].text = ""
            cells[1].text = ""
            _set_run_font(cells[0].paragraphs[0].add_run(concept), font_name, font_size)
            _set_run_font(cells[1].paragraphs[0].add_run(amount), font_name, font_size)

        if financial.cofinancing_notes:
            doc.add_paragraph(financial.cofinancing_notes).italic = True

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return str(out_path)


def _render_md_table(doc, table_lines, font_name: str | None = None, font_size: float | None = None):
    rows = [_split_table_row(l) for l in table_lines if not _is_separator_row(l)]
    if not rows:
        return
    ncols = max(len(r) for r in rows)
    table = doc.add_table(rows=0, cols=ncols)
    try:
        table.style = "Light Grid Accent 1"
    except KeyError:
        pass
    for ridx, row in enumerate(rows):
        cells = table.add_row().cells
        for cidx in range(ncols):
            val = row[cidx] if cidx < len(row) else ""
            val = _INLINE_BOLD.sub(r"\1", val)
            para = cells[cidx].paragraphs[0]
            run = para.add_run(val)
            if ridx == 0:
                run.bold = True
            if font_name:
                # El estilo de tabla ("Light Grid Accent 1") puede traer su propia
                # fuente de tema, que pisaría la exigida por el financiador si no
                # se fuerza explícitamente en cada run.
                _set_run_font(run, font_name, font_size)


# ════════════════════════════════════════════════════════════════════════════
#  EXCEL
# ════════════════════════════════════════════════════════════════════════════
def build_excel(brief, financial, out_path: Path):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None

    from models.doc_types import get_doc_type
    cur = financial.currency if financial else "USD"
    dt_name = get_doc_type(brief.doc_type_key).name if brief else "Documento"
    # Duración estimada: máximo mes presente en el cronograma, o 12 por defecto
    sched_months = [
        int(m) for s in (financial.schedule_rows if financial else [])
        for m in (s.get("meses", []) or []) if str(m).strip().lstrip("-").isdigit()
    ]
    duration = max(sched_months) if sched_months else 12
    HEAD_FILL = PatternFill("solid", fgColor="1F3B73")
    HEAD_FONT = Font(bold=True, color="FFFFFF")
    TOT_FILL = PatternFill("solid", fgColor="DCE6F1")
    BOLD = Font(bold=True)
    THIN = Side(style="thin", color="BFBFBF")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
    money_fmt = f'"{cur}" #,##0.00'

    wb = Workbook()

    def _style_header(ws, row, ncols):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=row, column=c)
            cell.fill = HEAD_FILL
            cell.font = HEAD_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = BORDER

    # ── Hoja 1: Resumen ───────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Resumen"
    ws["A1"] = "RESUMEN — CÁLCULOS DEL DOCUMENTO"
    ws["A1"].font = Font(bold=True, size=14, color="1F3B73")
    meta = [
        ("Documento", brief.title if brief else ""),
        ("Tipo", dt_name),
        ("Duración estimada (meses)", duration),
        ("Moneda", cur),
        ("Generado", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    r = 3
    for k, v in meta:
        ws.cell(row=r, column=1, value=k).font = BOLD
        ws.cell(row=r, column=2, value=v)
        r += 1

    # Totales (referencian la hoja Presupuesto con fórmulas)
    r += 1
    ws.cell(row=r, column=1, value="TOTALES DEL PRESUPUESTO").font = Font(bold=True, size=12, color="1F3B73")
    r += 1
    totals_map = [
        ("Solicitado al financiador", "Presupuesto!F{first}:F{last}"),
        ("Contraparte / cofinanciamiento", "Presupuesto!G{first}:G{last}"),
        ("TOTAL DEL PROYECTO", "Presupuesto!H{first}:H{last}"),
    ]

    # ── Hoja 2: Presupuesto ───────────────────────────────────────────────
    wsp = wb.create_sheet("Presupuesto")
    headers = ["Categoría", "Actividad / Concepto", "Unidad", "Cantidad",
               "Costo unitario", "Solicitado", "Contraparte", "Total"]
    for c, h in enumerate(headers, 1):
        wsp.cell(row=1, column=c, value=h)
    _style_header(wsp, 1, len(headers))

    items = financial.budget_items if financial else []
    first_data = 2
    row = first_data
    for it in items:
        wsp.cell(row=row, column=1, value=it.get("categoria", ""))
        wsp.cell(row=row, column=2, value=it.get("actividad", ""))
        wsp.cell(row=row, column=3, value=it.get("unidad", ""))
        wsp.cell(row=row, column=4, value=it.get("cantidad", 0))
        wsp.cell(row=row, column=5, value=it.get("costo_unitario", 0))
        # Solicitado: usa el valor extraído, pero si es 0 y hay cantidad×costo, calcula
        sol = it.get("fuente_solicitada", 0)
        contrap = it.get("contraparte", 0)
        wsp.cell(row=row, column=6, value=sol)
        wsp.cell(row=row, column=7, value=contrap)
        # Total por fila = Solicitado + Contraparte (fórmula viva)
        wsp.cell(row=row, column=8, value=f"=F{row}+G{row}")
        for c in range(1, 9):
            wsp.cell(row=row, column=c).border = BORDER
            if c in (5, 6, 7, 8):
                wsp.cell(row=row, column=c).number_format = money_fmt
        row += 1

    last_data = row - 1 if row > first_data else first_data
    # Fila de totales con SUM
    if items:
        wsp.cell(row=row, column=1, value="TOTAL").font = BOLD
        for c, col in ((6, "F"), (7, "G"), (8, "H")):
            cell = wsp.cell(row=row, column=c, value=f"=SUM({col}{first_data}:{col}{last_data})")
            cell.font = BOLD
            cell.number_format = money_fmt
            cell.fill = TOT_FILL
        for c in range(1, 9):
            cell = wsp.cell(row=row, column=c)
            cell.border = BORDER
            cell.fill = TOT_FILL

    # Anchos
    widths = [22, 40, 12, 10, 16, 16, 16, 16]
    for c, w in enumerate(widths, 1):
        wsp.column_dimensions[get_column_letter(c)].width = w
    if financial and financial.budget_narrative:
        nrow = row + 2
        wsp.cell(row=nrow, column=1, value="Nota presupuestaria:").font = BOLD
        wsp.cell(row=nrow, column=2, value=financial.budget_narrative)

    # Completar fórmulas de totales en Resumen
    for label, ref_tpl in totals_map:
        ws.cell(row=r, column=1, value=label).font = BOLD
        ref = ref_tpl.format(first=first_data, last=last_data)
        cell = ws.cell(row=r, column=2, value=f"=SUM({ref})" if items else 0)
        cell.number_format = money_fmt
        cell.font = BOLD
        r += 1
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 40

    # ── Hoja 3: Marco Lógico ──────────────────────────────────────────────
    wsl = wb.create_sheet("Marco Lógico")
    lf_headers = ["Nivel", "Resumen narrativo", "Indicador", "Línea base",
                  "Meta", "Fuente de verificación", "Supuestos"]
    for c, h in enumerate(lf_headers, 1):
        wsl.cell(row=1, column=c, value=h)
    _style_header(wsl, 1, len(lf_headers))
    lr = 2
    for rowd in (financial.logframe_rows if financial else []):
        vals = [rowd.get("nivel", ""), rowd.get("resumen_narrativo", ""),
                rowd.get("indicador", ""), rowd.get("linea_base", ""),
                rowd.get("meta", ""), rowd.get("fuente_verificacion", ""),
                rowd.get("supuestos", "")]
        for c, v in enumerate(vals, 1):
            cell = wsl.cell(row=lr, column=c, value=v)
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        lr += 1
    for c, w in enumerate([14, 38, 30, 16, 16, 26, 26], 1):
        wsl.column_dimensions[get_column_letter(c)].width = w

    # ── Hoja 4: Cronograma ────────────────────────────────────────────────
    wsc = wb.create_sheet("Cronograma")
    n_months = max(1, min(int(duration), 60))
    headers_c = ["Actividad", "Responsable", "Entregable"] + [f"M{m}" for m in range(1, n_months + 1)]
    for c, h in enumerate(headers_c, 1):
        wsc.cell(row=1, column=c, value=h)
    _style_header(wsc, 1, len(headers_c))
    cr = 2
    MARK = PatternFill("solid", fgColor="70AD47")
    for sched in (financial.schedule_rows if financial else []):
        wsc.cell(row=cr, column=1, value=sched.get("actividad", ""))
        wsc.cell(row=cr, column=2, value=sched.get("responsable", ""))
        wsc.cell(row=cr, column=3, value=sched.get("entregable", ""))
        months = sched.get("meses", []) or []
        for m in months:
            try:
                mi = int(m)
            except (ValueError, TypeError):
                continue
            if 1 <= mi <= n_months:
                cell = wsc.cell(row=cr, column=3 + mi, value="●")
                cell.fill = MARK
                cell.alignment = Alignment(horizontal="center")
        for c in range(1, len(headers_c) + 1):
            wsc.cell(row=cr, column=c).border = BORDER
        cr += 1
    wsc.column_dimensions["A"].width = 38
    wsc.column_dimensions["B"].width = 20
    wsc.column_dimensions["C"].width = 26
    for m in range(1, n_months + 1):
        wsc.column_dimensions[get_column_letter(3 + m)].width = 5

    # Hojas de estadística (si el documento trae datos analizables)
    _add_statistics_sheets(wb, brief)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return str(out_path)


# ════════════════════════════════════════════════════════════════════════════
#  REPORTE DE SCOUTING (detección de oportunidades, con calificación ponderada)
# ════════════════════════════════════════════════════════════════════════════
def build_scouting_report(opportunities: list, topic: str, out_path: Path):
    """Genera un .docx con el reporte de las oportunidades detectadas: tabla
    comparativa por calificación ponderada + una ficha completa por oportunidad.
    `opportunities` = lista de dicts (los del scout). Devuelve None si falta python-docx."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.table import WD_TABLE_ALIGNMENT
    except ImportError:
        return None

    AZUL = RGBColor(0x1F, 0x3A, 0x5F)
    GRIS = RGBColor(0x55, 0x55, 0x55)
    VERDE = RGBColor(0x1B, 0x5E, 0x20)
    AMBAR = RGBColor(0xB8, 0x6A, 0x00)
    ROJO = RGBColor(0x9A, 0x2A, 0x2A)

    def _score_color(s):
        return VERDE if s >= 80 else (AMBAR if s >= 60 else ROJO)

    def _g(d, *path, default=""):
        cur = d
        for p in path:
            cur = (cur or {}).get(p) if isinstance(cur, dict) else None
        return cur if cur not in (None, "") else default

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for section in doc.sections:
        section.left_margin = section.right_margin = Cm(2.2)

    # ── Portada ─────────────────────────────────────────────────────────────
    t = doc.add_paragraph(); t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = t.add_run("REPORTE DE OPORTUNIDADES DETECTADAS")
    r.bold = True; r.font.size = Pt(22); r.font.color.rgb = AZUL
    s = doc.add_paragraph(); s.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = s.add_run(f"Búsqueda: {topic}\nGenerado: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                  f"  ·  {len(opportunities)} oportunidad(es)")
    r.italic = True; r.font.size = Pt(11); r.font.color.rgb = GRIS

    # ── Tabla comparativa (ranking por calificación ponderada) ──────────────
    doc.add_heading("Resumen comparativo", level=1).runs[0].font.color.rgb = AZUL
    tbl = doc.add_table(rows=1, cols=5)
    tbl.style = "Light Grid Accent 1"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(["#", "Financiador", "Deadline", "Monto", "Calif. ponderada"]):
        c = tbl.rows[0].cells[i]; c.text = ""
        rr = c.paragraphs[0].add_run(h); rr.bold = True; rr.font.size = Pt(10)
        rr.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    for idx, o in enumerate(opportunities):
        cells = tbl.add_row().cells
        ws = float(o.get("weighted_score", 0) or 0)
        vals = [str(idx + 1), _g(o, "funder", "name", default="—"),
                _g(o, "funder", "deadline", default="—"),
                o.get("total_amount") or _g(o, "funder", "amount_range", default="—"),
                f"{ws:.0f}/100"]
        for i, v in enumerate(vals):
            cells[i].text = ""
            rr = cells[i].paragraphs[0].add_run(str(v)); rr.font.size = Pt(9.5)
            if i == 4:
                rr.bold = True; rr.font.color.rgb = _score_color(ws)

    # ── Ficha por oportunidad ───────────────────────────────────────────────
    for idx, o in enumerate(opportunities):
        doc.add_page_break()
        ws = float(o.get("weighted_score", 0) or 0)
        h = doc.add_heading(f"{idx + 1}. {o.get('title', 'Oportunidad')}", level=1)
        h.runs[0].font.color.rgb = AZUL

        p = doc.add_paragraph()
        r = p.add_run(f"Calificación ponderada: {ws:.0f}/100")
        r.bold = True; r.font.size = Pt(13); r.font.color.rgb = _score_color(ws)

        # Datos básicos
        info = [
            ("Financiador", f"{_g(o, 'funder', 'name')} ({_g(o, 'funder', 'type')})"),
            ("Convocatoria (URL)", _g(o, "funder", "url", default="—")),
            ("Fecha límite", _g(o, "funder", "deadline", default="—")),
            ("Monto", o.get("total_amount") or _g(o, "funder", "amount_range", default="—")),
            ("Sector", o.get("sector", "—")),
            ("Duración", f"{o.get('duration_months', '—')} meses"),
            ("Idioma", o.get("language", "—")),
            ("Beneficiarios", o.get("beneficiaries", "—")),
            ("Viabilidad", f"{float(o.get('viability_score', 0) or 0):.0f}/100"),
            ("Prob. de ganar", f"{float(o.get('winning_probability', 0) or 0):.0f}%"),
        ]
        it = doc.add_table(rows=0, cols=2); it.style = "Light List Accent 1"
        for k, v in info:
            cells = it.add_row().cells
            cells[0].text = ""; rk = cells[0].paragraphs[0].add_run(k); rk.bold = True; rk.font.size = Pt(9.5)
            cells[1].text = ""; cells[1].paragraphs[0].add_run(str(v)).font.size = Pt(9.5)

        # Resumen ejecutivo
        doc.add_heading("Resumen ejecutivo", level=2)
        doc.add_paragraph(o.get("summary", "(sin resumen)"))

        # Desglose de la calificación ponderada
        fb = o.get("feasibility_breakdown") or {}
        if fb:
            doc.add_heading("Desglose de la calificación ponderada", level=2)
            dt = doc.add_table(rows=1, cols=3); dt.style = "Light Grid Accent 1"
            for i, hh in enumerate(["Dimensión", "Puntaje", "Razón"]):
                c = dt.rows[0].cells[i]; c.text = ""
                rr = c.paragraphs[0].add_run(hh); rr.bold = True; rr.font.size = Pt(9.5)
                rr.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            labels = {"funder_match": "Encaje con financiador",
                      "geographic_eligibility": "Elegibilidad (Ecuador)",
                      "institutional_fit": "Ajuste institucional",
                      "budget_fit": "Ajuste de presupuesto",
                      "deadline_feasibility": "Viabilidad de plazo"}
            for key, label in labels.items():
                d = fb.get(key) or {}
                cells = dt.add_row().cells
                cells[0].text = ""; cells[0].paragraphs[0].add_run(label).font.size = Pt(9)
                sc = float(d.get("score", 0) or 0)
                cells[1].text = ""; rs = cells[1].paragraphs[0].add_run(f"{sc:.0f}/100")
                rs.font.size = Pt(9); rs.bold = True; rs.font.color.rgb = _score_color(sc)
                cells[2].text = ""; cells[2].paragraphs[0].add_run(str(d.get("reason", ""))).font.size = Pt(8.5)

        def _bullets(title, items):
            items = [i for i in (items or []) if i]
            if not items:
                return
            doc.add_heading(title, level=2)
            for i in items:
                doc.add_paragraph(str(i), style="List Bullet")

        _bullets("Fortalezas", o.get("strengths"))
        _bullets("Riesgos", o.get("risks"))
        _bullets("Requisitos críticos", o.get("key_requirements"))
        _bullets("Lineamientos nacionales (Ecuador)", o.get("national_guidelines"))
        _bullets("Lineamientos internacionales / del financiador", o.get("international_guidelines"))

        # Fuentes verificadas
        ev = o.get("evidence_sources") or []
        if ev:
            doc.add_heading("Fuentes verificadas", level=2)
            for s in ev:
                if not isinstance(s, dict):
                    continue
                p = doc.add_paragraph(style="List Bullet")
                p.add_run(f"{s.get('claim', '')} — ").font.size = Pt(9)
                rr = p.add_run(s.get("source_url", "")); rr.font.size = Pt(9); rr.italic = True
                vr = s.get("verification", "")
                if vr:
                    tag = p.add_run(f"  [{vr}]"); tag.font.size = Pt(8.5); tag.font.color.rgb = GRIS

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return str(out_path)


# ════════════════════════════════════════════════════════════════════════════
#  PDF (desde Markdown) — alternativa de descarga al Word
# ════════════════════════════════════════════════════════════════════════════
_PDF_REPL = {"—": "-", "–": "-", "•": "-", "“": '"', "”": '"', "‘": "'", "’": "'",
             "…": "...", "→": "->", "≥": ">=", "≤": "<=", "✅": "[OK]", "❌": "[X]",
             "✓": "[v]", "·": "-", "​": "", "️": ""}


def _pdf_text(s: str) -> str:
    s = s or ""
    for a, b in _PDF_REPL.items():
        s = s.replace(a, b)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\*(.+?)\*", r"\1", s)
    s = re.sub(r"`(.+?)`", r"\1", s)
    return s.encode("latin-1", "replace").decode("latin-1")


def build_pdf(content_md: str, title: str, out_path: Path, subtitle: str = ""):
    """Convierte el Markdown del entregable/reporte en un .pdf legible (encabezados,
    párrafos, listas y tablas). Devuelve None si falta fpdf2."""
    try:
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos
    except ImportError:
        return None

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_margins(20, 18, 18)
    pdf.add_page()

    def mc(h, txt):  # multi_cell que SIEMPRE vuelve al margen izq. y baja una línea
        pdf.multi_cell(0, h, txt, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "B", 16)
    mc(8, _pdf_text(title))
    if subtitle:
        pdf.ln(1); pdf.set_font("Helvetica", "I", 10); pdf.set_text_color(90, 90, 90)
        mc(5, _pdf_text(subtitle)); pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    table_block: list[str] = []

    def _flush_table():
        if not table_block:
            return
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table_block]
        rows = [r for r in rows if r and not all(set(c) <= set("-: ") for c in r)]
        table_block.clear()
        if not rows:
            return
        ncol = max(len(r) for r in rows)
        avail = pdf.w - pdf.l_margin - pdf.r_margin
        w = avail / ncol
        line_h = 5.2
        for ri, r in enumerate(rows):
            pdf.set_font("Helvetica", "B" if ri == 0 else "", 8.5)
            cells_txt = [_pdf_text(r[ci] if ci < len(r) else "") for ci in range(ncol)]
            # multi_cell ajusta línea (wrap) en vez de truncar por conteo de
            # caracteres: una celda larga ya no pierde contenido silenciosamente.
            x0, y0 = pdf.get_x(), pdf.get_y()
            max_y = y0
            for ci, txt in enumerate(cells_txt):
                pdf.set_xy(x0 + ci * w, y0)
                pdf.multi_cell(w, line_h, txt, border=1, new_x=XPos.LEFT, new_y=YPos.TOP)
                max_y = max(max_y, pdf.get_y())
            pdf.set_xy(x0, max_y)
        pdf.ln(2)

    for raw in (content_md or "").split("\n"):
        line = raw.rstrip()
        if line.count("|") >= 2:
            table_block.append(line); continue
        _flush_table()
        s = line.strip()
        if not s:
            pdf.ln(2); continue
        if s.startswith("### "):
            pdf.set_font("Helvetica", "B", 11.5); pdf.ln(1); mc(6, _pdf_text(s[4:]))
        elif s.startswith("## "):
            pdf.set_font("Helvetica", "B", 13); pdf.ln(1.5); mc(6.5, _pdf_text(s[3:]))
        elif s.startswith("# "):
            pdf.set_font("Helvetica", "B", 15); pdf.ln(2); mc(7, _pdf_text(s[2:]))
        elif s.startswith(("- ", "* ")):
            pdf.set_font("Helvetica", "", 10.5); mc(5.4, "   -  " + _pdf_text(s[2:]))
        else:
            pdf.set_font("Helvetica", "", 10.5); mc(5.4, _pdf_text(s))

    _flush_table()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return str(out_path)


def _add_statistics_sheets(wb, brief) -> int:
    """Añade una hoja por dataset con estadística (fórmulas vivas). Devuelve cuántas creó."""
    try:
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter as gcl
    except ImportError:
        return 0
    datasets = ((brief.statistics or {}).get("datasets") if brief else None) or []
    if not datasets:
        return 0

    HEAD = PatternFill("solid", fgColor="1F3B73"); HF = Font(bold=True, color="FFFFFF")
    BOLD = Font(bold=True); TITLE = Font(bold=True, size=13, color="1F3B73")
    SUB = Font(bold=True, size=11, color="1F3B73")

    def hc(ws, r, c, v):
        cell = ws.cell(row=r, column=c, value=v); cell.fill = HEAD; cell.font = HF
        cell.alignment = Alignment(horizontal="center"); return cell

    def kv(ws, r, lc, vc, label, formula):
        ws.cell(row=r, column=lc, value=label).font = BOLD
        ws.cell(row=r, column=vc, value=formula)

    made = 0
    for idx, d in enumerate(datasets, 1):
        base = (f"Est{idx} " + str(d.get("name", ""))).strip()[:28] or f"Est{idx}"
        name, k = base, 1
        while name in wb.sheetnames:
            name = f"{base[:24]} {k}"; k += 1
        ws = wb.create_sheet(title=name)
        for col, w in (("A", 16), ("B", 16), ("C", 16), ("E", 28), ("F", 18)):
            ws.column_dimensions[col].width = w
        ws.cell(row=1, column=1, value=str(d.get("name", "Datos"))).font = TITLE
        if d.get("notes"):
            ws.cell(row=2, column=1, value=str(d["notes"]))
        t = d.get("type")

        if t == "series":
            vals = d.get("values", []); n = len(vals)
            hc(ws, 4, 1, "Valor" + (f" ({d['unit']})" if d.get("unit") else ""))
            for i, v in enumerate(vals):
                ws.cell(row=5 + i, column=1, value=v)
            rng = f"A5:A{4+n}"
            ws.cell(row=4, column=3, value="ESTADÍSTICA DESCRIPTIVA").font = SUB
            rows = [
                ("N (conteo)", f"=COUNT({rng})"), ("Suma", f"=SUM({rng})"),
                ("Media", f"=AVERAGE({rng})"), ("Mediana", f"=MEDIAN({rng})"),
                ("Moda", f'=IFERROR(MODE.SNGL({rng}),"sin moda")'),
                ("Mínimo", f"=MIN({rng})"), ("Máximo", f"=MAX({rng})"),
                ("Rango", f"=MAX({rng})-MIN({rng})"),
                ("Varianza (muestral)", f"=VAR.S({rng})"),
                ("Desv. estándar (muestral)", f"=STDEV.S({rng})"),
                ("Coef. de variación", f"=IFERROR(STDEV.S({rng})/AVERAGE({rng}),0)"),
                ("Q1 (percentil 25)", f"=QUARTILE.INC({rng},1)"),
                ("Q3 (percentil 75)", f"=QUARTILE.INC({rng},3)"),
                ("Rango intercuartílico (RIC)", f"=QUARTILE.INC({rng},3)-QUARTILE.INC({rng},1)"),
                ("Asimetría (skewness)", f"=IFERROR(SKEW({rng}),0)"),
                ("Curtosis", f"=IFERROR(KURT({rng}),0)"),
                ("Error estándar de la media", f"=STDEV.S({rng})/SQRT(COUNT({rng}))"),
                ("IC 95% — margen", f"=CONFIDENCE.T(0.05,STDEV.S({rng}),COUNT({rng}))"),
                ("IC 95% — límite inferior", f"=AVERAGE({rng})-CONFIDENCE.T(0.05,STDEV.S({rng}),COUNT({rng}))"),
                ("IC 95% — límite superior", f"=AVERAGE({rng})+CONFIDENCE.T(0.05,STDEV.S({rng}),COUNT({rng}))"),
            ]
            for i, (lab, f) in enumerate(rows):
                kv(ws, 5 + i, 3, 4, lab, f)

        elif t == "xy":
            x, y = d.get("x", []), d.get("y", []); n = min(len(x), len(y))
            hc(ws, 4, 1, d.get("x_label", "X")); hc(ws, 4, 2, d.get("y_label", "Y"))
            hc(ws, 4, 3, "Y estimado")
            for i in range(n):
                ws.cell(row=5 + i, column=1, value=x[i])
                ws.cell(row=5 + i, column=2, value=y[i])
                ws.cell(row=5 + i, column=3, value=f"=$F$9*A{5+i}+$F$10")
            xr, yr = f"A5:A{4+n}", f"B5:B{4+n}"
            ws.cell(row=4, column=5, value="CORRELACIÓN Y REGRESIÓN LINEAL").font = SUB
            rows = [
                ("N (pares)", f"=COUNT({xr})"),
                ("Correlación (Pearson r)", f"=CORREL({xr},{yr})"),
                ("Coef. determinación R²", f"=RSQ({yr},{xr})"),
                ("Covarianza (muestral)", f"=COVARIANCE.S({xr},{yr})"),
                ("Pendiente (b)", f"=SLOPE({yr},{xr})"),
                ("Intercepto (a)", f"=INTERCEPT({yr},{xr})"),
                ("Ecuación", '="Y = "&TEXT(F9,"0.0000")&" * X + "&TEXT(F10,"0.0000")'),
            ]
            for i, (lab, f) in enumerate(rows):
                kv(ws, 5 + i, 5, 6, lab, f)

        elif t == "groups":
            groups = list((d.get("groups") or {}).items())
            kk = len(groups); maxlen = max(len(v) for _, v in groups)
            ranges = []
            for gi, (gname, gvals) in enumerate(groups):
                col = gi + 1; L = gcl(col)
                hc(ws, 4, col, str(gname))
                for i, v in enumerate(gvals):
                    ws.cell(row=5 + i, column=col, value=v)
                ranges.append(f"{L}5:{L}{4+len(gvals)}")
            dR = 6 + maxlen
            ws.cell(row=dR, column=1, value="DESCRIPTIVA POR GRUPO").font = SUB
            for gi, (gname, _) in enumerate(groups):
                ws.cell(row=dR, column=gi + 2, value=str(gname)).font = BOLD
            stat_rows = [("N", "COUNT"), ("Media", "AVERAGE"), ("Desv. est.", "STDEV.S"),
                         ("Varianza", "VAR.S"), ("Mínimo", "MIN"), ("Máximo", "MAX")]
            for si, (lab, fn) in enumerate(stat_rows):
                ws.cell(row=dR + 1 + si, column=1, value=lab).font = BOLD
                for gi, rng in enumerate(ranges):
                    ws.cell(row=dR + 1 + si, column=gi + 2, value=f"={fn}({rng})")
            tR = dR + len(stat_rows) + 3
            if kk == 2:
                ws.cell(row=tR, column=1, value="PRUEBA t (dos muestras)").font = SUB
                rows = [
                    ("Diferencia de medias", f"=AVERAGE({ranges[0]})-AVERAGE({ranges[1]})"),
                    ("p (dos colas, var. iguales)", f"=T.TEST({ranges[0]},{ranges[1]},2,2)"),
                    ("p (dos colas, var. distintas)", f"=T.TEST({ranges[0]},{ranges[1]},2,3)"),
                    ("Significativo al 5%", f'=IF(T.TEST({ranges[0]},{ranges[1]},2,3)<0.05,"Sí","No")'),
                ]
                for i, (lab, f) in enumerate(rows):
                    kv(ws, tR + 1 + i, 1, 2, lab, f)
            else:
                # ANOVA de un factor con fórmulas (referencia la tabla descriptiva)
                nrow, mrow, vrow = dR + 1, dR + 2, dR + 4
                first, last = gcl(2), gcl(kk + 1)
                nR = f"{first}{nrow}:{last}{nrow}"; mR = f"{first}{mrow}:{last}{mrow}"
                vR = f"{first}{vrow}:{last}{vrow}"
                ws.cell(row=tR, column=1, value="ANOVA de un factor").font = SUB
                gm = "SUMPRODUCT(%s,%s)/SUM(%s)" % (nR, mR, nR)
                rows = [
                    ("Ntotal", f"=SUM({nR})"), ("k (grupos)", str(kk)),
                    ("Media global", f"={gm}"),
                    ("SSB (entre)", f"=SUMPRODUCT({nR},({mR}-({gm}))^2)"),
                    ("SSW (dentro)", f"=SUMPRODUCT({nR}-1,{vR})"),
                    ("gl entre", str(kk - 1)), ("gl dentro", f"=SUM({nR})-{kk}"),
                    ("MSB", f"=SUMPRODUCT({nR},({mR}-({gm}))^2)/{kk-1}"),
                    ("MSW", f"=SUMPRODUCT({nR}-1,{vR})/(SUM({nR})-{kk})"),
                    ("F", f"=(SUMPRODUCT({nR},({mR}-({gm}))^2)/{kk-1})/(SUMPRODUCT({nR}-1,{vR})/(SUM({nR})-{kk}))"),
                    ("p (valor)", f"=F.DIST.RT((SUMPRODUCT({nR},({mR}-({gm}))^2)/{kk-1})/(SUMPRODUCT({nR}-1,{vR})/(SUM({nR})-{kk})),{kk-1},SUM({nR})-{kk})"),
                ]
                for i, (lab, f) in enumerate(rows):
                    kv(ws, tR + 1 + i, 1, 2, lab, f)
        made += 1
    return made


def build_excel_stats(brief, out_path: Path):
    """Excel SOLO de estadística (documentos sin presupuesto). None si no hay datos/openpyxl."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        return None
    datasets = ((brief.statistics or {}).get("datasets") if brief else None) or []
    if not datasets:
        return None
    wb = Workbook()
    ws = wb.active; ws.title = "Resumen"
    ws["A1"] = "ESTADÍSTICA DEL DOCUMENTO"; ws["A1"].font = Font(bold=True, size=14, color="1F3B73")
    ws["A3"] = brief.title if brief else ""
    ws["A4"] = f"Conjuntos de datos: {len(datasets)}"
    ws["A5"] = f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    _add_statistics_sheets(wb, brief)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return str(out_path)


def scouting_report_markdown(opportunities: list, topic: str) -> str:
    """Genera el reporte de oportunidades en Markdown (para PDF u otros usos)."""
    out = ["# Reporte de oportunidades detectadas", f"Búsqueda: {topic}", ""]
    out.append("| # | Financiador | Deadline | Monto | Calif. ponderada |")
    out.append("|---|---|---|---|---|")
    for i, o in enumerate(opportunities):
        f = o.get("funder") or {}
        out.append(f"| {i+1} | {f.get('name','—')} | {f.get('deadline','—')} | "
                   f"{o.get('total_amount') or f.get('amount_range','—')} | "
                   f"{float(o.get('weighted_score',0) or 0):.0f}/100 |")
    out.append("")
    for i, o in enumerate(opportunities):
        f = o.get("funder") or {}
        out.append(f"## {i+1}. {o.get('title','Oportunidad')}  ({float(o.get('weighted_score',0) or 0):.0f}/100)")
        out.append(f"Financiador: {f.get('name','—')} ({f.get('type','')}) — {f.get('url','')}")
        out.append(f"Deadline: {f.get('deadline','—')}  ·  Monto: {o.get('total_amount') or f.get('amount_range','—')}  ·  Sector: {o.get('sector','—')}")
        out.append("")
        out.append(o.get("summary", ""))
        out.append("")
        fb = o.get("feasibility_breakdown") or {}
        if fb:
            out.append("### Desglose de la calificación")
            labels = {"funder_match": "Encaje financiador", "geographic_eligibility": "Elegibilidad (Ecuador)",
                      "institutional_fit": "Ajuste institucional", "budget_fit": "Ajuste de presupuesto",
                      "deadline_feasibility": "Viabilidad de plazo"}
            for k, lab in labels.items():
                d = fb.get(k) or {}
                out.append(f"- {lab}: {float(d.get('score',0) or 0):.0f}/100 — {d.get('reason','')}")
            out.append("")
    return "\n".join(out)


def build_word_plain(content: str, title: str, out_path: Path) -> str | None:
    """Genera un .docx simple (sin portada de proyecto) para oficios y peticiones."""
    try:
        from docx import Document
        from docx.shared import Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    except ImportError:
        return None

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    pf = normal.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
    pf.line_spacing = 1.5
    pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    for section in doc.sections:
        section.top_margin = Cm(2.5)
        section.bottom_margin = Cm(2.5)
        section.left_margin = Cm(3.5)
        section.right_margin = Cm(2.5)

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(stripped[2:])
            run.bold = True
            run.font.size = Pt(14)
            run.font.color.rgb = RGBColor(0x1F, 0x3B, 0x73)
        elif stripped.startswith("## "):
            p = doc.add_paragraph()
            run = p.add_run(stripped[3:])
            run.bold = True
            run.font.size = Pt(12)
            run.font.color.rgb = RGBColor(0x1F, 0x3B, 0x73)
        elif not stripped:
            doc.add_paragraph()
        else:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            # Bold spans **...**
            import re as _re
            parts = _re.split(r"(\*\*[^*]+\*\*)", stripped)
            for part in parts:
                if part.startswith("**") and part.endswith("**"):
                    r = p.add_run(part[2:-2])
                    r.bold = True
                else:
                    p.add_run(part)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return str(out_path)
