"""
SISTEMA MULTIAGENTE DE PROPUESTAS DE FINANCIAMIENTO NO REEMBOLSABLE
Ecuador · Nacional e Internacional · CAF · BID · BM · UE · PNUD · GIZ · USAID

Flujo:
  Usuario → Agente 1 (Analista) → Agente 2 (Redactor) → Agente 3 (Revisor)
                                         ↑_____________________|  (ciclo hasta aprobar)
"""
import os
import sys
import uuid
from pathlib import Path

import config
from config import ANTHROPIC_API_KEY, MAX_REVIEW_CYCLES
from models.schemas import ProjectSession
from models.doc_types import DOC_TYPES, get_doc_type, list_doc_types
from utils import display
from utils.output import save_session
from tools.file_reader import read_file, list_supported_files
import agents.analyst as analyst
import agents.classifier as classifier
import agents.intake as intake
import agents.writer as writer
import agents.reviewer as reviewer
import agents.financial as financial
import agents.phase_review as phase_review
from utils import empresas as empresas_util
from utils import perfil as perfil_util


# ── Startup check ─────────────────────────────────────────────────────────
def check_setup() -> str:
    key = ANTHROPIC_API_KEY or os.getenv("ANTHROPIC_API_KEY", "")
    if not key or key.startswith("sk-ant-XXXXX"):
        display.setup_guide()
        sys.exit(0)
    return key


# ── Input menu ────────────────────────────────────────────────────────────
def select_mode() -> tuple[str, str]:
    """Returns (mode, user_input)"""
    display.console.print("[bold cyan]¿Cómo deseas ingresar el proyecto?[/bold cyan]\n")
    display.console.print("  [bold yellow]1[/bold yellow]  Buscar oportunidades automáticamente (el agente busca)")
    display.console.print("  [bold yellow]2[/bold yellow]  Escribir/pegar idea o propuesta en consola")
    display.console.print("  [bold yellow]3[/bold yellow]  Cargar archivo (.txt, .docx, .pdf)")
    display.console.print("  [bold yellow]0[/bold yellow]  Salir\n")

    while True:
        choice = input("  Selecciona [1/2/3/0]: ").strip()

        if choice == "0":
            display.console.print("[dim]Hasta luego.[/dim]")
            sys.exit(0)

        elif choice == "1":
            display.console.print()
            display.console.print(
                "[dim]Describe el área o tema que te interesa "
                "(ej: 'educación rural', 'cambio climático', 'emprendimiento mujeres'):[/dim]"
            )
            topic = input("  Tema: ").strip()
            if not topic:
                topic = "desarrollo sostenible Ecuador"
            return "search", topic

        elif choice == "2":
            display.console.print()
            display.console.print(
                "[dim]Pega o escribe tu idea/propuesta. "
                "Cuando termines, escribe una línea con solo '---' y presiona Enter:[/dim]\n"
            )
            lines = []
            while True:
                line = input()
                if line.strip() == "---":
                    break
                lines.append(line)
            text = "\n".join(lines).strip()
            if not text:
                display.error("No ingresaste ningún texto. Intenta de nuevo.")
                continue
            return "text", text

        elif choice == "3":
            return _file_mode()

        else:
            display.error("Opción inválida. Escribe 1, 2, 3 o 0.")


def _file_mode() -> tuple[str, str]:
    display.console.print()
    files = list_supported_files(".")
    if files:
        display.console.print("[dim]Archivos disponibles en el directorio actual:[/dim]")
        for i, f in enumerate(files, 1):
            display.console.print(f"  [yellow]{i}.[/yellow] {f}")
        display.console.print()

    while True:
        path = input("  Ruta del archivo (o número de la lista): ").strip()
        if not path:
            display.error("Ingresa una ruta válida.")
            continue

        # If user entered a number from the list
        if path.isdigit() and files:
            idx = int(path) - 1
            if 0 <= idx < len(files):
                path = files[idx]
            else:
                display.error("Número fuera de rango.")
                continue

        try:
            content = read_file(path)
            display.success(f"Archivo cargado: {path} ({len(content):,} caracteres)")
            return "file", content
        except (FileNotFoundError, ValueError, ImportError) as e:
            display.error(str(e))


