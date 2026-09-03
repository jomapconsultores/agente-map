# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""VIGÍA DE CONVOCATORIAS — búsqueda desatendida y entrega por correo.

El sistema encontraba buenas oportunidades pero solo cuando alguien se acordaba de
abrirlo y pedírselo. Este script invierte eso: corre solo (una tarea programada
semanal), busca sobre los temas de interés, descarta lo que ya se reportó antes y
manda un correo con lo nuevo. El trabajo del destinatario pasa de buscar a decidir.

Uso:
    python vigia.py                 # ciclo completo y envío
    python vigia.py --dry-run       # busca y muestra, sin enviar ni registrar
    python vigia.py --tema "agua segura rural"   # un solo tema, ignora la lista

Configuración (variables de entorno):
    VIGIA_TEMAS        temas separados por «;». Si falta, se usa vigia_temas.txt
                       y, si tampoco existe, la lista por defecto de este archivo.
    VIGIA_MAX          máximo de oportunidades en el correo (por defecto 5)
    VIGIA_TTL_DIAS     días que una convocatoria ya reportada no se repite (120)
    VIGIA_PANEL_URL    URL del panel, para el enlace de «abrir» de cada tarjeta
    VIGIA_SMTP_*       ver utils/mailer.py

Si el correo no está configurado, el reporte NO se pierde: se guarda en
`output/vigia/` y el script termina con código 2 para que la tarea programada lo
registre como incidencia en vez de fingir éxito.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import config
from utils import mailer

# Temas por defecto: los sectores donde estas entidades compiten bien, según
# perfil-map/PERFIL_MAP.md. Se sobrescriben con VIGIA_TEMAS o vigia_temas.txt.
TEMAS_POR_DEFECTO = [
    "desarrollo económico local y fortalecimiento de MIPYMES",
    "capacitación y formación profesional",
    "planificación territorial, urbana y gestión hídrica",
    "transformación digital e inteligencia artificial aplicada a la gestión pública",
    "emprendimiento e innovación para mujeres y jóvenes",
]

# El historial es lo único que hace que una convocatoria no se repita cada semana.
# En el contenedor `output/` no sobrevive a un redespliegue, así que la ruta es
# configurable para poder apuntarla a un directorio persistente (ver VIGIA.md).
HISTORIAL = Path(os.getenv("VIGIA_HISTORIAL", "").strip() or (config.OUTPUT_DIR / "vigia_historial.json"))
REPORTES_DIR = config.OUTPUT_DIR / "vigia"


# ── Temas ────────────────────────────────────────────────────────────────────
def temas() -> list[str]:
    env = os.getenv("VIGIA_TEMAS", "").strip()
    if env:
        return [t.strip() for t in env.split(";") if t.strip()]
    archivo = config.BASE_DIR / "vigia_temas.txt"
    if archivo.is_file():
        lineas = [l.strip() for l in archivo.read_text(encoding="utf-8").splitlines()]
        vivos = [l for l in lineas if l and not l.startswith("#")]
        if vivos:
            return vivos
    return list(TEMAS_POR_DEFECTO)


# ── Historial: no repetir la misma convocatoria cada semana ──────────────────
def _clave(o: dict) -> str:
    """Huella estable de una convocatoria.

    Se prefiere la URL normalizada (sin protocolo, www, barra final ni query),
    porque el título lo redacta el LLM y cambia de una corrida a otra. Sin URL
    utilizable, se cae al financiador + título normalizados.
    """
    url = ((o.get("funder") or {}).get("url") or "").strip().lower()
    if url.startswith("http"):
        url = re.sub(r"^https?://(www\.)?", "", url)
        url = url.split("?")[0].split("#")[0].rstrip("/")
        if url:
            return f"url:{url}"
    funder = ((o.get("funder") or {}).get("name") or "").strip().lower()
    titulo = re.sub(r"[^a-z0-9áéíóúñ ]+", "", (o.get("title") or "").strip().lower())
    return f"txt:{funder}|{' '.join(titulo.split())[:80]}"


