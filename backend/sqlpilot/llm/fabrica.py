"""Construcción del proveedor LLM según configuración."""

from __future__ import annotations

from sqlpilot.config import ConfigLLM
from sqlpilot.llm.base import ProveedorLLM

MODELOS_DEFAULT = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-4.1",
    "gemini": "gemini-2.5-pro",
    "ollama": "qwen2.5-coder:14b",
}

# Endpoints compatibles con la API de OpenAI.
BASE_URL_DEFAULT = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "ollama": "http://localhost:11434/v1",
}


def crear_proveedor(cfg: ConfigLLM) -> ProveedorLLM:
    modelo = cfg.modelo or MODELOS_DEFAULT[cfg.proveedor]
    if cfg.proveedor == "anthropic":
        from sqlpilot.llm.anthropic_ import ProveedorAnthropic

        return ProveedorAnthropic(modelo, api_key=cfg.api_key, max_tokens=cfg.max_tokens, esfuerzo=cfg.esfuerzo)
    if cfg.proveedor in ("openai", "gemini", "ollama"):
        from sqlpilot.llm.openai_ import ProveedorOpenAI

        base_url = cfg.base_url or BASE_URL_DEFAULT.get(cfg.proveedor)
        return ProveedorOpenAI(modelo, api_key=cfg.api_key, base_url=base_url, max_tokens=cfg.max_tokens,
                               esfuerzo=cfg.esfuerzo, nombre=cfg.proveedor)
    raise ValueError(f"Proveedor LLM no soportado: {cfg.proveedor}")
