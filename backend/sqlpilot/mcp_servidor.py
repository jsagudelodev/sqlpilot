"""Servidor MCP de SQLPilot: expone las herramientas de diagnóstico a cualquier cliente
(Claude Code, Claude Desktop, Cursor, VS Code...). Solo lectura, igual que la CLI.

Arranque: `sqlpilot mcp` (stdio) o `sqlpilot mcp --http --puerto 8765`.

Diseño:
- Cada herramienta corre en un hilo (no bloquea el bucle de eventos) con timeout.
- Pool de conexiones por perfil: llamadas concurrentes no comparten conexión pyodbc.
- Resultados recortados a `max_caracteres` (con `_omitidos`) para no saturar el contexto del cliente.
- Errores como `ToolError` → el cliente recibe `isError=true`.
- Logging a stderr (stdout es el canal del protocolo en stdio).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from sqlpilot import __version__
from sqlpilot.config import Configuracion, cargar_configuracion
from sqlpilot.db.pool import PoolConexiones
from sqlpilot.herramientas import REGISTRO, Herramienta

log = logging.getLogger("sqlpilot.mcp")

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
- "Dame un informe / algo para entregar": generar_informe_salud (HTML imprimible + Markdown en ~/.sqlpilot/informes).
Todas las herramientas aceptan `perfil` (ver listar_perfiles; si se omite, el default) y `max_caracteres`
(los resultados largos se recortan y se marca `_omitidos`; súbelo si necesitas más detalle).
Ninguna herramienta escribe en el servidor: KILL, índices, estadísticas o configuración se devuelven como
scripts para que el DBA los ejecute él mismo (usa proponer_accion para dejarlos registrados).
"""

_SOLO_LECTURA = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