def historial() -> dict:
    if not HISTORIAL.is_file():
        return {}
    try:
        data = json.loads(HISTORIAL.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}   # un historial corrupto no debe impedir el reporte de hoy


def guardar_historial(previo: dict, nuevas: list[dict]) -> None:
    ttl = int(os.getenv("VIGIA_TTL_DIAS", "120") or 120)
    corte = (date.today() - timedelta(days=ttl)).isoformat()
    # La purga evita que el archivo crezca sin fin y permite que una convocatoria
    # de ciclo anual vuelva a reportarse cuando se reabre.
    vigente = {k: v for k, v in previo.items() if str(v) >= corte}
    hoy = date.today().isoformat()
    for o in nuevas:
        vigente[_clave(o)] = hoy
    HISTORIAL.parent.mkdir(parents=True, exist_ok=True)
    HISTORIAL.write_text(json.dumps(vigente, ensure_ascii=False, indent=1), encoding="utf-8")


# ── Búsqueda ─────────────────────────────────────────────────────────────────
def buscar(tema: str) -> tuple[list[dict], str]:
    """Ejecuta el scouting de un tema. Devuelve (oportunidades, session_id).

    Se usa `pipeline.run_scouting` y no `scout.run` directamente para que cada
    corrida quede registrada como sesión: así el correo puede enlazar al panel,
    donde está el detalle completo y desde donde se lanza la propuesta.
    """
    from core import pipeline
    ses = pipeline.run_scouting(user_input=tema)
    opps = list((ses.analysis.alternatives if ses.analysis else None) or [])
    return opps, ses.session_id


# ── Correo ───────────────────────────────────────────────────────────────────
def _panel_url(session_id: str) -> str:
    base = os.getenv("VIGIA_PANEL_URL", "").strip().rstrip("/")
    return f"{base}/sesion/{session_id}" if base else ""


def _fmt_score(o: dict) -> str:
    return f"{float(o.get('weighted_score', 0) or 0):.0f}"


def es_accionable(o: dict) -> bool:
    """True si la oportunidad tiene una fecha de cierre verificada y vigente.

    Sin fecha no hay nada que decidir, y el agente puede construir una oportunidad
    verosímil a partir de una nota de prensa: en la primera corrida real devolvió
    71/100 sobre una noticia del PNUD sin convocatoria detrás. El score no protege
    contra eso —la calificación mide encaje, no existencia— pero la fecha sí: una
    convocatoria real publica cuándo cierra. Las demás no se tiran, van al final
    del correo como pistas, sin ocupar el espacio de lo accionable.
    """
    f = o.get("funder") or {}
    if (f.get("deadline_status") or "") in ("cerrada", "sin_fecha"):
        return False
    if f.get("deadline_iso"):
        return True
    # Sin verificación de red utilizable, se acepta el texto del LLM solo si
    # contiene una fecha parseable; "ciclo anual — verificar" no basta.
    from utils.deadline_checker import parse_deadline_text
    return parse_deadline_text(f.get("deadline") or "") is not None


