"""Bucle agéntico: LLM ↔ herramientas DMV sobre una conexión, con eventos para la UI/CLI."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlpilot.agente.prompts import construir_sistema
from sqlpilot.config import Configuracion
from sqlpilot.db.bitacora import registrar
from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas import REGISTRO, Herramienta
from sqlpilot.llm.base import ErrorLLM, ProveedorLLM


@dataclass
class Evento:
    tipo: str  # "texto" | "llamada" | "resultado" | "fin" | "error"
    datos: dict[str, Any] = field(default_factory=dict)


def _serializar_resultado(resultado: Any, max_caracteres: int) -> str:
    texto = json.dumps(resultado, ensure_ascii=False, default=str)
    if len(texto) <= max_caracteres:
        return texto
    # Recorte inteligente: si hay listas largas, se truncan avisando.
    if isinstance(resultado, dict):
        recortado = dict(resultado)
        for k, v in resultado.items():
            if isinstance(v, list) and len(v) > 5:
                recortado[k] = v[: max(5, len(v) // 3)]
                recortado[f"_{k}_omitidos"] = len(v) - len(recortado[k])
        texto = json.dumps(recortado, ensure_ascii=False, default=str)
    if len(texto) > max_caracteres:
        texto = texto[:max_caracteres] + f'... [truncado, {len(texto) - max_caracteres} caracteres más]'
    return texto


class AgenteDBA:
    def __init__(self, config: Configuracion, conexion: ConexionSql, proveedor: ProveedorLLM,
                 herramientas: list[Herramienta] | None = None):
        self.config = config
        self.conexion = conexion
        self.proveedor = proveedor
        self.herramientas = herramientas or list(REGISTRO.values())
        self.historial: list[dict[str, Any]] = []
        self.info = conexion.info_servidor()
        self.sistema = construir_sistema(self.info)
        self.tokens_entrada = 0
        self.tokens_salida = 0

    def reiniciar(self) -> None:
        self.historial.clear()

    def preguntar(self, pregunta: str) -> Iterator[Evento]:
        """Procesa una pregunta del DBA y emite eventos hasta la respuesta final."""
        self.historial.append({"rol": "usuario", "contenido": pregunta})
        registrar("pregunta", perfil=self.conexion.perfil.nombre, texto=pregunta)
        llamadas_hechas: set[str] = set()

        for iteracion in range(self.config.llm.max_iteraciones):
            try:
                respuesta = self.proveedor.completar(self.sistema, self.historial, self.herramientas)
            except ErrorLLM as ex:
                yield Evento("error", {"mensaje": str(ex)})
                return
            self.tokens_entrada += respuesta.tokens_entrada
            self.tokens_salida += respuesta.tokens_salida

            self.historial.append({
                "rol": "asistente",
                "contenido": respuesta.texto,
                "llamadas": [{"id": ll.id, "nombre": ll.nombre, "argumentos": ll.argumentos} for ll in respuesta.llamadas],
                "crudo": respuesta.crudo,
            })
            if respuesta.texto:
                yield Evento("texto", {"texto": respuesta.texto, "final": not respuesta.llamadas})

            if not respuesta.llamadas:
                yield Evento("fin", {"iteraciones": iteracion + 1, "tokens_entrada": self.tokens_entrada,
                                     "tokens_salida": self.tokens_salida, "razon": respuesta.razon_parada})
                return

            for ll in respuesta.llamadas:
                firma = f"{ll.nombre}:{json.dumps(ll.argumentos, sort_keys=True, default=str)}"
                yield Evento("llamada", {"id": ll.id, "nombre": ll.nombre, "argumentos": ll.argumentos})
                inicio = time.perf_counter()
                if firma in llamadas_hechas:
                    contenido = json.dumps({"error": "Ya ejecutaste esta herramienta con los mismos argumentos; usa el resultado anterior."})
                    error = True
                else:
                    llamadas_hechas.add(firma)
                    contenido, error = self._ejecutar(ll.nombre, ll.argumentos)
                ms = round((time.perf_counter() - inicio) * 1000)
                self.historial.append({"rol": "herramienta", "id": ll.id, "nombre": ll.nombre, "contenido": contenido, "error": error})
                yield Evento("resultado", {"id": ll.id, "nombre": ll.nombre, "tiempo_ms": ms, "error": error,
                                           "resumen": contenido[:300]})

        yield Evento("error", {"mensaje": f"Se alcanzó el máximo de {self.config.llm.max_iteraciones} iteraciones sin respuesta final."})

    def _ejecutar(self, nombre: str, argumentos: dict[str, Any]) -> tuple[str, bool]:
        herramienta = REGISTRO.get(nombre)
        if herramienta is None:
            return json.dumps({"error": f"Herramienta desconocida: {nombre}"}), True
        try:
            resultado = herramienta.invocar(self.conexion, **argumentos)
            registrar("herramienta", perfil=self.conexion.perfil.nombre, nombre=nombre, argumentos=argumentos)
            return _serializar_resultado(resultado, self.config.agente.max_caracteres_resultado), False
        except TypeError as ex:
            return json.dumps({"error": f"Argumentos inválidos para {nombre}: {ex}", "esquema": herramienta.parametros}), True
        except Exception as ex:
            registrar("herramienta_error", perfil=self.conexion.perfil.nombre, nombre=nombre, error=str(ex))
            return json.dumps({"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}), True


def ejecutar_herramienta_directa(conexion: ConexionSql, nombre: str, **argumentos: Any) -> Any:
    """Atajo para la CLI/API: invocar una herramienta sin pasar por el LLM."""
    return REGISTRO[nombre].invocar(conexion, **argumentos)