def _pick_file(prompt_label: str):
    """Selector de archivo reutilizable. Devuelve (nombre, texto) o None si se cancela."""
    files = list_supported_files(".")
    if files:
        display.console.print("[dim]Archivos disponibles en el directorio actual:[/dim]")
        for i, f in enumerate(files, 1):
            display.console.print(f"  [yellow]{i}.[/yellow] {f}")
    path = input(f"  {prompt_label} (ruta, número, o Enter para omitir): ").strip()
    if not path:
        return None
    if path.isdigit() and files:
        idx = int(path) - 1
        if 0 <= idx < len(files):
            path = files[idx]
        else:
            display.error("Número fuera de rango.")
            return None
    try:
        content = read_file(path)
        from pathlib import Path as _P
        display.success(f"Cargado: {path} ({len(content):,} caracteres)")
        return (_P(path).name, content)
    except (FileNotFoundError, ValueError, ImportError) as e:
        display.error(str(e))
        return None


def select_doc_type(user_input: str, template_text: str, support_docs: list, api_key: str) -> str:
    """Menú de tipo de documento con detección automática + override manual."""
    display.console.print("\n[bold cyan]¿Qué tipo de documento quieres generar?[/bold cyan]\n")
    display.console.print("  [bold yellow]0[/bold yellow]  Detección automática (recomendado)")
    types = list_doc_types()
    for i, dt in enumerate(types, 1):
        display.console.print(f"  [bold yellow]{i}[/bold yellow]  {dt.name} [dim]— {dt.description}[/dim]")

    choice = input("\n  Selecciona [0 para auto]: ").strip()

    if choice and choice.isdigit() and 1 <= int(choice) <= len(types):
        key = types[int(choice) - 1].key
        display.info(f"Tipo seleccionado: [bold]{get_doc_type(key).name}[/bold]")
        return key

    # Detección automática
    with display.spinner("Clasificando el tipo de documento solicitado"):
        try:
            key = classifier.detect_type(user_input, template_text, support_docs, api_key)
        except Exception as e:
            display.warning(f"No se pudo autodetectar ({e}). Usando 'generico'.")
            key = "generico"
    display.info(f"Tipo detectado: [bold]{get_doc_type(key).name}[/bold]")
    confirm = input("  Enter para confirmar, o número de la lista para cambiar: ").strip()
    if confirm.isdigit() and 1 <= int(confirm) <= len(types):
        key = types[int(confirm) - 1].key
        display.info(f"Cambiado a: [bold]{get_doc_type(key).name}[/bold]")
    return key


def load_optional_inputs() -> tuple[str, list]:
    """Plantilla modelo OPCIONAL + documentos de apoyo (varios)."""
    template_text = ""
    support_docs = []

    display.console.print(
        "\n[dim]Puedes subir una PLANTILLA/MODELO opcional a imitar (no es obligatorio).[/dim]"
    )
    if input("  ¿Subir plantilla modelo? [s/N]: ").strip().lower() in ("s", "si", "sí", "y"):
        picked = _pick_file("Plantilla modelo")
        if picked:
            template_text = picked[1]

    display.console.print(
        "\n[dim]Puedes subir DOCUMENTOS DE APOYO (material fuente). Añade los que quieras.[/dim]"
    )
    if input("  ¿Subir documentos de apoyo? [s/N]: ").strip().lower() in ("s", "si", "sí", "y"):
        while True:
            picked = _pick_file("Documento de apoyo")
            if picked:
                support_docs.append(picked)
            if input("  ¿Añadir otro documento de apoyo? [s/N]: ").strip().lower() not in ("s", "si", "sí", "y"):
                break

    return template_text, support_docs


