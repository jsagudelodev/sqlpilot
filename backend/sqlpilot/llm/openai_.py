"""Proveedor OpenAI y compatibles (Ollama, LM Studio, vLLM...) vía Chat Completions con tools."""

from __future__ import annotations

import json
import os
from typing import Any

import openai

from sqlpilot.herramientas.base import Herramienta
from sqlpilot.llm.base import ErrorLLM, LlamadaHerramienta, ProveedorLLM, RespuestaLLM


class ProveedorOpenAI(ProveedorLLM):
    nombre = "openai"

    def __init__(self, modelo: str, api_key: str | None = None, base_url: str | None = None,
                 max_tokens: int = 16000, esfuerzo: str = "high", nombre: str = "openai"):
        super().__init__(modelo, max_tokens, esfuerzo)
        self.nombre = nombre
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        elif nombre == "gemini":
            kwargs["api_key"] = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not kwargs["api_key"]:
                raise ErrorLLM("Falta la API key de Gemini: define GEMINI_API_KEY (o api_key en [llm] de sqlpilot.toml).")
        elif nombre == "ollama":
            kwargs["api_key"] = "ollama"  # Ollama no valida la clave pero el SDK la exige
        if base_url:
            kwargs["base_url"] = base_url
        self._cliente = openai.OpenAI(**kwargs)

    @staticmethod
    def _herramientas(herramientas: list[Herramienta]) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": h.nombre, "description": h.descripcion, "parameters": h.parametros}}
            for h in herramientas
        ]

    @staticmethod
    def _mensajes(sistema: str, mensajes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        salida: list[dict[str, Any]] = [{"role": "system", "content": sistema}]
        for m in mensajes:
            rol = m["rol"]
            if rol == "usuario":
                salida.append({"role": "user", "content": m["contenido"]})
            elif rol == "asistente":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("contenido") or None}
                if m.get("llamadas"):
                    msg["tool_calls"] = [
                        {"id": ll["id"], "type": "function",
                         "function": {"name": ll["nombre"], "arguments": json.dumps(ll["argumentos"], ensure_ascii=False)}}
                        for ll in m["llamadas"]
                    ]
                salida.append(msg)
            elif rol == "herramienta":
                salida.append({"role": "tool", "tool_call_id": m["id"], "content": m["contenido"]})
        return salida

    def completar(self, sistema: str, mensajes: list[dict[str, Any]], herramientas: list[Herramienta]) -> RespuestaLLM:
        try:
            respuesta = self._cliente.chat.completions.create(
                model=self.modelo,
                messages=self._mensajes(sistema, mensajes),
                tools=self._herramientas(herramientas) or openai.NOT_GIVEN,
                max_tokens=self.max_tokens,
            )
        except openai.AuthenticationError as ex:
            raise ErrorLLM(f"Credenciales inválidas para {self.nombre}. Define OPENAI_API_KEY / GEMINI_API_KEY o SQLPILOT_LLM_API_KEY.") from ex
        except openai.APIConnectionError as ex:
            raise ErrorLLM(f"No se pudo conectar con {self.nombre} ({self._cliente.base_url}): {ex}") from ex
        except openai.APIStatusError as ex:
            raise ErrorLLM(f"Error de la API de {self.nombre} ({ex.status_code}): {ex.message}") from ex

        eleccion = respuesta.choices[0]
        llamadas: list[LlamadaHerramienta] = []
        for tc in eleccion.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            llamadas.append(LlamadaHerramienta(id=tc.id, nombre=tc.function.name, argumentos=args))
        razon = "herramientas" if llamadas else ("max_tokens" if eleccion.finish_reason == "length" else "fin")
        uso = respuesta.usage
        return RespuestaLLM(
            texto=(eleccion.message.content or "").strip(),
            llamadas=llamadas,
            razon_parada=razon,
            tokens_entrada=getattr(uso, "prompt_tokens", 0) or 0,
            tokens_salida=getattr(uso, "completion_tokens", 0) or 0,
        )
