"""Cliente Supabase con resiliencia ante desconexiones transitorias.

Supabase/PostgREST cierra conexiones keep-alive ociosas; la siguiente petición
sobre esa conexión muerta del pool levanta `httpx.RemoteProtocolError: Server
disconnected`. Como el cliente está memoizado (reusa el pool), el error se repite
hasta que el pool se recrea. Eso hacía fallar en silencio los latidos de progreso
(update_progress) y los save_session del pipeline → el watchdog marcaba la sesión
"sin actividad" aunque el proceso siguiera vivo.

`run_with_retry` reintenta la operación descartando el cliente (pool nuevo) ante
cualquier error de transporte de httpx, de modo que un corte transitorio no tumbe
la persistencia del estado.
"""
import time
from functools import lru_cache

import httpx
from supabase import Client, create_client

import config

# Errores de transporte de httpx (desconexión, timeouts de red, protocolo). NO
# incluye HTTPStatusError (4xx/5xx con respuesta), que no se debe reintentar aquí.
_TRANSPORT_ERRORS = (httpx.TransportError,)


@lru_cache(maxsize=2)
def _make_client(service_role: bool) -> Client:
    if not config.SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not set in .env")
    key = config.SUPABASE_SECRET_KEY if service_role else config.SUPABASE_PUBLISHABLE_KEY
    if not key:
        which = "SUPABASE_SECRET_KEY" if service_role else "SUPABASE_PUBLISHABLE_KEY"
        raise RuntimeError(f"{which} is not set in .env")
    return create_client(config.SUPABASE_URL, key)


def get_client(*, service_role: bool = False) -> Client:
    """Return a cached Supabase client.

    service_role=True uses the secret key (bypasses RLS) — only for trusted,
    server-side operations. Default uses the publishable key.
    """
    return _make_client(service_role)


def reset_client() -> None:
    """Descarta los clientes memoizados para forzar un pool de conexiones nuevo.
    Se llama tras una desconexión para que el próximo get_client no reuse conexiones
    muertas."""
    _make_client.cache_clear()


def run_with_retry(fn, *, retries: int = 3, base_delay: float = 0.5):
    """Ejecuta `fn` (callable sin argumentos) reintentando ante desconexiones
    transitorias de Supabase, recreando el cliente entre intentos. Repropaga el
    último error si se agotan los reintentos. Errores NO de transporte (p. ej. un
    4xx de PostgREST) se propagan de inmediato sin reintentar."""
    last = None
    for attempt in range(retries):
        try:
            return fn()
        except _TRANSPORT_ERRORS as e:
            last = e
            reset_client()  # el pool tiene conexiones muertas: recrear
            if attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last