# ── Main pipeline ─────────────────────────────────────────────────────────
def run_pipeline(api_key: str):
    display.banner()

    mode, user_input = select_mode()
    template_text, support_docs = load_optional_inputs()
    doc_type_key = select_doc_type(user_input, template_text, support_docs, api_key)

    session = ProjectSession(
        session_id=str(uuid.uuid4())[:8],
        user_input=user_input,
        input_mode=mode,
        doc_type_key=doc_type_key,
        template_text=template_text,
        support_docs=support_docs,
    )

    doc_type = get_doc_type(doc_type_key)

    # ── FASE 0.5: INTAKE + CONTEXTO ORGANIZACIONAL ───────────────────────
    n_empresas = empresas_util.count()
    if n_empresas:
        display.info(f"Cargados {n_empresas} documentos organizacionales (Empresas/)")

    has_input_docs = bool(template_text or support_docs or
                          ("http" in (user_input or "").lower()))
    if has_input_docs:
        display.phase("FASE 0.5 — ANÁLISIS DE DOCUMENTOS DE ENTRADA", "📋")
        display.info("Analizando plantilla, documentos de apoyo y/o URLs...")
        with display.spinner("Agente Intake extrayendo secciones y requisitos"):
            try:
                session.intake_data = intake.analyze(session)
                sections_found = len(session.intake_data.get("required_sections") or [])
                constraints_found = len(session.intake_data.get("key_constraints") or [])
                urls_found = len(session.intake_data.get("url_contents") or [])
                display.success(
                    f"Intake: {sections_found} secciones, {constraints_found} restricciones"
                    + (f", {urls_found} URLs descargadas" if urls_found else "")
                )
            except Exception as e:
                display.warning(f"Intake no disponible: {e}")
                session.intake_data = {}

    # ── FASE 1: ANÁLISIS / BRIEF ──────────────────────────────────────────
    if doc_type.is_proposal:
        display.phase("FASE 1 — ANÁLISIS DE VIABILIDAD", "🔍")
        if mode == "search":
            display.info(f"Buscando oportunidades para: [bold]{user_input}[/bold]")
        else:
            display.info("Analizando propuesta/idea...")

        with display.spinner("Agente Analista trabajando (búsqueda profunda + análisis)"):
            try:
                result = analyst.run(session, api_key)
            except Exception as e:
                display.error(f"Error en el análisis: {e}")
                return

        session.analysis = result
        display.analysis_report(result)

        if not result.viable:
            display.no_go(result)
            return

        session.brief = classifier.analysis_to_brief(result, doc_type_key)
    else:
        display.phase(f"FASE 1 — DEFINICIÓN DEL ENTREGABLE ({doc_type.name})", "🔍")
        display.info("Clasificando, investigando formato/normativa y construyendo el brief...")
        with display.spinner("Agente Clasificador trabajando (búsqueda profunda + brief)"):
            try:
                session.brief = classifier.build_brief(session, doc_type_key, api_key)
            except Exception as e:
                display.error(f"Error construyendo el brief: {e}")
                return
        display.brief_report(session.brief)

    # Enriquecer el brief con requisitos del intake (secciones obligatorias, restricciones)
    if session.brief and session.intake_data:
        from core.pipeline import _enrich_brief_with_intake
        _enrich_brief_with_intake(session)
        n_extra = len(session.intake_data.get("required_sections") or [])
        if n_extra:
            display.info(f"Brief enriquecido con {n_extra} secciones del formulario de entrada.")

    display.console.print("\n[bold green]Definición completa. Procediendo a redacción...[/bold green]")
    input("\n  Presiona Enter para continuar → ")

    # ── FASES 2+3+3.5+GATE3: REDACCIÓN → REVISIÓN → FINANCIERO → PAQUETE (ciclo) ──
    corrections = []
    final_proposal = ""
    approved = False
    # Perfil maestro (identidad y catálogo) + documentos legales de Empresas/.
    empresas_ctx = "\n\n".join(
        b for b in (perfil_util.block(), empresas_util.context_block()) if b
    )

    for cycle in range(1, MAX_REVIEW_CYCLES + 1):
        session.current_cycle = cycle

        # FASE 2: Redacción
        display.phase(f"FASE 2.{cycle} — REDACCIÓN ({doc_type.name})", "✍️")
        if corrections:
            display.info(f"Aplicando {len(corrections)} correcciones del revisor...")
        else:
            display.info("Redactando el documento completo...")

        with display.spinner(f"Agente Redactor trabajando (ciclo {cycle})"):
            try:
                proposal = writer.run(session, corrections, api_key)
            except Exception as e:
                display.error(f"Error en redacción: {e}")
                break

        session.proposal_versions.append(proposal)
        final_proposal = proposal
        display.success(f"Propuesta redactada ({len(proposal):,} caracteres)")

        # FASE 3: Control de calidad (Claude, regla 90/90)
        display.phase(f"FASE 3.{cycle} — CONTROL DE CALIDAD (Claude)", "🔬")
        display.info("Evaluando propuesta con máximo rigor...")

        # Para los tipos de máxima exigencia (tesis, artículo científico, peer
        # review, TDR), core/pipeline.py (la ruta headless/API) ya usa consenso
        # de 2 revisores independientes (reviewer.run_dual) en vez de un solo
        # árbitro — main.py (CLI) hacía siempre reviewer.run() sin esa rama,
        # dejando el consenso dual inalcanzable desde esta vía de entrada.
        dual = bool(session.brief and session.brief.doc_type_key in config.SECOND_OPINION_DOC_TYPES)
        with display.spinner(f"Agente Revisor evaluando (ciclo {cycle})" +
                             (" — consenso de 2 revisores" if dual else "")):
            try:
                review = reviewer.run_dual(session, proposal, api_key) if dual \
                    else reviewer.run(session, proposal, api_key)
            except Exception as e:
                display.error(f"Error en revisión: {e}")
                break

        session.review_results.append(review)
        display.review_report(review, cycle)

        if not review.approved:
            corrections = review.corrections
            if cycle < MAX_REVIEW_CYCLES:
                display.warning(
                    f"Ciclo {cycle}: score {review.overall_score:.0f}/100. "
                    f"Enviando {len(corrections)} correcciones al redactor..."
                )
                continue
            else:
                display.warning(
                    f"Se alcanzaron los {MAX_REVIEW_CYCLES} ciclos. "
                    "Se guarda la mejor versión disponible."
                )
                break

        # Llegó aquí: Claude aprobó (≥90/90) → FASE 3.5 + GATE 3
        display.success(
            f"[bold green]¡Aprobado por Claude en ciclo {cycle}![/bold green] "
            f"Score: {review.overall_score:.0f}/100"
        )

        # FASE 3.5: Estructuración financiera
        if session.brief and session.brief.needs_budget_excel:
            display.phase(f"FASE 3.5.{cycle} — ESTRUCTURACIÓN FINANCIERA", "📊")
            display.info("Estructurando presupuesto, marco lógico y cronograma...")
            with display.spinner("Agente Financiero trabajando"):
                try:
                    session.financial = financial.run(session, proposal, api_key)
                    display.success(
                        f"Estructurados {len(session.financial.budget_items)} rubros y "
                        f"{len(session.financial.logframe_rows)} filas de marco lógico."
                    )
                except Exception as e:
                    display.warning(f"Excel no disponible: {e}")
                    session.financial = None
        else:
            session.financial = None

        # GATE 3: DeepSeek revisa el paquete completo
        display.phase(f"GATE 3.{cycle} — REVISIÓN PAQUETE COMPLETO (DeepSeek)", "🔎")
        display.info("Verificando cumplimiento de plantilla, datos reales y consistencia...")
        g3_passed = True
        with display.spinner("DeepSeek auditando el paquete completo"):
            try:
                g3 = phase_review.review_package(
                    brief=session.brief,
                    proposal=proposal,
                    financial=session.financial,
                    intake_data=getattr(session, "intake_data", {}),
                    empresas_context=empresas_ctx,
                )
                session.phase_reviews.append({"attempt": cycle, **g3})
                g3_passed = g3["passed"]
                score_color = "green" if g3_passed else "yellow"
                display.console.print(
                    f"  [{score_color}]Score paquete: {g3['score']:.0f}/100 "
                    f"({'PASA' if g3_passed else 'REQUIERE CORRECCIÓN'})[/{score_color}]"
                )
                if g3.get("critical"):
                    display.warning("Problemas críticos (DeepSeek):")
                    for c in g3["critical"][:5]:
                        display.console.print(f"    • {c}")
                    if g3_passed is False and g3.get("issues"):
                        corrections = corrections + [
                            f"[Gate3] {iss}" for iss in g3["issues"][:8]
                        ]
            except Exception as e:
                display.warning(f"Gate 3 no disponible: {e}")

        if not g3_passed and cycle < MAX_REVIEW_CYCLES:
            display.warning(f"Gate 3 falló. Ciclo {cycle} → rehaciendo con correcciones adicionales.")
            continue

        # Paquete completo aprobado
        approved = True
        break

    # ── GUARDAR RESULTADOS ────────────────────────────────────────────────
    session.final_proposal = final_proposal
    session.approved = approved

    display.phase("FASE 4 — GENERACIÓN DE DOCUMENTOS", "📄")
    if session.brief and session.brief.needs_budget_excel:
        display.info("Generando Word y Excel vinculados.")
    else:
        display.info("Generando Word con el formato exigido.")

    display.phase("GUARDANDO RESULTADOS", "💾")
    with display.spinner("Guardando documentos (MD, TXT, Word, Excel)"):
        output_path = save_session(session)

    if approved:
        display.approved_banner(output_path)
    else:
        display.max_cycles_warning(output_path)

    _show_final_summary(session, output_path)


