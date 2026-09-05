"""Estadísticas del optimizador: desactualizadas, histograma."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta


@herramienta(
    "estadisticas_desactualizadas",
    """Estadísticas con muchas modificaciones desde su última actualización (sys.dm_db_stats_properties).
    Estadísticas viejas = estimaciones malas = planes malos. Devuelve el script UPDATE STATISTICS propuesto.""",
    "estadisticas",
    tabla="Filtrar por tabla (opcional).",
    min_pct_modificado="Porcentaje mínimo de filas modificadas para incluir (por defecto 10).",
    top="Cantidad a devolver.",
)
def estadisticas_desactualizadas(conexion: ConexionSql, tabla: str | None = None, min_pct_modificado: float = 10, top: int = 30) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT TOP (?)
            OBJECT_SCHEMA_NAME(s.object_id) + '.' + OBJECT_NAME(s.object_id) AS tabla, s.name AS estadistica,
            s.auto_created, s.no_recompute,
            sp.last_updated AS ultima_actualizacion, sp.rows AS filas, sp.rows_sampled AS filas_muestreadas,
            CAST(100.0 * sp.rows_sampled / NULLIF(sp.rows, 0) AS DECIMAL(5,2)) AS pct_muestra,
            sp.modification_counter AS modificaciones,
            CAST(100.0 * sp.modification_counter / NULLIF(sp.rows, 0) AS DECIMAL(10,2)) AS pct_modificado,
            'UPDATE STATISTICS ' + OBJECT_SCHEMA_NAME(s.object_id) + '.[' + OBJECT_NAME(s.object_id) + '] [' + s.name + '] WITH FULLSCAN;' AS script
        FROM sys.stats s
        JOIN sys.objects o ON o.object_id = s.object_id AND o.type = 'U'
        CROSS APPLY sys.dm_db_stats_properties(s.object_id, s.stats_id) sp
        WHERE sp.rows > 0
          AND (? IS NULL OR OBJECT_NAME(s.object_id) = ?)
          AND 100.0 * sp.modification_counter / NULLIF(sp.rows, 0) >= ?
        ORDER BY sp.modification_counter DESC
        """,
        (int(top), tabla, tabla, float(min_pct_modificado)),
    )
    return {"estadisticas": filas}


@herramienta(
    "histograma_estadistica",
    "Histograma y densidad de una estadística (DBCC SHOW_STATISTICS) para diagnosticar estimaciones de cardinalidad.",
    "estadisticas",
    tabla="Tabla con esquema (ej. dbo.Despachos).",
    estadistica="Nombre de la estadística o del índice.",
)
def histograma_estadistica(conexion: ConexionSql, tabla: str, estadistica: str) -> dict:
    tabla_s = tabla.replace("'", "''")
    est_s = estadistica.replace("'", "''")
    cabecera = conexion.consultar_dicts(f"DBCC SHOW_STATISTICS ('{tabla_s}', '{est_s}') WITH STAT_HEADER, NO_INFOMSGS")
    densidad = conexion.consultar_dicts(f"DBCC SHOW_STATISTICS ('{tabla_s}', '{est_s}') WITH DENSITY_VECTOR, NO_INFOMSGS")
    histograma = conexion.consultar_dicts(f"DBCC SHOW_STATISTICS ('{tabla_s}', '{est_s}') WITH HISTOGRAM, NO_INFOMSGS", max_filas=200)
    return {"cabecera": cabecera[0] if cabecera else None, "densidad": densidad, "histograma": histograma}
