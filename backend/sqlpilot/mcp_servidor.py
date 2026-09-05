"""Servidor MCP de SQLPilot: expone las herramientas de diagnóstico a cualquier cliente
(Claude Code, Claude Desktop, Cursor, VS Code...). Solo lectura, igual que la CLI.

Arranque: `sqlpilot mcp` (stdio) o `sqlpilot mcp --http --puerto 8765`.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sqlpilot import __version__
from sqlpilot.config import Configuracion, cargar_configuracion
from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas import REGISTRO, Herramienta

INSTRUCCIONES = """SQLPilot: herramientas de diagnóstico y operación para DBAs de SQL Server. SOLO LECTURA.

Metodología recomendada:
- "Está lento / hay bloqueos": esperas_en_ventana (o esperas_acumuladas) → sesiones_activas / cadena_bloqueos
  → consultas_costosas_cache o consultas_costosas_query_store → obtener_plan_cache.
- "Analiza este SP / consulta": definicion_objeto → plan_estimado → indices_de_tabla, estadisticas_desactualizadas
  e indices_faltantes de las tablas implicadas.
- "Salud general": configuracion_instancia, configuracion_bases_datos, estado_backups, espacio_bases_datos,
  jobs_fallidos, errores_log_sql, deadlocks_recientes, integridad_bases_datos, auditoria_seguridad.
- "Antes funcionaba bien": regresiones_query_store → comparar_planes_query_store; o tomar_snapshot en un momento
  normal y comparar_snapshots durante el problema.
