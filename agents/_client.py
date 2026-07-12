"""Cliente Anthropic compartido con políticas de retry para tier free/bajo.

Los agentes hacen llamadas grandes (8–16 k tokens) muy seguidas; en planes con
~30 k tokens/min de input es fácil chocar el rate limit. Centralizamos aquí:

- max_retries acotado: el SDK ya implementa backoff exponencial y respeta
  el header Retry-After del servidor. Mantenemos margen para sobrevivir a
  saturación breve, PERO acotado para no exceder el watchdog de "sin actividad".
- timeout acotado: el wall-clock efectivo de una llamada es
  timeout × (max_retries + 1) porque el SDK reintenta los timeouts. Con los
  valores por defecto anteriores (600 s × 10) una sola llamada podía bloquear
  el hilo del pipeline hasta ~110 min — muy por encima del watchdog de 30 min
  (db.queries._STALE_RUNNING_MINUTES), que entonces marcaba la sesión como
  "interrumpida" aunque el proceso siguiera vivo. Se acota por debajo de ese
  umbral y se expone por variables de entorno.
"""
from __future__ import annotations

import os

import anthropic
import httpx  # dependencia transitiva del SDK Anthropic; necesaria para httpx.Timeout

# Techos por-llamada, configurables por entorno. Deben mantenerse MUY por debajo
# del watchdog de "sin actividad" (STALE_RUNNING_MINUTES=30 min): peor caso
# efectivo = DEFAULT_TIMEOUT_SEC × (DEFAULT_MAX_RETRIES + 1) = 240 × 4 = 960 s ≈ 16 min.
DEFAULT_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "3"))
DEFAULT_TIMEOUT_SEC = float(os.getenv("ANTHROPIC_TIMEOUT_SEC", "240"))  # 4 min


def make_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=api_key,
        max_retries=DEFAULT_MAX_RETRIES,
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SEC, connect=10.0),
    )
