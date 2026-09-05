"""Orquestación del informe: recolectar, resumir con IA (opcional), renderizar y guardar."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlpilot.config import Configuracion
from sqlpilot.db.conexion import ConexionSql
from sqlpilot.informes.recoleccion import recolectar
from sqlpilot.informes.render import a_html, a_json, a_markdown

_PROMPT_RESUMEN = """Eres un DBA senior. A partir de los hallazgos de un informe de salud de SQL Server, escribe un resumen
ejecutivo en español para el jefe del DBA (no técnico): máximo 8 líneas, sin viñetas anidadas, sin jerga innecesaria.
Estructura: estado general en una frase; los 3 riesgos más importantes con su impacto para el negocio; qué se recomienda
hacer esta semana; y qué requiere decisión o presupuesto. No inventes datos: usa solo lo que está en los hallazgos.

Servidor: {servidor} · Base: {base_datos} · Versión: {version} {edicion}
Hallazgos (severidad | área | título | detalle):
{hallazgos}
"""


def directorio_informes() -> Path:
    base = Path(os.environ.get("SQLPILOT_HOME", Path.home() / ".sqlpilot")) / "informes"
    base.mkdir(parents=True, exist_ok=True)
    return base


def resumen_ejecutivo(config: Configuracion, informe: dict[str, Any]) -> str:
    """Una sola llamada al LLM configurado, sin herramientas."""
    from sqlpilot.llm.fabrica import crear_proveedor

    srv = informe["servidor"]
    lineas = [f"{h['severidad']} | {h['area']} | {h['titulo']} | {h.get('detalle', '')[:200]}"
              for h in informe["hallazgos"] if h["severidad"] != "info"][:40]
    prompt = _PROMPT_RESUMEN.format(servidor=srv.get("servidor"), base_datos=srv.get("base_datos"), version=srv.get("version"),
                                    edicion=srv.get("edicion", ""), hallazgos="\n".join(lineas) or "(sin hallazgos)")
    proveedor = crear_proveedor(config.llm)
    respuesta = proveedor.completar("Responde solo con el resumen ejecutivo.", [{"rol": "usuario", "contenido": prompt}], [])
    return respuesta.texto


def generar_informe(
    conexion: ConexionSql,
    config: Configuracion | None = None,
    formatos: tuple[str, ...] = ("html", "md"),
    salida: str | Path | None = None,
    con_resumen_ia: bool = False,
    secciones: list[str] | None = None,
    paralelismo: int | None = None,
) -> dict[str, Any]:
    """Recolecta, renderiza y guarda. Devuelve rutas, conteos y el markdown (para MCP/API)."""
    if paralelismo is None:
        paralelismo = config.agente.paralelismo_informe if config is not None else 4
    inicio = time.perf_counter()
    informe = recolectar(conexion, secciones, paralelismo=paralelismo)
    informe["segundos_recoleccion"] = round(time.perf_counter() - inicio, 1)
    resumen = None
    if con_resumen_ia and config is not None:
        try:
            resumen = resumen_ejecutivo(config, informe)
        except Exception as ex:
            resumen = f"(No se pudo generar el resumen con IA: {str(ex)[:200]})"
    informe["resumen_ejecutivo"] = resumen

    marca = datetime.now().strftime("%Y%m%d_%H%M")  # noqa: DTZ005
    servidor = "".join(c if c.isalnum() else "_" for c in str(informe["servidor"].get("servidor", conexion.perfil.nombre)))[:40]
    base = Path(salida) if salida else directorio_informes() / f"informe_{servidor}_{marca}"
    if base.suffix in (".html", ".md", ".json"):
        base = base.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)

    rutas: dict[str, str] = {}
    markdown = a_markdown(informe, resumen)
    if "md" in formatos:
        (base.with_suffix(".md")).write_text(markdown, encoding="utf-8")
        rutas["md"] = str(base.with_suffix(".md"))
    if "html" in formatos:
        (base.with_suffix(".html")).write_text(a_html(informe, resumen), encoding="utf-8")
        rutas["html"] = str(base.with_suffix(".html"))
    if "json" in formatos:
        (base.with_suffix(".json")).write_text(a_json(informe), encoding="utf-8")
        rutas["json"] = str(base.with_suffix(".json"))

    conteo = {s: sum(1 for h in informe["hallazgos"] if h["severidad"] == s) for s in ("alta", "media", "baja", "info")}
    return {"rutas": rutas, "hallazgos": conteo, "total_hallazgos": len(informe["hallazgos"]),
            "segundos": informe["segundos_recoleccion"],
            "secciones_con_error": [k for k, v in informe["secciones"].items() if v.get("error")],
            "resumen_ejecutivo": resumen, "markdown": markdown, "hallazgos_detalle": informe["hallazgos"]}


def informe_como_dict_ligero(resultado: dict[str, Any]) -> dict[str, Any]:
    """Versión sin el markdown completo, para respuestas JSON."""
    return {k: v for k, v in resultado.items() if k not in ("markdown",)}


__all__ = ["directorio_informes", "generar_informe", "informe_como_dict_ligero", "resumen_ejecutivo"]
