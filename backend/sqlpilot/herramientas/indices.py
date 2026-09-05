"""Índices: faltantes, no usados, duplicados/solapados, fragmentación."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta


@herramienta(
    "indices_faltantes",
    """Índices faltantes sugeridos por el optimizador (sys.dm_db_missing_index_*) en la base de datos actual,
    ordenados por impacto estimado (avg_user_impact * seeks * avg_total_user_cost). Incluye el script
    CREATE INDEX propuesto. Ojo: son sugerencias del optimizador, hay que revisarlas (solapamientos,
    demasiadas columnas INCLUDE, tablas con mucha escritura).""",
    "indices",
    top="Cantidad a devolver (por defecto 20).",
    tabla="Filtrar por nombre de tabla (opcional).",
)
def indices_faltantes(conexion: ConexionSql, top: int = 20, tabla: str | None = None) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT TOP (?)
            OBJECT_SCHEMA_NAME(d.object_id) + '.' + OBJECT_NAME(d.object_id) AS tabla,
            d.equality_columns, d.inequality_columns, d.included_columns,
            s.user_seeks, s.user_scans, s.avg_user_impact, s.avg_total_user_cost, s.last_user_seek,
            CAST(s.avg_user_impact * s.avg_total_user_cost * (s.user_seeks + s.user_scans) AS BIGINT) AS impacto,
            'CREATE NONCLUSTERED INDEX [IX_' + OBJECT_NAME(d.object_id) + '_'
              + REPLACE(REPLACE(REPLACE(ISNULL(d.equality_columns, '') + ISNULL('_' + d.inequality_columns, ''), '[', ''), ']', ''), ', ', '_')
              + '] ON ' + d.statement + ' (' + ISNULL(d.equality_columns, '')
              + CASE WHEN d.equality_columns IS NOT NULL AND d.inequality_columns IS NOT NULL THEN ', ' ELSE '' END
              + ISNULL(d.inequality_columns, '') + ')'
              + ISNULL(' INCLUDE (' + d.included_columns + ')', '') + ' WITH (ONLINE = ON);' AS script
        FROM sys.dm_db_missing_index_details d
        JOIN sys.dm_db_missing_index_groups g ON g.index_handle = d.index_handle
        JOIN sys.dm_db_missing_index_group_stats s ON s.group_handle = g.index_group_handle
        WHERE d.database_id = DB_ID()
          AND (? IS NULL OR OBJECT_NAME(d.object_id) = ?)
        ORDER BY impacto DESC
        """,
        (int(top), tabla, tabla),
    )
    return {"base_datos": conexion.perfil.base_datos, "indices_faltantes": filas}