# --------------------------------------------------------------------------- recorte
def recortar(resultado: Any, max_caracteres: int) -> Any:
    """Recorta listas largas dentro del resultado hasta que su JSON quepa en `max_caracteres`.
    Añade `_omitidos_<clave>` con la cantidad de elementos descartados y `_recortado: true`."""
    texto = json.dumps(resultado, ensure_ascii=False, default=str)
    if len(texto) <= max_caracteres or not isinstance(resultado, dict):
        if isinstance(resultado, list) and len(texto) > max_caracteres:
            n = max(1, len(resultado) * max_caracteres // len(texto))
            return {"elementos": resultado[:n], "_omitidos": len(resultado) - n, "_recortado": True}
        return resultado
    recortado = dict(resultado)
    listas = sorted(
        ((k, v) for k, v in resultado.items() if isinstance(v, list) and len(v) > 1),
        key=lambda kv: len(json.dumps(kv[1], default=str)),
        reverse=True,
    )
    factor = max_caracteres / len(texto)
    for k, v in listas:
        n = max(1, int(len(v) * factor))
        if n < len(v):
            recortado[k] = v[:n]
            recortado[f"_omitidos_{k}"] = len(v) - n
    recortado["_recortado"] = True
    texto = json.dumps(recortado, ensure_ascii=False, default=str)
    if len(texto) > max_caracteres:
        # Sigue grande (strings enormes, p. ej. una definición): recortar strings largos.
        for k, v in list(recortado.items()):
            if isinstance(v, str) and len(v) > 2000:
                recortado[k] = v[: max(2000, max_caracteres // 2)] + f"... [{len(v) - max_caracteres // 2} caracteres omitidos]"
    return recortado


# --------------------------------------------------------------------------- wrapper
async def _avisar(ctx: Context | None, nivel: str, mensaje: str, progreso: float = 0.0, total: float | None = None) -> None:
    """Notifica al cliente sin romper la herramienta.

    Usa `notifications/progress` con mensaje (la capacidad `logging` quedó deprecada en la
    especificación MCP, SEP-2577). Solo llega si el cliente envió progressToken; si no, es no-op.
    """
    getattr(log, nivel if nivel in ("debug", "info", "warning", "error") else "info")(mensaje)
    if ctx is None:
        return
    try:
        await ctx.report_progress(progreso, total, message=mensaje)
    except Exception as ex:  # sin request context (llamada in-process) o sesión cerrada
        log.debug("No se pudo notificar al cliente: %s", ex)


def _envolver(herramienta: Herramienta, pool: PoolConexiones, config: Configuracion):
    """Crea una función async con la firma de la herramienta (sin `conexion`) más `perfil` y
    `max_caracteres`, para que el SDK MCP derive el JSON Schema; ejecuta en hilo con timeout."""
    firma_original = inspect.signature(herramienta.funcion)
    parametros = [p for n, p in firma_original.parameters.items() if n != "conexion"]
    parametros.append(inspect.Parameter("perfil", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None))
    parametros.append(inspect.Parameter("max_caracteres", inspect.Parameter.KEYWORD_ONLY,
                                        default=config.agente.max_caracteres_resultado, annotation=int))
    # `ctx` lo inyecta el SDK (no aparece en el esquema): sirve para logs al cliente y progreso.
    parametros.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context | None))
    firma = inspect.Signature(parametros, return_annotation=dict[str, Any])
    timeout = config.agente.timeout_herramienta

    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        enlazados = firma.bind(*args, **kwargs)
        enlazados.apply_defaults()
        argumentos = dict(enlazados.arguments)
        perfil = argumentos.pop("perfil", None)
        max_caracteres = int(argumentos.pop("max_caracteres"))
        ctx: Context | None = argumentos.pop("ctx", None)
        en_curso: dict[str, Any] = {}  # la conexión que usa el hilo, para poder cancelarla desde aquí

        def ejecutar() -> Any:
            with pool.tomar(perfil) as conexion:
                en_curso["conexion"] = conexion
                try:
                    return herramienta.invocar(conexion, **argumentos)
                finally:
                    en_curso.pop("conexion", None)

        def cancelar_sql() -> None:
            conexion = en_curso.get("conexion")
            if conexion is not None and conexion.cancelar():
                log.info("Sentencia de %s cancelada en el servidor.", herramienta.nombre)

        detalle = ", ".join(f"{k}={v!r}" for k, v in argumentos.items())[:200]
        log.info("-> %s(%s) perfil=%s", herramienta.nombre, detalle, perfil)
        await _avisar(ctx, "debug", f"Ejecutando {herramienta.nombre}({detalle}) en el perfil {perfil or 'default'}…", 0, 1)
        try:
            resultado = await asyncio.wait_for(anyio.to_thread.run_sync(ejecutar), timeout=timeout)
        except TimeoutError as ex:
            cancelar_sql()
            await _avisar(ctx, "warning", f"{herramienta.nombre} superó {timeout} s; la consulta fue cancelada en SQL Server.")
            raise ToolError(f"{herramienta.nombre} superó el tiempo máximo de {timeout} s; la consulta se canceló.") from ex
        except asyncio.CancelledError:
            cancelar_sql()  # el cliente canceló la petición: no dejar la consulta corriendo en el servidor
            raise
        except TypeError as ex:
            raise ToolError(f"Argumentos inválidos para {herramienta.nombre}: {ex}") from ex
        except Exception as ex:
            log.warning("ERROR %s: %s", herramienta.nombre, ex)
            await _avisar(ctx, "error", f"{herramienta.nombre} falló: {str(ex)[:300]}")
            raise ToolError(f"{type(ex).__name__}: {str(ex)[:2000]}") from ex
        if isinstance(resultado, dict) and "error" in resultado and len(resultado) <= 3:
            raise ToolError(str(resultado["error"]))
        recortado = recortar(resultado, max_caracteres)
        if isinstance(recortado, dict) and recortado.get("_recortado"):
            omitidos = {k[len("_omitidos_"):]: v for k, v in recortado.items() if k.startswith("_omitidos")}
            await _avisar(ctx, "info", f"Resultado de {herramienta.nombre} recortado a {max_caracteres} caracteres; omitidos: {omitidos}. "
                                       "Sube max_caracteres si necesitas más.", 1, 1)
        return recortado if isinstance(recortado, dict) else {"resultado": recortado}

    wrapper.__name__ = herramienta.nombre
    wrapper.__qualname__ = herramienta.nombre
    wrapper.__doc__ = herramienta.descripcion
    wrapper.__signature__ = firma  # type: ignore[attr-defined]
    anotaciones = {p.name: p.annotation for p in parametros if p.annotation is not inspect.Parameter.empty}
    anotaciones["return"] = dict[str, Any]
    wrapper.__annotations__ = anotaciones
    return wrapper


def _describir(herramienta: Herramienta) -> str:
    texto = " ".join(herramienta.descripcion.split())
    props = herramienta.parametros.get("properties", {})
    if props:
        detalles = "; ".join(f"{n}: {p.get('description', '')}".rstrip(": ") for n, p in props.items())
        texto += f" Parámetros — {detalles}."
    return texto


# --------------------------------------------------------------------------- servidor
def crear_servidor(config: Configuracion | None = None) -> MCPServer:
    config = config or cargar_configuracion()
    pool = PoolConexiones(config, maximo=config.agente.conexiones_por_perfil)
    servidor = MCPServer(
        name="sqlpilot",
        title="SQLPilot — DBA copilot para SQL Server",
        version=__version__,
        instructions=INSTRUCCIONES,
    )

    for h in REGISTRO.values():
        servidor.add_tool(_envolver(h, pool, config), name=h.nombre, description=_describir(h),
                          annotations=_SOLO_LECTURA, structured_output=True)

    def _perfiles() -> dict[str, Any]:
        return {
            "default": config.perfil_default or (next(iter(config.perfiles)) if len(config.perfiles) == 1 else None),
            "perfiles": [{"nombre": n, "servidor": p.servidor, "base_datos": p.base_datos, "autenticacion": p.autenticacion}
                         for n, p in config.perfiles.items()],
            "archivo": str(config.ruta_archivo) if config.ruta_archivo else None,
        }

    @servidor.tool(name="listar_perfiles", description="Perfiles de conexión configurados en SQLPilot y cuál es el default.",
                   annotations=_SOLO_LECTURA, structured_output=True)
    def listar_perfiles() -> dict[str, Any]:
        return _perfiles()

    @servidor.tool(name="info_servidor", description="Prueba la conexión de un perfil y devuelve versión, edición, login y bases online.",
                   annotations=_SOLO_LECTURA, structured_output=True)
    async def info_servidor(perfil: str | None = None) -> dict[str, Any]:
        def ejecutar() -> dict[str, Any]:
            with pool.tomar(perfil) as c:
                return c.info_servidor()
        try:
            return await asyncio.wait_for(anyio.to_thread.run_sync(ejecutar), timeout=config.agente.timeout_herramienta)
        except Exception as ex:
            raise ToolError(f"No se pudo conectar: {ex}") from ex

    @servidor.tool(name="catalogo_herramientas", description="Lista las herramientas de SQLPilot por categoría con sus parámetros.",
                   annotations=_SOLO_LECTURA, structured_output=True)
    def catalogo_herramientas() -> dict[str, Any]:
        return {"herramientas": [{"categoria": h.categoria, "nombre": h.nombre, "parametros": list(h.parametros.get("properties", {}))}
                                 for h in REGISTRO.values()]}

    @servidor.resource("sqlpilot://perfiles", name="perfiles", description="Perfiles de conexión configurados.", mime_type="application/json")
    def recurso_perfiles() -> str:
        return json.dumps(_perfiles(), ensure_ascii=False)

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
            "integridad_bases_datos, auditoria_seguridad, esperas_acumuladas. Prioriza los hallazgos por riesgo "
            "(pérdida de datos > seguridad > disponibilidad > rendimiento) y entrega un plan de acción con scripts propuestos."
        )

    servidor._pool_sqlpilot = pool  # type: ignore[attr-defined]  (para cerrar en pruebas)
    return servidor


def configurar_logging(nivel: str = "INFO") -> None:
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, nivel.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def ejecutar(transporte: str = "stdio", host: str = "127.0.0.1", puerto: int = 8765, nivel_log: str = "INFO") -> None:
    configurar_logging(nivel_log)
    servidor = crear_servidor()
    log.info("SQLPilot MCP %s · %d herramientas · transporte %s", __version__, len(REGISTRO) + 3, transporte)
    if transporte == "stdio":
        servidor.run(transport="stdio")
    else:
        servidor.settings.host = host
        servidor.settings.port = puerto
        servidor.run(transport="streamable-http")


if __name__ == "__main__":
    ejecutar()
