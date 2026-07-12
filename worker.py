"""Worker RQ que procesa la cola de trabajos largos del pipeline.

Se ejecuta como un servicio SEPARADO del web (ver Dockerfile / Coolify):

    python worker.py

Al estar en su propio proceso, un redeploy/restart del servicio web ya NO mata
los trabajos en curso — esa era la causa raíz de las sesiones marcadas como
"interrumpidas — sin actividad". Requiere REDIS_URL; sin ella, el despacho cae al
modo hilo dentro del web (ver core.jobs) y este worker no es necesario.
"""
from __future__ import annotations

import os


def main() -> None:
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        raise SystemExit("REDIS_URL no configurada; el worker RQ requiere Redis.")
    from redis import Redis
    from rq import Queue, Worker

    conn = Redis.from_url(url)
    qname = os.getenv("RQ_QUEUE", "pipeline")
    print(f"[worker] escuchando cola '{qname}' en {url.split('@')[-1]}")
    worker = Worker([Queue(qname, connection=conn)], connection=conn)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