@herramienta(
    "indices_no_usados",
    """Índices no clustered que no reciben seeks/scans/lookups pero sí actualizaciones desde el último
    reinicio (sys.dm_db_index_usage_stats). Candidatos a eliminar para reducir costo de escritura.
    Verifica el tiempo desde el reinicio antes de concluir.""",
    "indices",
    top="Cantidad a devolver.",
    min_escrituras="Mínimo de escrituras para incluir el índice (por defecto 1).",
)
def indices_no_usados(conexion: ConexionSql, top: int = 30, min_escrituras: int = 1) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT TOP (?)
            OBJECT_SCHEMA_NAME(i.object_id) + '.' + OBJECT_NAME(i.object_id) AS tabla,
            i.name AS indice, i.type_desc, i.is_unique,
            ISNULL(u.user_seeks, 0) AS seeks, ISNULL(u.user_scans, 0) AS scans, ISNULL(u.user_lookups, 0) AS lookups,
            ISNULL(u.user_updates, 0) AS escrituras,
            ps.row_count AS filas,
            ps.used_page_count * 8 / 1024 AS tamano_mb,
            'DROP INDEX [' + i.name + '] ON ' + OBJECT_SCHEMA_NAME(i.object_id) + '.[' + OBJECT_NAME(i.object_id) + '];' AS script
        FROM sys.indexes i
        JOIN sys.objects o ON o.object_id = i.object_id AND o.type = 'U'
        LEFT JOIN sys.dm_db_index_usage_stats u ON u.object_id = i.object_id AND u.index_id = i.index_id AND u.database_id = DB_ID()
        LEFT JOIN (SELECT object_id, index_id, SUM(row_count) AS row_count, SUM(used_page_count) AS used_page_count
                   FROM sys.dm_db_partition_stats GROUP BY object_id, index_id) ps
               ON ps.object_id = i.object_id AND ps.index_id = i.index_id
        WHERE i.type_desc = 'NONCLUSTERED' AND i.is_primary_key = 0 AND i.is_unique_constraint = 0
          AND ISNULL(u.user_seeks, 0) + ISNULL(u.user_scans, 0) + ISNULL(u.user_lookups, 0) = 0
          AND ISNULL(u.user_updates, 0) >= ?
        ORDER BY escrituras DESC, tamano_mb DESC
        """,
        (int(top), int(min_escrituras)),
    )
    desde = conexion.escalar("SELECT sqlserver_start_time FROM sys.dm_os_sys_info")
    return {"estadisticas_desde": desde, "indices_no_usados": filas}


@herramienta(
    "indices_duplicados",
    "Índices duplicados o solapados (misma columna inicial y clave contenida en otro índice) en la base de datos actual.",
    "indices",
)
def indices_duplicados(conexion: ConexionSql) -> dict:
    filas = conexion.consultar_dicts(
        """
        ;WITH claves AS (
            SELECT i.object_id, i.index_id, i.name, i.type_desc, i.is_unique,
                   STUFF((SELECT ',' + c.name FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                          WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 0
                          ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 1, '') AS columnas_clave,
                   STUFF((SELECT ',' + c.name FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                          WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 1
                          ORDER BY c.name FOR XML PATH('')), 1, 1, '') AS columnas_incluidas
            FROM sys.indexes i JOIN sys.objects o ON o.object_id = i.object_id
            WHERE o.type = 'U' AND i.type > 0
        )
        SELECT OBJECT_SCHEMA_NAME(a.object_id) + '.' + OBJECT_NAME(a.object_id) AS tabla,
               a.name AS indice, a.columnas_clave, a.columnas_incluidas,
               b.name AS solapado_por, b.columnas_clave AS clave_del_otro, b.columnas_incluidas AS incluidas_del_otro,
               CASE WHEN a.columnas_clave = b.columnas_clave THEN 'duplicado' ELSE 'prefijo' END AS tipo
        FROM claves a
        JOIN claves b ON a.object_id = b.object_id AND a.index_id <> b.index_id
             AND (b.columnas_clave = a.columnas_clave OR b.columnas_clave LIKE a.columnas_clave + ',%')
        WHERE a.type_desc = 'NONCLUSTERED'
        ORDER BY tabla, a.name
        """
    )
    return {"total": len(filas), "solapamientos": filas}


@herramienta(
    "fragmentacion_indices",
    """Fragmentación de índices (modo LIMITED) para índices con más de `min_paginas` páginas.
    Costoso en bases grandes: limita por tabla cuando puedas. Regla clásica: > 30% REBUILD, 5-30% REORGANIZE,
    aunque en SSD la fragmentación lógica importa menos que la densidad de página.""",
    "indices",
    tabla="Nombre de tabla (opcional; sin ella analiza toda la base).",
    min_paginas="Mínimo de páginas del índice (por defecto 1000).",
    top="Cantidad a devolver.",
)
def fragmentacion_indices(conexion: ConexionSql, tabla: str | None = None, min_paginas: int = 1000, top: int = 30) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT TOP (?)
            OBJECT_SCHEMA_NAME(ps.object_id) + '.' + OBJECT_NAME(ps.object_id) AS tabla, i.name AS indice, i.type_desc,
            ps.partition_number, CAST(ps.avg_fragmentation_in_percent AS DECIMAL(5,2)) AS fragmentacion_pct,
            ps.page_count AS paginas, ps.page_count * 8 / 1024 AS tamano_mb,
            CASE WHEN ps.avg_fragmentation_in_percent > 30 THEN 'REBUILD' WHEN ps.avg_fragmentation_in_percent > 5 THEN 'REORGANIZE' ELSE '' END AS accion
        FROM sys.dm_db_index_physical_stats(DB_ID(), OBJECT_ID(?), NULL, NULL, 'LIMITED') ps
        JOIN sys.indexes i ON i.object_id = ps.object_id AND i.index_id = ps.index_id
        WHERE ps.page_count >= ? AND ps.index_id > 0
        ORDER BY ps.avg_fragmentation_in_percent DESC
        """,
        (int(top), tabla, int(min_paginas)),
        max_filas=None,
    )
    return {"tabla": tabla, "indices": filas}


@herramienta(
    "indices_de_tabla",
    "Todos los índices de una tabla con sus columnas clave/incluidas, tamaño, filas y uso (seeks/scans/updates).",
    "indices",
    tabla="Nombre de la tabla, con o sin esquema (ej. dbo.Despachos).",
)
def indices_de_tabla(conexion: ConexionSql, tabla: str) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT i.index_id, i.name AS indice, i.type_desc, i.is_unique, i.is_primary_key, i.filter_definition,
               STUFF((SELECT ', ' + c.name + CASE WHEN ic.is_descending_key = 1 THEN ' DESC' ELSE '' END
                      FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                      WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 0
                      ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 2, '') AS columnas_clave,
               STUFF((SELECT ', ' + c.name FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                      WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 1
                      FOR XML PATH('')), 1, 2, '') AS columnas_incluidas,
               ps.row_count AS filas, ps.used_page_count * 8 / 1024 AS tamano_mb,
               ISNULL(u.user_seeks, 0) AS seeks, ISNULL(u.user_scans, 0) AS scans, ISNULL(u.user_lookups, 0) AS lookups, ISNULL(u.user_updates, 0) AS escrituras,
               u.last_user_seek, u.last_user_scan
        FROM sys.indexes i
        LEFT JOIN (SELECT object_id, index_id, SUM(row_count) AS row_count, SUM(used_page_count) AS used_page_count
                   FROM sys.dm_db_partition_stats GROUP BY object_id, index_id) ps ON ps.object_id = i.object_id AND ps.index_id = i.index_id
        LEFT JOIN sys.dm_db_index_usage_stats u ON u.object_id = i.object_id AND u.index_id = i.index_id AND u.database_id = DB_ID()
        WHERE i.object_id = OBJECT_ID(?)
        ORDER BY i.index_id
        """,
        (tabla,),
    )
    if not filas:
        return {"error": f"No se encontró la tabla '{tabla}' en {conexion.perfil.base_datos}."}
    return {"tabla": tabla, "indices": filas}
