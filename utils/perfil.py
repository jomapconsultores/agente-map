# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""Carga el PERFIL MAESTRO (PERFIL_MAP.md) como fuente de verdad del perfil.

Antes, el perfil institucional vivía DUPLICADO en tres sitios: el texto
`_ORG_PROFILE` incrustado en `agents/scout.py`, los documentos de `Empresas/`
y la tabla `products` del sistema de marketing. Cambiar un precio o añadir una
entidad obligaba a recordar los tres — y en la práctica se actualizaba uno solo.

Ahora el archivo canónico es uno:

    Dropbox/06_DESARROLLO/perfil-map/PERFIL_MAP.md

Como el despliegue del contenedor solo lleva consigo este repositorio, se guarda
una copia en `perfil/PERFIL_MAP.md` que `sync()` refresca desde el canónico. La
copia es un artefacto de despliegue: se edita el canónico, nunca la copia.

Orden de búsqueda:
  1. $PERFIL_MAP_PATH            (permite apuntarlo donde sea en el servidor)
  2. <repo>/perfil/PERFIL_MAP.md (copia desplegable — la que existe en Docker)
  3. <repo>/../perfil-map/PERFIL_MAP.md (el canónico, en el equipo local)
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

import config

FILENAME = "PERFIL_MAP.md"

# Copia que viaja dentro del repositorio (y por tanto dentro de la imagen Docker).
LOCAL_COPY = config.BASE_DIR / "perfil" / FILENAME

# Archivo canónico en el disco de trabajo, hermano del repositorio.
CANONICAL = config.BASE_DIR.parent / "perfil-map" / FILENAME

# Recorte por defecto al insertarlo en un prompt. El perfil completo ronda los
# 7 KB; el bloque de `Empresas/` ya consume 3 KB del contexto del scout.
MAX_CHARS = 6000


def path() -> Path | None:
    """Primera ruta existente del perfil, o None si no hay ninguna."""
    env = os.getenv("PERFIL_MAP_PATH", "").strip()
    candidates = ([Path(env)] if env else []) + [LOCAL_COPY, CANONICAL]
    for p in candidates:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


@lru_cache(maxsize=1)
def load() -> str:
    """Contenido del perfil, o cadena vacía si no se encuentra."""
    p = path()
    if p is None:
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def block(max_chars: int = MAX_CHARS) -> str:
    """Bloque listo para insertar en un prompt. Vacío si no hay perfil."""
    text = load().strip()
    if not text:
        return ""
    return "\n".join([
        "═══════════════════════════════════════════════════════════",
        "PERFIL MAESTRO — fuente de verdad de identidad y catálogo",
        "Usa SOLO estos datos para identificar a la organización que",
        "postula, su catálogo y sus diferenciadores. No inventes ni",
        "adornes: lo que no esté aquí, no se afirma.",
        "═══════════════════════════════════════════════════════════",
        text[:max_chars],
    ])


def sync() -> str:
    """Refresca la copia desplegable desde el canónico. Devuelve qué hizo.

    Pensado para ejecutarse antes de un despliegue. Nunca copia en sentido
    contrario: el canónico manda siempre.
    """
    if not CANONICAL.is_file():
        return f"sin canónico en {CANONICAL}: no hay nada que sincronizar"
    LOCAL_COPY.parent.mkdir(parents=True, exist_ok=True)
    if LOCAL_COPY.is_file():
        if LOCAL_COPY.read_bytes() == CANONICAL.read_bytes():
            return "la copia ya está al día"
    shutil.copy2(CANONICAL, LOCAL_COPY)
    load.cache_clear()
    return f"copia actualizada: {LOCAL_COPY}"


if __name__ == "__main__":  # python -m utils.perfil
    print(sync())
    p = path()
    print(f"perfil activo: {p or 'NINGUNO'} ({len(load())} caracteres)")
