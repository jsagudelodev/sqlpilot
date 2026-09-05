import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from sqlpilot.config import Configuracion, PerfilConexion
from sqlpilot.herramientas import REGISTRO
from sqlpilot.mcp_servidor import crear_servidor, recortar


def _config() -> Configuracion:
    return Configuracion(perfiles={"t": PerfilConexion(nombre="t", servidor="127.0.0.1,1", timeout_conexion=1)}, perfil_default="t")


def test_expone_todas_las_herramientas_mas_utilitarias():
    servidor = crear_servidor(_config())
    tools = asyncio.run(servidor.list_tools())
    nombres = {t.name for t in tools}
    assert set(REGISTRO) <= nombres
    assert {"listar_perfiles", "info_servidor", "catalogo_herramientas"} <= nombres
    assert all(t.annotations.read_only_hint for t in tools)


def test_esquema_conserva_parametros_y_agrega_perfil_y_recorte():
    servidor = crear_servidor(_config())
    tools = {t.name: t for t in asyncio.run(servidor.list_tools())}
    props = tools["detalle_sesion"].input_schema["properties"]
    assert {"session_id", "perfil", "max_caracteres"} <= set(props)
    assert tools["detalle_sesion"].input_schema["required"] == ["session_id"]
    assert tools["detalle_sesion"].output_schema is not None


def test_error_de_conexion_es_tool_error():
    servidor = crear_servidor(_config())
    with pytest.raises(ToolError):
        asyncio.run(servidor.call_tool("sesiones_activas", {"top": 1}))


def test_listar_perfiles_es_estructurado():
    servidor = crear_servidor(_config())
    r = asyncio.run(servidor.call_tool("listar_perfiles", {}))
    assert r.structured_content["default"] == "t"
    assert r.structured_content["perfiles"][0]["nombre"] == "t"


def test_prompts_y_recursos():
    servidor = crear_servidor(_config())
    assert {p.name for p in asyncio.run(servidor.list_prompts())} >= {"diagnostico_lentitud", "analizar_procedimiento", "revision_salud"}
    assert [str(x.uri) for x in asyncio.run(servidor.list_resources())] == ["sqlpilot://perfiles"]


def test_recortar_listas_largas():
    datos = {"total": 500, "filas": [{"i": i, "texto": "x" * 50} for i in range(500)]}
    r = recortar(datos, 3000)
    assert r["_recortado"] and r["_omitidos_filas"] > 0
    assert len(r["filas"]) + r["_omitidos_filas"] == 500
    assert r["total"] == 500


def test_recortar_no_toca_lo_pequeno():
    datos = {"a": [1, 2, 3]}
    assert recortar(datos, 3000) == datos
