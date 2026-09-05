"""Herramienta: generar el informe de salud exportable (Markdown/HTML) desde el agente o el MCP."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta


@herramienta(
    "generar_informe_salud",
    """Genera el informe de salud completo de la instancia (configuración, backups, integridad, espacio, esperas,
    bloqueos, consultas costosas, regresiones, índices, estadísticas, deadlocks, jobs, seguridad) con hallazgos
    priorizados y scripts propuestos. Lo guarda en ~/.sqlpilot/informes como HTML (imprimible a PDF) y Markdown,
    y devuelve las rutas, el conteo de hallazgos y el Markdown. Tarda 30-90 s. Úsalo cuando el DBA pida
    'un informe', 'algo para entregar' o 'revisión completa'.""",
    "informes",
    formato="html | md | ambos (por defecto ambos).",
    incluir_markdown="Si es true devuelve el Markdown completo en la respuesta (largo); si no, solo rutas y hallazgos.",
)
def generar_informe_salud(conexion: ConexionSql, formato: str = "ambos", incluir_markdown: bool = False) -> dict:
    from sqlpilot.informes.generar import generar_informe

    formatos = ("html", "md") if formato == "ambos" else (formato,)
    resultado = generar_informe(conexion, config=None, formatos=formatos, con_resumen_ia=False)
    if not incluir_markdown:
        resultado.pop("markdown", None)
    return resultado