def _show_final_summary(session: ProjectSession, output_path: str):
    display.console.print("\n[bold]ARCHIVOS GENERADOS:[/bold]")
    for f in sorted(Path(output_path).iterdir()):
        size = f.stat().st_size
        tag = ""
        if f.suffix == ".docx":
            tag = "  [bold green]← WORD (propuesta)[/bold green]"
        elif f.suffix == ".xlsx":
            tag = "  [bold green]← EXCEL (cálculos vinculados)[/bold green]"
        display.console.print(f"  [cyan]{f.name}[/cyan]  [dim]({size:,} bytes)[/dim]{tag}")

    if session.review_results:
        last = session.review_results[-1]
        color = "green" if last.overall_score >= 90 else "yellow"
        display.console.print(
            f"\n[bold]Score final:[/bold] [{color}]{last.overall_score:.0f}/100[/{color}] "
            f"[dim](umbral 90 global y 90 por elemento)[/dim]"
        )
        if last.failing_elements:
            display.console.print("[yellow]  Elementos por debajo de 90:[/yellow]")
            for e in last.failing_elements:
                display.console.print(f"    [yellow]•[/yellow] {e}")

    display.console.print(
        f"\n[dim]Ciclos completados: {session.current_cycle}/{MAX_REVIEW_CYCLES}[/dim]"
    )
    display.console.print(f"[dim]Sesión ID: {session.session_id}[/dim]\n")


# ── Entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        api_key = check_setup()
        run_pipeline(api_key)
    except KeyboardInterrupt:
        display.console.print("\n\n[dim]Sesión interrumpida por el usuario.[/dim]\n")
        sys.exit(0)
