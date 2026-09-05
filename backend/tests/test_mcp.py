import asyncio
import json

from sqlpilot.config import Configuracion, PerfilConexion
from sqlpilot.herramientas import REGISTRO
from sqlpilot.mcp_servidor import crear_servidor


def _config() -> Configuracion:
    return Configuracion(perfiles={"t": PerfilConexion(nombre="t", servidor="127.0.0.1,1", timeout_conexion=1)}, perfil_default="t")


def test_expone_todas_las_herramientas_mas_utilitarias():
    servidor = crear_servidor(_config())
    tools = asyncio.run(servidor.list_tools())
    nombres = {t.name for t in tools}
    assert set(REGISTRO) <= nombres
    assert {"listar_perfiles", "info_servidor", "catalogo_herramientas"} <= nombres
    assert all(t.annotations.read_only_hint for t in tools)


def test_esquema_conserva_parametros_y_agrega_perfil():
    servidor = crear_servidor(_config())
    tools = {t.name: t for t in asyncio.run(servidor.list_tools())}
    props = tools["detalle_sesion"].input_schema["properties"]
    assert "session_id" in props and "perfil" in props
    assert tools["detalle_sesion"].input_schema["required"] == ["session_id"]


def test_error_de_conexion_vuelve_como_dato():
    servidor = crear_servidor(_config())
    r = asyncio.run(servidor.call_tool("sesiones_activas", {"top": 1}))
    datos = json.loads(r.content[0].text)
    assert "error" in datos


def test_prompts_y_recursos():
    servidor = crear_servidor(_config())
    assert {p.name for p in asyncio.run(servidor.list_prompts())} >= {"diagnostico_lentitud", "analizar_procedimiento", "revision_salud"}
    assert [str(x.uri) for x in asyncio.run(servidor.list_resources())] == ["sqlpilot://perfiles"]