- "¿Las estimaciones están mal?": plan_real (solo SELECT o SP de lectura pura; se ejecuta con ROLLBACK).
Todas las herramientas aceptan `perfil` (nombre del perfil de conexión; ver listar_perfiles). Si se omite se usa el
perfil por defecto. Ninguna herramienta escribe en el servidor: KILL, índices, estadísticas o configuración se
devuelven como scripts para que el DBA los ejecute él mismo (usa proponer_accion para dejarlos registrados).
"""

_SOLO_LECTURA = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


class _Conexiones:
    """Una conexión viva por perfil, abierta bajo demanda."""

    def __init__(self, config: Configuracion):
        self.config = config
        self._abiertas: dict[str, ConexionSql] = {}

    def obtener(self, perfil: str | None) -> ConexionSql:
        p = self.config.perfil(perfil)
        conexion = self._abiertas.get(p.nombre)
        if conexion is None:
            conexion = ConexionSql(p).abrir()
            self._abiertas[p.nombre] = conexion
        else:
            try:
                conexion.escalar("SELECT 1")
            except Exception:  # conexión caída: reabrir
                conexion.cerrar()
                conexion = ConexionSql(p).abrir()
                self._abiertas[p.nombre] = conexion
        return conexion

    def cerrar_todas(self) -> None:
        for c in self._abiertas.values():
            c.cerrar()
        self._abiertas.clear()


def _a_json(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False, default=str)


def _envolver(herramienta: Herramienta, conexiones: _Conexiones):
    """Crea una función con la firma de la herramienta (sin `conexion`) más `perfil`, para que
    el SDK MCP derive el JSON Schema de los parámetros."""
    firma_original = inspect.signature(herramienta.funcion)
    parametros = [p for n, p in firma_original.parameters.items() if n != "conexion"]
    parametros.append(inspect.Parameter("perfil", inspect.Parameter.KEYWORD_ONLY, default=None, annotation="str | None"))
    firma = inspect.Signature(parametros, return_annotation=str)

    def wrapper(*args: Any, **kwargs: Any) -> str:
        enlazados = firma.bind(*args, **kwargs)
        enlazados.apply_defaults()
        argumentos = dict(enlazados.arguments)
        perfil = argumentos.pop("perfil", None)
        try:
            conexion = conexiones.obtener(perfil)
            return _a_json(herramienta.invocar(conexion, **argumentos))
        except Exception as ex:  # el error vuelve al modelo como dato, no como fallo del protocolo
            return _a_json({"error": f"{type(ex).__name__}: {str(ex)[:2000]}"})

    wrapper.__name__ = herramienta.nombre
    wrapper.__qualname__ = herramienta.nombre
    wrapper.__doc__ = herramienta.descripcion
    wrapper.__signature__ = firma  # type: ignore[attr-defined]
    anotaciones = {p.name: p.annotation for p in parametros if p.annotation is not inspect.Parameter.empty}
    anotaciones["return"] = str
    wrapper.__annotations__ = anotaciones
    return wrapper


def _describir(herramienta: Herramienta) -> str:
    texto = " ".join(herramienta.descripcion.split())
    props = herramienta.parametros.get("properties", {})
    if props:
        detalles = "; ".join(f"{n}: {p.get('description', '')}".rstrip(": ") for n, p in props.items())
        texto += f" Parámetros — {detalles}. perfil: perfil de conexión (opcional)."
    return texto


def crear_servidor(config: Configuracion | None = None) -> MCPServer:
    config = config or cargar_configuracion()
    conexiones = _Conexiones(config)
    servidor = MCPServer(
        name="sqlpilot",
        title="SQLPilot — DBA copilot para SQL Server",
        version=__version__,
        instructions=INSTRUCCIONES,
    )

    for h in REGISTRO.values():
        servidor.add_tool(_envolver(h, conexiones), name=h.nombre, description=_describir(h), annotations=_SOLO_LECTURA)

    @servidor.tool(name="listar_perfiles", description="Perfiles de conexión configurados en SQLPilot y cuál es el default.",
                   annotations=_SOLO_LECTURA)
    def listar_perfiles() -> str:
        return _a_json({
            "default": config.perfil_default or (next(iter(config.perfiles)) if len(config.perfiles) == 1 else None),
            "perfiles": [{"nombre": n, "servidor": p.servidor, "base_datos": p.base_datos, "autenticacion": p.autenticacion}
                         for n, p in config.perfiles.items()],
            "archivo": str(config.ruta_archivo) if config.ruta_archivo else None,
        })

    @servidor.tool(name="info_servidor", description="Prueba la conexión de un perfil y devuelve versión, edición, login y bases online.",
                   annotations=_SOLO_LECTURA)
    def info_servidor(perfil: str | None = None) -> str:
        try:
            return _a_json(conexiones.obtener(perfil).info_servidor())
        except Exception as ex:
            return _a_json({"error": str(ex)})

    @servidor.tool(name="catalogo_herramientas", description="Lista las herramientas de SQLPilot por categoría con sus parámetros.",
                   annotations=_SOLO_LECTURA)
    def catalogo_herramientas() -> str:
        return _a_json([{"categoria": h.categoria, "nombre": h.nombre, "parametros": list(h.parametros.get("properties", {}))}
                        for h in REGISTRO.values()])

    @servidor.resource("sqlpilot://perfiles", name="perfiles", description="Perfiles de conexión configurados.", mime_type="application/json")
    def recurso_perfiles() -> str:
        return listar_perfiles()

    @servidor.prompt(name="diagnostico_lentitud", description="Guía paso a paso para diagnosticar 'el servidor está lento'.")
    def prompt_lentitud(perfil: str | None = None) -> str:
        p = f" (perfil {perfil})" if perfil else ""
        return (
            f"Actúa como DBA senior de SQL Server. Diagnostica por qué la instancia{p} está lenta ahora mismo.\n"
            "1. esperas_en_ventana (10 s) y presion_cpu_memoria_io para identificar el recurso saturado.\n"
            "2. sesiones_activas y cadena_bloqueos para ver qué corre y quién bloquea.\n"
            "3. consultas_costosas_cache (orden=cpu y orden=lecturas) y, si aplica, consultas_costosas_query_store.\n"
            "4. obtener_plan_cache de la consulta más costosa; revisa scans, índices faltantes y advertencias.\n"
            "Responde con: hallazgos con números, causa probable, recomendaciones priorizadas y acciones propuestas "
            "(scripts) sin ejecutarlas."
        )

    @servidor.prompt(name="analizar_procedimiento", description="Analiza el rendimiento de un procedimiento almacenado.")
    def prompt_sp(nombre: str, perfil: str | None = None) -> str:
        p = f" (perfil {perfil})" if perfil else ""
        return (
            f"Analiza el rendimiento del procedimiento {nombre}{p}.\n"
            "1. definicion_objeto para leer el código completo y sus dependencias.\n"
            "2. plan_estimado con un EXEC representativo; identifica scans, lookups, conversiones implícitas, spills.\n"
            "3. Para cada tabla implicada: describir_tabla, indices_de_tabla, estadisticas_desactualizadas, indices_faltantes.\n"
            "4. Busca anti-patrones en el código: predicados no SARGables, cursores, SELECT *, NOLOCK, parameter sniffing.\n"
            "Entrega: resumen, hallazgos con severidad, reescritura sugerida y scripts de índices propuestos (sin ejecutar)."
        )

    @servidor.prompt(name="revision_salud", description="Revisión general de salud de la instancia.")
    def prompt_salud(perfil: str | None = None) -> str:
        p = f" (perfil {perfil})" if perfil else ""
        return (
            f"Haz una revisión de salud de la instancia{p}: configuracion_instancia, configuracion_bases_datos, "
            "estado_backups, espacio_bases_datos, estado_tempdb, jobs_fallidos, errores_log_sql, deadlocks_recientes, "
            "esperas_acumuladas. Prioriza los hallazgos por riesgo (pérdida de datos > disponibilidad > rendimiento) "
            "y entrega un plan de acción con scripts propuestos."
        )

    return servidor


def ejecutar(transporte: str = "stdio", host: str = "127.0.0.1", puerto: int = 8765) -> None:
    servidor = crear_servidor()
    if transporte == "stdio":
        servidor.run(transport="stdio")
    else:
        servidor.settings.host = host
        servidor.settings.port = puerto
        servidor.run(transport="streamable-http")


if __name__ == "__main__":
    ejecutar()
