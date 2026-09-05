"""Infraestructura de herramientas: cada herramienta es una función sobre DMVs
que el agente puede invocar. Se registran con el decorador `@herramienta`."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, get_type_hints

from sqlpilot.db.conexion import ConexionSql

_TIPOS_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass
class Herramienta:
    nombre: str
    descripcion: str
    categoria: str
    funcion: Callable[..., Any]
    parametros: dict[str, Any] = field(default_factory=dict)  # JSON Schema

    def invocar(self, conexion: ConexionSql, **argumentos: Any) -> Any:
        return self.funcion(conexion, **argumentos)


REGISTRO: dict[str, Herramienta] = {}


def _esquema_desde_firma(fn: Callable[..., Any], descripciones: dict[str, str]) -> dict[str, Any]:
    firma = inspect.signature(fn)
    hints = get_type_hints(fn)
    propiedades: dict[str, Any] = {}
    requeridos: list[str] = []
    for nombre, p in firma.parameters.items():
        if nombre == "conexion":
            continue
        tipo = hints.get(nombre, str)
        # Optional[X] -> X
        origen = getattr(tipo, "__args__", None)
        if origen:
            tipo = next((t for t in origen if t is not type(None)), str)
        prop: dict[str, Any] = {"type": _TIPOS_JSON.get(tipo, "string")}
        if nombre in descripciones:
            prop["description"] = descripciones[nombre]
        if p.default is not inspect.Parameter.empty:
            if p.default is not None:
                prop["default"] = p.default
        else:
            requeridos.append(nombre)
        propiedades[nombre] = prop
    esquema: dict[str, Any] = {"type": "object", "properties": propiedades}
    if requeridos:
        esquema["required"] = requeridos
    return esquema


def herramienta(_nombre: str, _descripcion: str, _categoria: str, **descripciones_parametros: str):
    """Registra una función `fn(conexion, ...)` como herramienta del agente.

    Los kwargs son descripciones de los parámetros de la función (por eso los
    posicionales llevan guion bajo: un parámetro puede llamarse `nombre`)."""

    def decorador(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRO[_nombre] = Herramienta(
            nombre=_nombre,
            descripcion=_descripcion.strip(),
            categoria=_categoria,
            funcion=fn,
            parametros=_esquema_desde_firma(fn, descripciones_parametros),
        )
        return fn

    return decorador


def obtener(nombre: str) -> Herramienta:
    if nombre not in REGISTRO:
        raise KeyError(f"Herramienta desconocida: {nombre}")
    return REGISTRO[nombre]


def listar(categoria: str | None = None) -> list[Herramienta]:
    return [h for h in REGISTRO.values() if categoria is None or h.categoria == categoria]
