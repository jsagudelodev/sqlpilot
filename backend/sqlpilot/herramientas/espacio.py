"""Espacio: archivos, logs, tempdb, tablas más grandes, crecimiento."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta


@herramienta(
    "espacio_bases_datos",
    """Tamaño y espacio libre de datos y log de todas las bases, estado, modelo de recuperación,
    log_reuse_wait (por qué no se trunca el log), y porcentaje de log usado.""",
    "espacio",
)
def espacio_bases_datos(conexion: ConexionSql) -> dict:
    # Tamaños desde contadores de rendimiento (solo requiere VIEW SERVER STATE; sys.master_files puede estar
    # vacío para logins sin VIEW ANY DEFINITION, p. ej. en Azure SQL Managed Instance).
    filas = conexion.consultar_dicts(
        """
        ;WITH c AS (
            SELECT RTRIM(instance_name) AS base_datos, RTRIM(counter_name) AS contador, cntr_value
            FROM sys.dm_os_performance_counters
            WHERE object_name LIKE '%Databases%'
              AND counter_name IN ('Data File(s) Size (KB)', 'Log File(s) Size (KB)', 'Log File(s) Used Size (KB)', 'Percent Log Used')
        )
        SELECT d.name AS base_datos, d.state_desc AS estado, d.recovery_model_desc AS recuperacion,
               d.log_reuse_wait_desc AS log_reuse_wait, d.compatibility_level, d.is_read_only,
               d.is_auto_close_on, d.is_auto_shrink_on, d.page_verify_option_desc,
               ISNULL(mf.datos_mb, dat.cntr_value / 1024) AS datos_mb,
               ISNULL(mf.log_mb, lt.cntr_value / 1024) AS log_mb,
               ls.cntr_value / 1024 AS log_usado_mb,
               CAST(100.0 * ls.cntr_value / NULLIF(lt.cntr_value, 0) AS DECIMAL(5,2)) AS log_usado_pct,
               HAS_DBACCESS(d.name) AS acceso
        FROM sys.databases d
        LEFT JOIN (SELECT database_id, SUM(CASE WHEN type = 0 THEN size END) * 8 / 1024 AS datos_mb,
                          SUM(CASE WHEN type = 1 THEN size END) * 8 / 1024 AS log_mb
                   FROM sys.master_files GROUP BY database_id) mf ON mf.database_id = d.database_id
        LEFT JOIN c dat ON dat.base_datos = d.name AND dat.contador = 'Data File(s) Size (KB)'
        LEFT JOIN c lt  ON lt.base_datos = d.name AND lt.contador = 'Log File(s) Size (KB)'
        LEFT JOIN c ls  ON ls.base_datos = d.name AND ls.contador = 'Log File(s) Used Size (KB)'
        ORDER BY ISNULL(mf.datos_mb, dat.cntr_value / 1024) DESC
        """,
        max_filas=None,
    )
    return {"bases_datos": filas}


@herramienta(
    "archivos_base_datos",
    "Archivos de la base de datos actual: ruta, tamaño, espacio usado/libre, crecimiento, tamaño máximo.",
    "espacio",
)
def archivos_base_datos(conexion: ConexionSql) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT f.name AS nombre_logico, f.type_desc, f.physical_name AS ruta, fg.name AS filegroup,
               f.size * 8 / 1024 AS tamano_mb,
               CAST(FILEPROPERTY(f.name, 'SpaceUsed') AS BIGINT) * 8 / 1024 AS usado_mb,
               (f.size - CAST(FILEPROPERTY(f.name, 'SpaceUsed') AS BIGINT)) * 8 / 1024 AS libre_mb,
               CASE WHEN f.is_percent_growth = 1 THEN CAST(f.growth AS VARCHAR) + ' %' ELSE CAST(f.growth * 8 / 1024 AS VARCHAR) + ' MB' END AS crecimiento,
               CASE f.max_size WHEN -1 THEN 'ilimitado' WHEN 0 THEN 'sin crecimiento' ELSE CAST(f.max_size * 8 / 1024 AS VARCHAR) + ' MB' END AS maximo,
               vs.available_bytes / 1048576 AS libre_en_disco_mb
        FROM sys.database_files f
        LEFT JOIN sys.filegroups fg ON fg.data_space_id = f.data_space_id
        CROSS APPLY sys.dm_os_volume_stats(DB_ID(), f.file_id) vs
        """
    )
    return {"base_datos": conexion.perfil.base_datos, "archivos": filas}


@herramienta(
    "tablas_mas_grandes",
    "Tablas más grandes de la base de datos actual por espacio total (datos + índices), con filas y cantidad de índices.",
    "espacio",
    top="Cantidad a devolver (por defecto 20).",
)
def tablas_mas_grandes(conexion: ConexionSql, top: int = 20) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT TOP (?)
            s.name + '.' + t.name AS tabla,
            SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.row_count END) AS filas,
            SUM(ps.reserved_page_count) * 8 / 1024 AS total_mb,
            SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.used_page_count END) * 8 / 1024 AS datos_mb,
            SUM(CASE WHEN ps.index_id > 1 THEN ps.used_page_count END) * 8 / 1024 AS indices_mb,
            (SELECT COUNT(*) FROM sys.indexes i WHERE i.object_id = t.object_id AND i.index_id > 0) AS cantidad_indices,
            MAX(CASE WHEN ps.index_id = 0 THEN 1 ELSE 0 END) AS es_heap
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
        GROUP BY s.name, t.name, t.object_id
        ORDER BY total_mb DESC
        """,
        (int(top),),
    )
    return {"base_datos": conexion.perfil.base_datos, "tablas": filas}


@herramienta(
    "estado_tempdb",
    "Uso de tempdb: espacio por tipo (user objects, internal, version store), archivos y sesiones que más tempdb consumen.",
    "espacio",
)
def estado_tempdb(conexion: ConexionSql) -> dict:
    uso = conexion.consultar_dicts(
        """
        SELECT SUM(user_object_reserved_page_count) * 8 / 1024 AS objetos_usuario_mb,
               SUM(internal_object_reserved_page_count) * 8 / 1024 AS objetos_internos_mb,
               SUM(version_store_reserved_page_count) * 8 / 1024 AS version_store_mb,
               SUM(unallocated_extent_page_count) * 8 / 1024 AS libre_mb,
               SUM(total_page_count) * 8 / 1024 AS total_mb
        FROM tempdb.sys.dm_db_file_space_usage
        """
    )
    archivos = conexion.consultar_dicts(
        "SELECT name, type_desc, physical_name, size * 8 / 1024 AS tamano_mb, growth, is_percent_growth "
        "FROM tempdb.sys.database_files"
    )
    sesiones = conexion.consultar_dicts(
        """
        SELECT TOP 10 su.session_id, s.login_name, s.program_name,
               (su.user_objects_alloc_page_count + su.internal_objects_alloc_page_count) * 8 / 1024 AS asignado_mb,
               (su.user_objects_dealloc_page_count + su.internal_objects_dealloc_page_count) * 8 / 1024 AS liberado_mb
        FROM sys.dm_db_session_space_usage su
        JOIN sys.dm_exec_sessions s ON s.session_id = su.session_id
        WHERE s.is_user_process = 1
        ORDER BY (su.user_objects_alloc_page_count + su.internal_objects_alloc_page_count)
               - (su.user_objects_dealloc_page_count + su.internal_objects_dealloc_page_count) DESC
        """
    )
    return {"uso": uso[0] if uso else None, "archivos": archivos, "sesiones_top": sesiones}