def componer_html(items: list[tuple[dict, str, str]], sin_novedad: list[str],
                  pistas: list[tuple[dict, str, str]] | None = None) -> str:
    """items = [(oportunidad, tema, session_id)] ya ordenados de mejor a peor."""
    hoy = datetime.now().strftime("%d/%m/%Y")
    e = html.escape
    p: list[str] = [
        '<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#12181B;'
        'max-width:660px;margin:0 auto;padding:8px 4px 24px">',
        f'<p style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;'
        f'color:#6B7878;margin:0 0 6px">Vigía de convocatorias · {hoy}</p>',
        f'<h1 style="font-size:23px;line-height:1.2;margin:0 0 14px">'
        f'{len(items)} oportunidad(es) nueva(s) para revisar</h1>',
    ]
    for o, tema, sid in items:
        f = o.get("funder") or {}
        url = (f.get("url") or "").strip()
        panel = _panel_url(sid)
        estado = (f.get("deadline_label") or f.get("deadline") or "—")
        p.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="border:1px solid #D6DDD8;border-radius:4px;margin:0 0 14px">'
            '<tr><td style="padding:14px 16px">'
            f'<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;'
            f'color:#6B7878;margin-bottom:4px">{e(f.get("name") or "financiador por confirmar")}'
            f' · {e(tema)}</div>'
            f'<div style="font-size:17px;font-weight:600;line-height:1.3;margin-bottom:8px">'
            f'{e(o.get("title") or "Oportunidad")}</div>'
            f'<div style="font-size:13px;color:#3E4A4C;margin-bottom:9px">'
            f'<b>{_fmt_score(o)}/100</b> · Cierre: {e(str(estado))} · '
            f'Monto: {e(str(o.get("total_amount") or f.get("amount_range") or "—"))} · '
            f'Postula: {e(str(o.get("best_fit_org") or "—"))}</div>'
            f'<div style="font-size:14px;line-height:1.55;color:#12181B;margin-bottom:10px">'
            f'{e((o.get("summary") or "")[:420])}</div>'
        )
        enlaces = []
        if url.startswith("http"):
            enlaces.append(f'<a href="{e(url)}" style="color:#1D4E89">Bases de la convocatoria</a>')
        if panel:
            enlaces.append(f'<a href="{e(panel)}" style="color:#1D4E89">Abrir en el panel</a>')
        if enlaces:
            p.append(f'<div style="font-size:13px">{" &nbsp;·&nbsp; ".join(enlaces)}</div>')
        p.append('</td></tr></table>')

    if pistas:
        p.append(
            '<p style="font-size:12.5px;letter-spacing:.06em;text-transform:uppercase;'
            'color:#6B7878;margin:22px 0 6px">Pistas sin fecha de cierre confirmada</p>'
            '<p style="font-size:12.5px;color:#6B7878;margin:0 0 8px">No son convocatorias '
            'verificadas: son rastros que pueden acabar en una. Se listan por si vale la pena '
            'mirarlos, no para decidir sobre ellos.</p>'
        )
        for o, tema, _ in pistas:
            f = o.get("funder") or {}
            url = (f.get("url") or "").strip()
            titulo = e(o.get("title") or "Sin título")
            enlace = (f'<a href="{e(url)}" style="color:#1D4E89">{titulo}</a>'
                      if url.startswith("http") else titulo)
            p.append(f'<div style="font-size:13px;color:#3E4A4C;margin:0 0 5px">'
                     f'{enlace} <span style="color:#6B7878">· {e(f.get("name") or "—")}</span></div>')

    if sin_novedad:
        p.append(
            f'<p style="font-size:12.5px;color:#6B7878;margin:18px 0 0">'
            f'Sin novedades en: {e(", ".join(sin_novedad))}.</p>'
        )
    p.append(
        '<p style="font-size:12px;color:#6B7878;margin:20px 0 0;border-top:1px solid #D6DDD8;'
        'padding-top:12px">Solo se listan convocatorias con calificación ≥ 60/100, con Ecuador '
        'elegible y con la fecha de cierre verificada contra la página oficial. Las ya enviadas '
        'en reportes anteriores no se repiten.</p></div>'
    )
    return "\n".join(p)


def componer_texto(items: list[tuple[dict, str, str]]) -> str:
    lineas = [f"Vigía de convocatorias — {datetime.now().strftime('%d/%m/%Y')}", ""]
    for o, tema, _ in items:
        f = o.get("funder") or {}
        lineas += [
            f"[{_fmt_score(o)}/100] {o.get('title', 'Oportunidad')}",
            f"  {f.get('name', '—')} · cierre {f.get('deadline_label') or f.get('deadline') or '—'}"
            f" · {tema}",
            f"  {f.get('url', '')}",
            "",
        ]
    return "\n".join(lineas)


