"""Abstracción mínima de proveedor LLM con tool use, independiente del SDK.

Formato neutral de mensajes (lista de dicts):
  {"rol": "usuario",    "contenido": "texto"}
  {"rol": "asistente",  "contenido": "texto", "llamadas": [{"id", "nombre", "argumentos"}]}
  {"rol": "herramienta", "id": "...", "nombre": "...", "contenido": "json/texto", "error": bool}
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlpilot.herramientas.base import Herramienta


@dataclass
class LlamadaHerramienta:
    id: str
    nombre: str
    argumentos: dict[str, Any]


@dataclass
class RespuestaLLM:
    texto: str
    llamadas: list[LlamadaHerramienta] = field(default_factory=list)
    razon_parada: str = "fin"
    tokens_entrada: int = 0
    tokens_salida: int = 0
    crudo: Any = None  # bloques de contenido nativos para re-enviar al proveedor


class ProveedorLLM(ABC):
    nombre: str = "base"

    def __init__(self, modelo: str, max_tokens: int = 16000, esfuerzo: str = "high"):
        self.modelo = modelo
        self.max_tokens = max_tokens
        self.esfuerzo = esfuerzo

    @abstractmethod
    def completar(self, sistema: str, mensajes: list[dict[str, Any]], herramientas: list[Herramienta]) -> RespuestaLLM:
        """Una vuelta de conversación. Devuelve texto y/o llamadas a herramientas."""


class ErrorLLM(RuntimeError):
    pass
