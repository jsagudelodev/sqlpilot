"""CLI de SQLPilot (Typer + Rich)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from sqlpilot import __version__
from sqlpilot.config import Configuracion, cargar_configuracion
from sqlpilot.db.conexion import ConexionSql

# Consolas Windows heredadas (cp1252) no soportan todos los caracteres; forzamos UTF-8 en la salida.
for _flujo in (sys.stdout, sys.stderr):
    if hasattr(_flujo, "reconfigure"):
        try:
            _flujo.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

app = typer.Typer(help="SQLPilot — copiloto de diagnóstico y operación para DBAs de SQL Server.", no_args_is_help=True)
consola = Console()

_OPCION_PERFIL = typer.Option(None, "--perfil", "-p", help="Perfil de conexión de sqlpilot.toml.")
_OPCION_CONFIG = typer.Option(None, "--config", "-c", help="Ruta a sqlpilot.toml.")


def _abrir(perfil: str | None, config: str | None) -> tuple[Configuracion, ConexionSql]:
    try:
        cfg = cargar_configuracion(config)
        p = cfg.perfil(perfil)
        conexion = ConexionSql(p).abrir()
    except Exception as ex:
        consola.print(f"[red]Error de conexión/configuración:[/red] {ex}")
        raise typer.Exit(1) from ex
    return cfg, conexion


def _tabla(filas: list[dict[str, Any]], titulo: str = "", max_ancho: int = 60) -> None:
    if not filas:
        consola.print(f"[dim]{titulo}: sin resultados[/dim]")
        return
    t = Table(title=titulo or None, show_lines=False, header_style="bold cyan")
    for col in filas[0]:
        t.add_column(str(col), overflow="fold", max_width=max_ancho)
    for f in filas:
        t.add_row(*[("" if v is None else str(v)) for v in f.values()])
    consola.print(t)


def _imprimir(resultado: Any, como_json: bool) -> None:
    if como_json:
        consola.print_json(json.dumps(resultado, ensure_ascii=False, default=str))
        return
    if isinstance(resultado, dict):
        for clave, valor in resultado.items():
            if isinstance(valor, list) and valor and isinstance(valor[0], dict):
                _tabla(valor, clave)
            elif isinstance(valor, dict):
                consola.print(Panel(json.dumps(valor, ensure_ascii=False, indent=2, default=str), title=clave))
            elif valor not in (None, "", []):
                consola.print(f"[bold]{clave}:[/bold] {valor}")
    else:
        consola.print(resultado)


# --------------------------------------------------------------------------
@app.command()
def version() -> None:
    """Muestra la versión."""
    consola.print(f"SQLPilot {__version__}")


@app.command()
def perfiles(config: str | None = _OPCION_CONFIG) -> None:
    """Lista los perfiles de conexión configurados."""
    cfg = cargar_configuracion(config)
    consola.print(f"[dim]Archivo: {cfg.ruta_archivo or '(sin archivo, solo entorno)'}[/dim]")
    filas = [{"perfil": n, "servidor": p.servidor, "base_datos": p.base_datos, "auth": p.autenticacion,
              "usuario": p.usuario or "", "default": n == cfg.perfil_default}
             for n, p in cfg.perfiles.items()]
    _tabla(filas, "Perfiles")


@app.command()
def salud(perfil: str | None = _OPCION_PERFIL, config: str | None = _OPCION_CONFIG) -> None:
    """Prueba la conexión y muestra información del servidor."""
    _, conexion = _abrir(perfil, config)
    with conexion:
        info = conexion.info_servidor()
    consola.print(Panel("\n".join(f"[bold]{k}:[/bold] {v}" for k, v in info.items()), title="Conexión OK", border_style="green"))


@app.command()
def herramientas(categoria: str | None = typer.Option(None, help="Filtrar por categoría.")) -> None:
    """Lista las herramientas de diagnóstico disponibles para el agente."""
    from sqlpilot.herramientas import listar

    filas = [{"categoria": h.categoria, "nombre": h.nombre,
              "parametros": ", ".join(h.parametros.get("properties", {}).keys()),
              "descripcion": " ".join(h.descripcion.split())[:110]} for h in listar(categoria)]
    filas.sort(key=lambda f: (f["categoria"], f["nombre"]))
    _tabla(filas, f"{len(filas)} herramientas", max_ancho=110)


@app.command()
def ejecutar(
    nombre: str = typer.Argument(..., help="Nombre de la herramienta (ver `sqlpilot herramientas`)."),
    argumentos: list[str] = typer.Argument(None, help="Argumentos clave=valor."),
    perfil: str | None = _OPCION_PERFIL,
    config: str | None = _OPCION_CONFIG,
    como_json: bool = typer.Option(False, "--json", help="Salida JSON."),
) -> None:
    """Ejecuta una herramienta directamente, sin LLM. Ej: sqlpilot ejecutar esperas_en_ventana segundos=5"""
    from sqlpilot.herramientas import REGISTRO

    if nombre not in REGISTRO:
        consola.print(f"[red]Herramienta desconocida:[/red] {nombre}")
        raise typer.Exit(1)
    kwargs: dict[str, Any] = {}
    props = REGISTRO[nombre].parametros.get("properties", {})
    for a in argumentos or []:
        if "=" not in a:
            consola.print(f"[red]Argumento inválido:[/red] {a} (usa clave=valor)")
            raise typer.Exit(1)
        k, v = a.split("=", 1)
        tipo = props.get(k, {}).get("type", "string")
        kwargs[k] = int(v) if tipo == "integer" else float(v) if tipo == "number" else (v.lower() in ("1", "true", "si", "sí")) if tipo == "boolean" else v
    _, conexion = _abrir(perfil, config)
    with conexion:
        try:
            resultado = REGISTRO[nombre].invocar(conexion, **kwargs)
        except Exception as ex:
            consola.print(f"[red]Error:[/red] {ex}")
            raise typer.Exit(1) from ex
    _imprimir(resultado, como_json)


@app.command()
def sql(
    consulta: str = typer.Argument(..., help="Consulta T-SQL de lectura."),
    perfil: str | None = _OPCION_PERFIL,
    config: str | None = _OPCION_CONFIG,
    max_filas: int = typer.Option(100, help="Máximo de filas."),
    como_json: bool = typer.Option(False, "--json"),
) -> None:
    """Ejecuta una consulta de solo lectura."""
    from sqlpilot.herramientas.sql_libre import ejecutar_sql_lectura

    _, conexion = _abrir(perfil, config)
    with conexion:
        resultado = ejecutar_sql_lectura(conexion, consulta, max_filas)
    if "error" in resultado:
        consola.print(f"[red]{resultado['error']}[/red]")
        raise typer.Exit(1)
    if como_json:
        _imprimir(resultado, True)
    else:
        _tabla([dict(zip(resultado["columnas"], f)) for f in resultado["filas"]],
               f"{resultado['total_filas']} filas{' (truncado)' if resultado['truncado'] else ''} · {resultado['tiempo_ms']} ms")


@app.command()
def revisar(perfil: str | None = _OPCION_PERFIL, config: str | None = _OPCION_CONFIG, como_json: bool = typer.Option(False, "--json")) -> None:
    """Chequeo rápido de salud sin LLM: hallazgos priorizados en pantalla (para el informe completo usa `informe`)."""
    from sqlpilot.informes.recoleccion import recolectar

    cfg, conexion = _abrir(perfil, config)
    with conexion, consola.status("[cyan]Revisando la instancia...[/cyan]", spinner="dots"):
        datos = recolectar(conexion, paralelismo=cfg.agente.paralelismo_informe)
    if como_json:
        _imprimir(datos, True)
        return
    hallazgos = [{"sev": h["severidad"], "area": h["area"], "titulo": h["titulo"], "base_datos": h["base_datos"]}
                 for h in datos["hallazgos"] if h["severidad"] != "info"]
    _tabla(hallazgos, f"Hallazgos ({len(hallazgos)})", max_ancho=90)
    no_eval = [h["titulo"] for h in datos["hallazgos"] if h["severidad"] == "info"]
    if no_eval:
        consola.print(f"[dim]No evaluado ({len(no_eval)}): {'; '.join(no_eval)[:300]}[/dim]")
    esperas = (datos["secciones"].get("esperas", {}).get("datos") or {}).get("esperas", [])
    _tabla(esperas[:8], "Top esperas acumuladas")
    consola.print("[dim]Informe completo con scripts: sqlpilot informe[/dim]")


@app.command()
def informe(
    perfil: str | None = _OPCION_PERFIL,
    config: str | None = _OPCION_CONFIG,
    formato: str = typer.Option("ambos", help="html | md | json | ambos (html+md) | todos."),
    salida: str | None = typer.Option(None, "--salida", "-o", help="Ruta base del archivo (sin extensión). Por defecto ~/.sqlpilot/informes/."),
    resumen_ia: bool = typer.Option(False, "--resumen-ia", help="Agrega un resumen ejecutivo generado con el LLM configurado."),
    abrir: bool = typer.Option(False, "--abrir", help="Abre el HTML en el navegador al terminar."),
) -> None:
    """Informe de salud exportable (HTML imprimible a PDF + Markdown) con hallazgos priorizados y scripts."""
    from sqlpilot.informes.generar import generar_informe

    formatos = {"ambos": ("html", "md"), "todos": ("html", "md", "json")}.get(formato, (formato,))
    cfg, conexion = _abrir(perfil, config)
    with conexion, consola.status("[cyan]Generando informe...[/cyan]", spinner="dots"):
        r = generar_informe(conexion, cfg, formatos=formatos, salida=salida, con_resumen_ia=resumen_ia)
    c = r["hallazgos"]
    rutas = "\n".join(f"[bold]{k.upper()}:[/bold] {v}" for k, v in r["rutas"].items())
    consola.print(Panel(
        f"[red]{c['alta']} altos[/red] · [yellow]{c['media']} medios[/yellow] · [blue]{c['baja']} bajos[/blue] · {c['info']} no evaluados"
        f"  [dim]({r['segundos']} s)[/dim]\n{rutas}",
        title="Informe generado", border_style="green"))
    if r.get("resumen_ejecutivo"):
        consola.print(Panel(r["resumen_ejecutivo"], title="Resumen ejecutivo"))
    if abrir and "html" in r["rutas"]:
        import webbrowser

        webbrowser.open(Path(r["rutas"]["html"]).as_uri())


@app.command()
def chat(
    pregunta: str | None = typer.Argument(None, help="Pregunta única (si se omite, abre sesión interactiva)."),
    perfil: str | None = _OPCION_PERFIL,
    config: str | None = _OPCION_CONFIG,
    mostrar_herramientas: bool = typer.Option(True, "--herramientas/--sin-herramientas", help="Mostrar las llamadas a herramientas."),
) -> None:
    """Conversa con el agente DBA sobre la instancia conectada."""
    from sqlpilot.agente.agente import AgenteDBA
    from sqlpilot.llm.fabrica import crear_proveedor

    cfg, conexion = _abrir(perfil, config)
    try:
        proveedor = crear_proveedor(cfg.llm)
    except Exception as ex:
        consola.print(f"[red]No se pudo inicializar el LLM ({cfg.llm.proveedor}):[/red] {ex}")
        raise typer.Exit(1) from ex

    with conexion:
        agente = AgenteDBA(cfg, conexion, proveedor)
        consola.print(Panel(
            f"[bold]{agente.info.get('servidor')}[/bold] · {agente.info.get('base_datos')} · v{agente.info.get('version')}\n"
            f"LLM: {cfg.llm.proveedor} / {proveedor.modelo} · {len(agente.herramientas)} herramientas · modo solo lectura",
            title="SQLPilot", border_style="cyan"))

        def procesar(texto: str) -> None:
            with consola.status("[cyan]Pensando...[/cyan]", spinner="dots") as estado:
                for ev in agente.preguntar(texto):
                    if ev.tipo == "llamada" and mostrar_herramientas:
                        args = ", ".join(f"{k}={v!r}" for k, v in ev.datos["argumentos"].items())
                        estado.update(f"[cyan]{ev.datos['nombre']}({args[:80]})[/cyan]")
                        consola.print(f"  [dim]-> {ev.datos['nombre']}({args[:120]})[/dim]")
                    elif ev.tipo == "resultado" and mostrar_herramientas:
                        marca = "[red]x[/red]" if ev.datos["error"] else "[green]ok[/green]"
                        consola.print(f"  [dim]{marca} {ev.datos['nombre']} · {ev.datos['tiempo_ms']} ms[/dim]")
                    elif ev.tipo == "texto" and ev.datos["final"]:
                        consola.print()
                        consola.print(Markdown(ev.datos["texto"]))
                    elif ev.tipo == "texto":
                        consola.print(f"[italic dim]{ev.datos['texto'][:300]}[/italic dim]")
                    elif ev.tipo == "error":
                        consola.print(f"[red]{ev.datos['mensaje']}[/red]")
                    elif ev.tipo == "fin":
                        consola.print(f"[dim]{ev.datos['iteraciones']} pasos · tokens in={ev.datos['tokens_entrada']} out={ev.datos['tokens_salida']}[/dim]")

        if pregunta:
            procesar(pregunta)
            return
        consola.print("[dim]Escribe tu pregunta. Comandos: /reiniciar, /salir[/dim]")
        while True:
            try:
                texto = consola.input("\n[bold green]dba>[/bold green] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not texto:
                continue
            if texto in ("/salir", "/exit", "/q"):
                break
            if texto == "/reiniciar":
                agente.reiniciar()
                consola.print("[dim]Conversación reiniciada.[/dim]")
                continue
            procesar(texto)


@app.command()
def mcp(
    http: bool = typer.Option(False, "--http", help="Usar transporte streamable-http en vez de stdio."),
    host: str = typer.Option("127.0.0.1"),
    puerto: int = typer.Option(8765),
    config: str | None = _OPCION_CONFIG,
) -> None:
    """Arranca el servidor MCP (para Claude Code, Claude Desktop, Cursor, VS Code...)."""
    from sqlpilot.mcp_servidor import ejecutar as ejecutar_mcp

    if config:
        import os

        os.environ["SQLPILOT_CONFIG"] = config
    ejecutar_mcp("streamable-http" if http else "stdio", host, puerto)


@app.command()
def servir(
    host: str = typer.Option("127.0.0.1"),
    puerto: int = typer.Option(8000),
    recargar: bool = typer.Option(False, "--recargar", help="Autoreload (desarrollo)."),
) -> None:
    """Levanta la API HTTP (FastAPI) para el frontend web."""
    import uvicorn

    uvicorn.run("sqlpilot.api.main:app", host=host, port=puerto, reload=recargar)


if __name__ == "__main__":
    app()
