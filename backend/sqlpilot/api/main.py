"""API HTTP de SQLPilot para el frontend web."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sqlpilot import __version__
from sqlpilot.agente.agente import AgenteDBA
from sqlpilot.config import Configuracion, cargar_configuracion
from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas import REGISTRO, listar
from sqlpilot.herramientas.sql_libre import ejecutar_sql_lectura
from sqlpilot.llm.fabrica import crear_proveedor

app = FastAPI(title="SQLPilot API", version=__version__)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_config: Configuracion = cargar_configuracion()
_sesiones: dict[str, AgenteDBA] = {}


def _conexion(perfil: str | None) -> ConexionSql:
    try:
        return ConexionSql(_config.perfil(perfil)).abrir()
    except Exception as ex:
        raise HTTPException(400, f"No se pudo conectar: {ex}") from ex


# ----- modelos ----------------------------------------------------------------
class PeticionSql(BaseModel):
    sql: str
    max_filas: int = 100


class PeticionHerramienta(BaseModel):
    argumentos: dict[str, Any] = {}


class PeticionChat(BaseModel):
    pregunta: str
    sesion_id: str | None = None


# ----- rutas --------------------------------------------------------------------
@app.get("/api/v1/perfiles")
def perfiles() -> list[dict[str, Any]]:
    return [{"nombre": n, "servidor": p.servidor, "base_datos": p.base_datos, "autenticacion": p.autenticacion,
             "default": n == _config.perfil_default} for n, p in _config.perfiles.items()]


@app.get("/api/v1/salud")
def salud(perfil: str | None = Query(None)) -> dict[str, Any]:
    with _conexion(perfil) as c:
        return {"estado": "ok", **c.info_servidor()}


@app.get("/api/v1/herramientas")
def herramientas() -> list[dict[str, Any]]:
    return [{"nombre": h.nombre, "categoria": h.categoria, "descripcion": " ".join(h.descripcion.split()), "parametros": h.parametros}
            for h in listar()]


@app.post("/api/v1/herramientas/{nombre}")
def ejecutar_herramienta(nombre: str, peticion: PeticionHerramienta, perfil: str | None = Query(None)) -> Any:
    if nombre not in REGISTRO:
        raise HTTPException(404, f"Herramienta desconocida: {nombre}")
    with _conexion(perfil) as c:
        try:
            return REGISTRO[nombre].invocar(c, **peticion.argumentos)
        except TypeError as ex:
            raise HTTPException(422, f"Argumentos inválidos: {ex}") from ex
        except Exception as ex:
            raise HTTPException(500, str(ex)) from ex


@app.post("/api/v1/sql")
def ejecutar_sql(peticion: PeticionSql, perfil: str | None = Query(None)) -> Any:
    with _conexion(perfil) as c:
        resultado = ejecutar_sql_lectura(c, peticion.sql, peticion.max_filas)
    if "error" in resultado:
        raise HTTPException(400, resultado["error"])
    return resultado


@app.get("/api/v1/llm")
def llm() -> dict[str, Any]:
    return {"proveedor": _config.llm.proveedor, "modelo": _config.llm.modelo, "esfuerzo": _config.llm.esfuerzo}


@app.post("/api/v1/chat")
def chat(peticion: PeticionChat, perfil: str | None = Query(None)) -> StreamingResponse:
    """Chat con el agente; respuesta como Server-Sent Events con eventos texto/llamada/resultado/fin/error."""
    sesion_id = peticion.sesion_id or str(uuid.uuid4())
    agente = _sesiones.get(sesion_id)
    if agente is None:
        conexion = _conexion(perfil)
        try:
            proveedor = crear_proveedor(_config.llm)
        except Exception as ex:
            raise HTTPException(500, f"No se pudo inicializar el LLM: {ex}") from ex
        agente = AgenteDBA(_config, conexion, proveedor)
        _sesiones[sesion_id] = agente

    def generar():
        yield f"event: sesion\ndata: {json.dumps({'sesion_id': sesion_id})}\n\n"
        for ev in agente.preguntar(peticion.pregunta):
            yield f"event: {ev.tipo}\ndata: {json.dumps(ev.datos, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(generar(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.delete("/api/v1/chat/{sesion_id}")
def cerrar_chat(sesion_id: str) -> dict[str, bool]:
    agente = _sesiones.pop(sesion_id, None)
    if agente:
        agente.conexion.cerrar()
    return {"cerrada": agente is not None}
