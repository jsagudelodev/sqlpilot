"""Proveedor Anthropic (Claude) con tool use nativo."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from sqlpilot.herramientas.base import Herramienta
from sqlpilot.llm.base import ErrorLLM, LlamadaHerramienta, ProveedorLLM, RespuestaLLM


class ProveedorAnthropic(ProveedorLLM):
    nombre = "anthropic"

    def __init__(self, modelo: str, api_key: str | None = None, max_tokens: int = 16000, esfuerzo: str = "high"):
        super().__init__(modelo, max_tokens, esfuerzo)
        self._cliente = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    # ----- conversión de formatos ----------------------------------------
    @staticmethod
    def _herramientas(herramientas: list[Herramienta]) -> list[dict[str, Any]]:
        return [{"name": h.nombre, "description": h.descripcion, "input_schema": h.parametros} for h in herramientas]

    @staticmethod
    def _mensajes(mensajes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        salida: list[dict[str, Any]] = []
        pendientes_resultados: list[dict[str, Any]] = []

        def vaciar() -> None:
            nonlocal pendientes_resultados
            if pendientes_resultados:
                salida.append({"role": "user", "content": pendientes_resultados})
                pendientes_resultados = []

        for m in mensajes:
            rol = m["rol"]
            if rol == "usuario":
                vaciar()
                salida.append({"role": "user", "content": m["contenido"]})
            elif rol == "asistente":
                vaciar()
                if m.get("crudo") is not None:
                    salida.append({"role": "assistant", "content": m["crudo"]})
                else:
                    bloques: list[dict[str, Any]] = []
                    if m.get("contenido"):
                        bloques.append({"type": "text", "text": m["contenido"]})
                    for ll in m.get("llamadas", []):
                        bloques.append({"type": "tool_use", "id": ll["id"], "name": ll["nombre"], "input": ll["argumentos"]})
                    salida.append({"role": "assistant", "content": bloques or m.get("contenido", "")})
            elif rol == "herramienta":
                pendientes_resultados.append({
                    "type": "tool_result", "tool_use_id": m["id"], "content": m["contenido"], "is_error": bool(m.get("error")),
                })
        vaciar()
        return salida

    # ----- llamada ---------------------------------------------------------
    def completar(self, sistema: str, mensajes: list[dict[str, Any]], herramientas: list[Herramienta]) -> RespuestaLLM:
        try:
            with self._cliente.messages.stream(
                model=self.modelo,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": sistema, "cache_control": {"type": "ephemeral"}}],
                tools=self._herramientas(herramientas),
                messages=self._mensajes(mensajes),
                thinking={"type": "adaptive"},
                output_config={"effort": self.esfuerzo},
            ) as stream:
                respuesta = stream.get_final_message()
        except anthropic.AuthenticationError as ex:
            raise ErrorLLM("Credenciales de Anthropic inválidas. Define ANTHROPIC_API_KEY o ejecuta `ant auth login`.") from ex
        except anthropic.RateLimitError as ex:
            raise ErrorLLM("Límite de tasa de Anthropic alcanzado; reintenta en unos segundos.") from ex
        except anthropic.APIStatusError as ex:
            raise ErrorLLM(f"Error de la API de Anthropic ({ex.status_code}): {ex.message}") from ex
        except anthropic.APIConnectionError as ex:
            raise ErrorLLM(f"No se pudo conectar con Anthropic: {ex}") from ex

        if respuesta.stop_reason == "refusal":
            return RespuestaLLM(texto="El modelo declinó responder esta petición.", razon_parada="rechazo")

        texto_partes: list[str] = []
        llamadas: list[LlamadaHerramienta] = []
        crudo: list[dict[str, Any]] = []
        for bloque in respuesta.content:
            if bloque.type == "text":
                texto_partes.append(bloque.text)
                crudo.append({"type": "text", "text": bloque.text})
            elif bloque.type == "tool_use":
                entrada = bloque.input if isinstance(bloque.input, dict) else json.loads(json.dumps(bloque.input))
                llamadas.append(LlamadaHerramienta(id=bloque.id, nombre=bloque.name, argumentos=entrada))
                crudo.append({"type": "tool_use", "id": bloque.id, "name": bloque.name, "input": entrada})
            elif bloque.type in ("thinking", "redacted_thinking"):
                crudo.append(bloque.model_dump())
        razon = "herramientas" if respuesta.stop_reason == "tool_use" else ("max_tokens" if respuesta.stop_reason == "max_tokens" else "fin")
        return RespuestaLLM(
            texto="\n".join(texto_partes).strip(),
            llamadas=llamadas,
            razon_parada=razon,
            tokens_entrada=respuesta.usage.input_tokens,
            tokens_salida=respuesta.usage.output_tokens,
            crudo=crudo,
        )
