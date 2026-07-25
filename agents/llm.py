# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""Capa LLM multi-proveedor.

Roles del sistema:
- **Corrector / verificador** (revisor, analista, clasificador) → **Anthropic (Claude)**,
  porque usan el tool-use de búsqueda web propio del SDK de Anthropic.
- **Constructores** (redactor, financiero) → **Mistral, Codestral y DeepSeek** en
  rotación por ciclo (ver `config.builder_for_cycle`). Claude dice qué corregir y
  el constructor del ciclo siguiente aplica las correcciones.

Mistral, Codestral y DeepSeek exponen una API compatible con OpenAI
(`POST {base}/chat/completions`), así que se manejan con un único cliente HTTP
basado en `urllib` (sin dependencias nuevas). Anthropic usa su SDK propio.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import config

# proveedor → (base_url, modelo por defecto, getter de api key)
_OPENAI_COMPAT = {
    "mistral": (lambda: config.MISTRAL_BASE_URL, lambda: config.MISTRAL_MODEL,
                lambda: config.MISTRAL_API_KEY),
    "codestral": (lambda: config.CODESTRAL_BASE_URL, lambda: config.CODESTRAL_MODEL,
                  lambda: config.CODESTRAL_API_KEY),
    "deepseek": (lambda: config.DEEPSEEK_BASE_URL, lambda: config.DEEPSEEK_MODEL,
                 lambda: config.DEEPSEEK_API_KEY),
}

_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))  # antes 6; acotado por el watchdog
_BACKOFF_BASE = 4.0  # segundos
_BACKOFF_CAP = 30.0  # máximo 30s por reintento (antes llegaba a 128s)
_RETRYABLE = (429, 500, 502, 503, 504)


class LLMError(RuntimeError):
    pass


def _builder_key(provider: str) -> str:
    spec = _OPENAI_COMPAT.get(provider)
    return spec[2]() if spec else ""


def _post_openai(base_url: str, model: str, api_key: str, system: str,
                 prompt: str, max_tokens: int, temperature: float) -> str:
    if not api_key:
        raise LLMError(f"API key faltante para {base_url}")
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT_SEC) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices") or []
            if not choices:
                raise LLMError(f"Respuesta sin 'choices' desde {url}: {str(data)[:300]}")
            content = choices[0]["message"]["content"]
            if not content:
                finish = choices[0].get("finish_reason", "?")
                raise LLMError(f"Contenido vacío desde {url} (finish_reason={finish!r})")
            return content
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last_err = LLMError(f"HTTP {e.code} desde {url}: {detail}")
            if e.code in _RETRYABLE and attempt < _MAX_RETRIES - 1:
                ra = e.headers.get("Retry-After")
                wait = float(ra) if (ra and ra.isdigit()) else _BACKOFF_BASE * (2 ** attempt)
                time.sleep(min(wait, _BACKOFF_CAP))
                continue
            raise last_err
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = LLMError(f"Error de red hacia {url}: {e}")
            if attempt < _MAX_RETRIES - 1:
                time.sleep(min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP))
                continue
            raise last_err
    raise last_err or LLMError(f"Fallo desconocido contra {url}")


def _complete_anthropic(system: str, prompt: str, max_tokens: int,
                        api_key: str | None, model: str | None) -> str:
    from agents._client import make_client
    client = make_client(api_key or config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=model or config.MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    # Extracción defensiva: Claude puede devolver bloques no-text (p. ej. tool_use)
    # o un content vacío según stop_reason. Leer content[0].text a ciegas lanzaba
    # IndexError/AttributeError, que tumbaba la fase con una excepción cruda en vez
    # de dejar que complete_builder rotara al siguiente proveedor.
    text = next((b.text for b in resp.content
                 if getattr(b, "type", None) == "text" and getattr(b, "text", "")), "")
    if not text:
        raise LLMError(f"Claude sin texto (stop_reason={getattr(resp, 'stop_reason', '?')!r})")
    return text


def complete(provider: str, *, system: str, prompt: str, max_tokens: int,
             api_key: str | None = None, model: str | None = None,
             temperature: float = 0.4) -> str:
    """Una sola llamada al proveedor indicado. Lanza LLMError si falla."""
    provider = (provider or "anthropic").lower()
    if provider == "anthropic":
        return _complete_anthropic(system, prompt, max_tokens, api_key, model)
    spec = _OPENAI_COMPAT.get(provider)
    if spec:
        base_url, default_model, key = spec[0](), spec[1](), spec[2]()
        return _post_openai(base_url, model or default_model, api_key or key,
                            system, prompt, max_tokens, temperature)
    raise LLMError(f"Proveedor desconocido: {provider!r}")


def complete_builder(provider: str, *, system: str, prompt: str, max_tokens: int,
                     anthropic_key: str | None = None,
                     temperature: float = 0.4) -> tuple[str, str]:
    """Constructor con rotación + fallback resiliente.

    Intenta primero el proveedor que toca por rotación. Si su key falta o la
    llamada falla, prueba los demás constructores y, como último recurso,
    Anthropic, para que el pipeline no se caiga por un proveedor indisponible.

    Devuelve (texto, proveedor_que_respondió).
    """
    order = [provider] + [p for p in config.BUILDER_ROTATION if p != provider]
    errors: list[str] = []
    for p in order:
        if not _builder_key(p):
            errors.append(f"{p}: sin API key")
            continue
        try:
            return complete(p, system=system, prompt=prompt,
                            max_tokens=max_tokens, temperature=temperature), p
        except LLMError as e:
            errors.append(f"{p}: {e}")

    # Último recurso: Claude (el corrector) también puede construir.
    if anthropic_key or config.ANTHROPIC_API_KEY:
        try:
            return _complete_anthropic(system, prompt, max_tokens, anthropic_key, None), "anthropic"
        except Exception as e:  # noqa: BLE001 - degradación controlada
            errors.append(f"anthropic: {e}")

    raise LLMError("Todos los constructores fallaron → " + " | ".join(errors))
