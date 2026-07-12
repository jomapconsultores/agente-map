"""Despacho de trabajos largos del pipeline.

Dos modos, seleccionados por la variable de entorno ``REDIS_URL``:

- **Con REDIS_URL (recomendado en producción):** el trabajo se ENCOLA en una cola
  Redis que procesa un servicio ``worker`` separado (ver ``worker.py``). Así un
  redeploy/restart del servicio web NO mata el trabajo en curso — esa era la causa
  raíz de que las sesiones quedaran "interrumpidas" a mitad del pipeline.

- **Sin REDIS_URL (fallback, comportamiento histórico):** el trabajo corre en un
  hilo daemon del propio proceso web, desacoplado del ciclo de request para poder
  invocarlo también desde el arranque (auto-resume). Un redeploy sigue matándolo,
  pero la reconciliación + auto-resume al arrancar (api.main lifespan) lo recuperan.

El módulo degrada con elegancia: si ``rq``/``redis`` no están instalados o Redis no
responde, cae automáticamente al hilo daemon sin romper el pipeline.
"""
from __future__ import annotations

import os
import threading

# Techo de tiempo del job en la cola: debe superar el wall-clock del pipeline
# (config.MAX_PIPELINE_WALLCLOCK_SEC) con margen para setup/teardown.
_JOB_TIMEOUT_SEC = int(os.getenv("RQ_JOB_TIMEOUT", "5400"))  # 90 min

_queue = None
_queue_resolved = False


def _redis_url() -> str:
    return os.getenv("REDIS_URL", "").strip()


def _get_queue():
    """Devuelve la cola RQ (memoizada) o None si no hay Redis/rq disponible."""
    global _queue, _queue_resolved
    if _queue_resolved:
        return _queue
    _queue_resolved = True
    url = _redis_url()
    if not url:
        return None
    try:
        from redis import Redis
        from rq import Queue
        conn = Redis.from_url(url)
        conn.ping()  # falla rápido si Redis no responde → fallback a hilo
        _queue = Queue(os.getenv("RQ_QUEUE", "pipeline"), connection=conn,
                       default_timeout=_JOB_TIMEOUT_SEC)
    except Exception:
        _queue = None
    return _queue


def queue_enabled() -> bool:
    return _get_queue() is not None


def _run_in_thread(target, kwargs: dict) -> None:
    def _wrap():
        try:
            target(**kwargs)
        except Exception:
            pass  # el pipeline ya persiste su propio fallo vía _mark_failed
    threading.Thread(target=_wrap, daemon=True).start()


def _submit(target, kwargs: dict) -> str:
    q = _get_queue()
    if q is not None:
        try:
            q.enqueue_call(func=target, kwargs=kwargs, timeout=_JOB_TIMEOUT_SEC)
            return "queued"
        except Exception:
            pass  # Redis cayó entre el ping y el enqueue → degradar a hilo
    _run_in_thread(target, kwargs)
    return "thread"


def submit_pipeline(**kwargs) -> str:
    """Despacha core.pipeline.run_pipeline con los kwargs dados (encolado o hilo)."""
    from core.pipeline import run_pipeline
    return _submit(run_pipeline, kwargs)


def submit_scouting(**kwargs) -> str:
    """Despacha core.pipeline.run_scouting con los kwargs dados (encolado o hilo)."""
    from core.pipeline import run_scouting
    return _submit(run_scouting, kwargs)
