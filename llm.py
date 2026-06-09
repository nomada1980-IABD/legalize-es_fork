"""
Capa de abstracción del proveedor de IA.

Permite usar la API de Anthropic (de pago, en la nube) o un modelo local
servido por llama.cpp (gratis, en la GPU, datos privados) según la variable
de entorno LLM_PROVIDER:

    LLM_PROVIDER=anthropic   -> usa ANTHROPIC_API_KEY (por defecto)
    LLM_PROVIDER=local       -> usa el servidor OpenAI-compatible de llama.cpp

Variables para el modo local:
    LOCAL_LLM_URL    (por defecto http://127.0.0.1:8080/v1)
    LOCAL_LLM_MODEL  (por defecto "local"; llama.cpp ignora el nombre)

Ambos backends exponen la misma función `complete(system, user)` que devuelve
texto o None si no hay backend disponible.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional


def provider() -> str:
    return os.environ.get("LLM_PROVIDER", "anthropic").lower()


def available() -> bool:
    """¿Hay un backend de IA utilizable según la configuración actual?"""
    if provider() == "local":
        return True  # el servidor local puede estar o no levantado; se comprueba al llamar
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def complete(system: str, user: str, max_tokens: int = 1500) -> Optional[str]:
    """Devuelve la respuesta del modelo como texto, o None si falla/indisponible."""
    if provider() == "local":
        return _complete_local(system, user, max_tokens)
    return _complete_anthropic(system, user, max_tokens)


# ---------------------------------------------------------------------------
# Backend: Anthropic (nube)
# ---------------------------------------------------------------------------

def _complete_anthropic(system: str, user: str, max_tokens: int) -> Optional[str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8"),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text
    except Exception as exc:
        return f"⚠️ Error al consultar la IA (Anthropic): {exc}"


# ---------------------------------------------------------------------------
# Backend: llama.cpp local (OpenAI-compatible)
# ---------------------------------------------------------------------------

def _complete_local(system: str, user: str, max_tokens: int) -> Optional[str]:
    base = os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1").rstrip("/")
    model = os.environ.get("LOCAL_LLM_MODEL", "local")
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except urllib.error.URLError as exc:
        return ("⚠️ No se pudo conectar con el modelo local. ¿Está arrancado "
                f"llama-server en {base}? Detalle: {exc}")
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        return f"⚠️ Respuesta inesperada del modelo local: {exc}"
