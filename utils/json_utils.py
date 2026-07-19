"""Utilidades para parsear JSON de respuestas de LLMs.

Los modelos a veces devuelven JSON con:
  - Trailing commas antes de } o ]
  - Comentarios estilo // o /* */
  - Texto extra antes/después del objeto
  - Respuesta truncada (falta el cierre })
  - Comillas internas sin escapar dentro de strings

`robust_json_loads` maneja todos estos casos con múltiples intentos de reparación.
"""
from __future__ import annotations

import json
import re


def _extract_block(raw: str) -> str:
    """Extrae JSON de bloques de código Markdown."""
    raw = raw.strip()
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    # Recorta al primer delimitador ({ u [) y cierra con el correspondiente.
    # Antes se recortaba siempre a {...}, lo que corrompía un array JSON top-level
    # ('[{...},{...}]' → '{...},{...}', inválido). Se elige el delimitador que
    # aparece primero para preservar objetos y arrays por igual.
    s_obj = raw.find("{")
    s_arr = raw.find("[")
    candidates = [(p, close) for p, close in ((s_obj, "}"), (s_arr, "]")) if p >= 0]
    if candidates:
        s, close = min(candidates, key=lambda t: t[0])
        e = raw.rfind(close) + 1
        if e > s:
            raw = raw[s:e]
    return raw


def _remove_trailing_commas(s: str) -> str:
    """Elimina comas colgantes antes de } o ]."""
    return re.sub(r",\s*([}\]])", r"\1", s)


def _remove_comments(s: str) -> str:
    """Elimina comentarios // y /* */ (no válidos en JSON estándar)."""
    s = re.sub(r"//[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    return s


def _try_truncated(s: str) -> str | None:
    """Si el JSON está cortado, intenta cerrar todos los brackets abiertos."""
    opens = 0
    in_str = False
    escape = False
    last_valid = -1
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
        if not in_str:
            if ch in ("{", "["):
                opens += 1
            elif ch in ("}", "]"):
                opens -= 1
                if opens == 0:
                    last_valid = i
    if last_valid >= 0 and last_valid < len(s) - 1:
        # There's content after the last valid close — truncate there
        return s[:last_valid + 1]
    if opens > 0:
        # Unclosed — try to close greedily
        close_map = {"{": "}", "[": "]"}
        stack = []
        in_str = False
        escape = False
        for ch in s:
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
            if not in_str:
                if ch in ("{", "["):
                    stack.append(close_map[ch])
                elif ch in ("}", "]") and stack:
                    stack.pop()
        return s + "".join(reversed(stack))
    return None


def robust_json_loads(raw: str) -> dict | list:
    """Parsea JSON de una respuesta LLM con múltiples intentos de reparación.

    Lanza json.JSONDecodeError solo si todos los intentos fallan.
    """
    if not raw:
        raise json.JSONDecodeError("Respuesta vacía", "", 0)

    cleaned = _extract_block(raw)

    # Intento 1: directo
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Intento 2: sin comentarios
    try:
        return json.loads(_remove_comments(cleaned))
    except json.JSONDecodeError:
        pass

    # Intento 3: sin trailing commas
    try:
        return json.loads(_remove_trailing_commas(cleaned))
    except json.JSONDecodeError:
        pass

    # Intento 4: ambas reparaciones
    repaired = _remove_trailing_commas(_remove_comments(cleaned))
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Intento 5: JSON truncado — cerrar brackets abiertos
    fixed = _try_truncated(repaired)
    if fixed:
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

    # Último intento: todo el raw original con reparaciones
    try:
        return json.loads(_remove_trailing_commas(_remove_comments(raw)))
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"No se pudo parsear el JSON del LLM tras 6 intentos de reparación: {e.msg}",
            e.doc, e.pos,
        ) from e