# ── Ciclo ────────────────────────────────────────────────────────────────────
def _consola_utf8() -> None:
    """La consola de Windows abre en cp1252 y cualquier «→» o tilde en un print
    aborta la corrida con UnicodeEncodeError — el proceso moría antes de buscar
    nada. En Linux/Docker ya es UTF-8; esto solo asegura el caso local. Se llama
    desde `ejecutar` y no solo desde `main` para que también proteja a quien
    importe el módulo (la API, una prueba) en vez de lanzarlo por línea de comandos.
    """
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def ejecutar(lista_temas: list[str], *, dry_run: bool = False, maximo: int = 5) -> int:
    _consola_utf8()
    previo = historial()
    encontrados: list[tuple[dict, str, str]] = []
    sin_novedad: list[str] = []
    fallidos: list[str] = []

    for tema in lista_temas:
        print(f"→ buscando: {tema}", flush=True)
        try:
            opps, sid = buscar(tema)
        except Exception as ex:  # un tema no debe tumbar el ciclo entero
            fallidos.append(f"{tema} ({type(ex).__name__}: {ex})")
            traceback.print_exc()
            continue
        nuevas = [o for o in opps if _clave(o) not in previo]
        print(f"   {len(opps)} encontradas · {len(nuevas)} nuevas · sesión {sid}", flush=True)
        if nuevas:
            encontrados += [(o, tema, sid) for o in nuevas]
        else:
            sin_novedad.append(tema)

    encontrados.sort(key=lambda t: float(t[0].get("weighted_score", 0) or 0), reverse=True)
    accionables = [t for t in encontrados if es_accionable(t[0])][:maximo]
    pistas = [t for t in encontrados if not es_accionable(t[0])][:5]

    if fallidos:
        print("temas con error: " + " | ".join(fallidos), file=sys.stderr)

    # Un correo semanal solo tiene sentido si trae algo sobre lo que decidir. Con
    # puras pistas no se escribe: la promesa del vigía es que, si llega un correo,
    # hay una convocatoria real esperando un sí o un no.
    if not accionables:
        print(f"sin convocatorias accionables ({len(pistas)} pista(s) sin fecha): no se envía correo")
        return 0

    asunto = f"Vigía MAP · {len(accionables)} oportunidad(es) nueva(s)"
    cuerpo_html = componer_html(accionables, sin_novedad, pistas)
    cuerpo_txt = componer_texto(accionables)

    if dry_run:
        print(cuerpo_txt)
        if pistas:
            print(f"({len(pistas)} pista(s) sin fecha de cierre, listadas aparte)")
        print("(dry-run: ni se envía ni se registra en el historial)")
        return 0

    REPORTES_DIR.mkdir(parents=True, exist_ok=True)
    copia = REPORTES_DIR / f"{date.today().isoformat()}.html"
    copia.write_text(cuerpo_html, encoding="utf-8")

    try:
        mailer.send_html(asunto, cuerpo_html, cuerpo_txt)
    except mailer.MailNotConfigured as ex:
        # El reporte ya está en disco; se sale con 2 para que la tarea programada
        # lo marque como incidencia en vez de dar el ciclo por bueno.
        print(f"NO ENVIADO — {ex}. Reporte guardado en {copia}", file=sys.stderr)
        guardar_historial(previo, [o for o, _, _ in accionables])
        return 2

    guardar_historial(previo, [o for o, _, _ in accionables])
    print(f"correo enviado · {len(accionables)} oportunidad(es) · copia en {copia}")
    return 0


def main() -> int:
    _consola_utf8()
    ap = argparse.ArgumentParser(description="Vigía de convocatorias con entrega por correo")
    ap.add_argument("--dry-run", action="store_true", help="no envía ni registra historial")
    ap.add_argument("--tema", action="append", help="tema concreto (repetible)")
    ap.add_argument("--max", type=int, default=int(os.getenv("VIGIA_MAX", "5") or 5))
    args = ap.parse_args()

    lista = args.tema or temas()
    print(f"vigía: {len(lista)} tema(s) · {mailer.describe()}", flush=True)
    return ejecutar(lista, dry_run=args.dry_run, maximo=args.max)


if __name__ == "__main__":
    raise SystemExit(main())
