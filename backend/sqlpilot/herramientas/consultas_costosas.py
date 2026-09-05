"""Consultas más costosas desde el plan cache y Query Store, y obtención de planes."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta

_ORDENES = {
    "cpu": "qs.total_worker_time",
    "duracion": "qs.total_elapsed_time",
    "lecturas": "qs.total_logical_reads",
    "escrituras": "qs.total_logical_writes",
    "ejecuciones": "qs.execution_count",
    "cpu_promedio": "qs.total_worker_time / NULLIF(qs.execution_count, 0)",
    "duracion_promedio": "qs.total_elapsed_time / NULLIF(qs.execution_count, 0)",
    "lecturas_promedio": "qs.total_logical_reads / NULLIF(qs.execution_count, 0)",
}

_SQL_CACHE = """
SELECT TOP (@top)
    DB_NAME(t.dbid) AS base_datos,
    OBJECT_NAME(t.objectid, t.dbid) AS objeto,
    qs.execution_count AS ejecuciones,
    qs.total_worker_time / 1000 AS cpu_total_ms,
    qs.total_worker_time / 1000 / NULLIF(qs.execution_count, 0) AS cpu_prom_ms,
    qs.total_elapsed_time / 1000 AS duracion_total_ms,
    qs.total_elapsed_time / 1000 / NULLIF(qs.execution_count, 0) AS duracion_prom_ms,
    qs.total_logical_reads AS lecturas_total,
    qs.total_logical_reads / NULLIF(qs.execution_count, 0) AS lecturas_prom,
    qs.total_logical_writes AS escrituras_total,
    qs.max_elapsed_time / 1000 AS duracion_max_ms,
    qs.total_grant_kb / NULLIF(qs.execution_count, 0) AS grant_prom_kb,
    qs.creation_time AS plan_desde,
    qs.last_execution_time AS ultima_ejecucion,
    SUBSTRING(t.text, (qs.statement_start_offset / 2) + 1,
        ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(t.text) ELSE qs.statement_end_offset END
          - qs.statement_start_offset) / 2) + 1) AS sentencia,
    CONVERT(VARCHAR(64), qs.query_hash, 1) AS query_hash,
    CONVERT(VARCHAR(64), qs.query_plan_hash, 1) AS plan_hash,
    CONVERT(VARCHAR(140), qs.plan_handle, 1) AS plan_handle,
    qs.statement_start_offset, qs.statement_end_offset
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t
WHERE (? IS NULL OR DB_NAME(t.dbid) = ?)
ORDER BY @orden DESC;
"""

_SQL_QS_DISPONIBLE = """
SELECT actual_state_desc, readonly_reason, current_storage_size_mb, max_storage_size_mb,
       query_capture_mode_desc, stale_query_threshold_days
FROM sys.database_query_store_options;
"""

_SQL_QUERY_STORE = """
;WITH agregados AS (
    SELECT q.query_id, p.plan_id, q.object_id, p.is_forced_plan, q.query_text_id,
        SUM(rs.count_executions) AS ejecuciones,
        CAST(SUM(rs.avg_cpu_time * rs.count_executions) / 1000 AS BIGINT) AS cpu_total_ms,
        CAST(SUM(rs.avg_cpu_time * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000 AS DECIMAL(18,2)) AS cpu_prom_ms,
        CAST(SUM(rs.avg_duration * rs.count_executions) / 1000 AS BIGINT) AS duracion_total_ms,
        CAST(SUM(rs.avg_duration * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000 AS DECIMAL(18,2)) AS duracion_prom_ms,
        CAST(SUM(rs.avg_logical_io_reads * rs.count_executions) AS BIGINT) AS lecturas_total,
        CAST(SUM(rs.avg_logical_io_reads * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) AS BIGINT) AS lecturas_prom,
        MAX(rs.max_duration) / 1000 AS duracion_max_ms
    FROM sys.query_store_query q
    JOIN sys.query_store_plan p ON p.query_id = q.query_id
    JOIN sys.query_store_runtime_stats rs ON rs.plan_id = p.plan_id
    JOIN sys.query_store_runtime_stats_interval i ON i.runtime_stats_interval_id = rs.runtime_stats_interval_id
    WHERE i.start_time >= DATEADD(HOUR, -@horas, SYSUTCDATETIME())
    GROUP BY q.query_id, p.plan_id, q.object_id, p.is_forced_plan, q.query_text_id
)
SELECT TOP (@top)
    a.query_id, a.plan_id, OBJECT_NAME(a.object_id) AS objeto,
    a.ejecuciones, a.cpu_total_ms, a.cpu_prom_ms, a.duracion_total_ms, a.duracion_prom_ms,
    a.lecturas_total, a.lecturas_prom, a.duracion_max_ms, a.is_forced_plan,
    (SELECT COUNT(*) FROM sys.query_store_plan p2 WHERE p2.query_id = a.query_id) AS planes_distintos,
    LEFT(qt.query_sql_text, 1500) AS sentencia
FROM agregados a
JOIN sys.query_store_query_text qt ON qt.query_text_id = a.query_text_id
ORDER BY @orden DESC;
"""

_ORDENES_QS = {
    "cpu": "a.cpu_total_ms", "duracion": "a.duracion_total_ms", "lecturas": "a.lecturas_total", "ejecuciones": "a.ejecuciones",
    "cpu_promedio": "a.cpu_prom_ms", "duracion_promedio": "a.duracion_prom_ms", "lecturas_promedio": "a.lecturas_prom",
}


@herramienta(
    "consultas_costosas_cache",
    """Top de consultas más costosas según el plan cache (sys.dm_exec_query_stats). Acumulado desde que
    cada plan entró en caché. Ordena por cpu, duracion, lecturas, escrituras, ejecuciones o sus promedios.
    Devuelve el plan_handle para pedir luego el plan con `obtener_plan_cache`.""",
    "consultas",
    orden="Criterio: cpu | duracion | lecturas | escrituras | ejecuciones | cpu_promedio | duracion_promedio | lecturas_promedio.",
    top="Cantidad de consultas (por defecto 15).",
    base_datos="Filtrar por nombre de base de datos (opcional).",
)
def consultas_costosas_cache(conexion: ConexionSql, orden: str = "cpu", top: int = 15, base_datos: str | None = None) -> dict:
    expresion = _ORDENES.get(orden, _ORDENES["cpu"])
    sql = _SQL_CACHE.replace("@top", str(int(top))).replace("@orden", expresion)
    filas = conexion.consultar_dicts(sql, (base_datos, base_datos))
    return {"orden": orden, "consultas": filas}


@herramienta(
    "consultas_costosas_query_store",
    """Top de consultas según Query Store en las últimas N horas (más fiable que el plan cache porque
    persiste reinicios y muestra regresiones de plan: `planes_distintos` > 1). Requiere Query Store
    habilitado en la base de datos del perfil; si no lo está, lo indica.""",
    "consultas",
    orden="Criterio: cpu | duracion | lecturas | ejecuciones | cpu_promedio | duracion_promedio.",
    horas="Ventana de tiempo hacia atrás (por defecto 24).",
    top="Cantidad de consultas (por defecto 15).",
)
def consultas_costosas_query_store(conexion: ConexionSql, orden: str = "cpu", horas: int = 24, top: int = 15) -> dict:
    estado = conexion.consultar_dicts(_SQL_QS_DISPONIBLE)
    if not estado or estado[0]["actual_state_desc"] in ("OFF", None):
        return {"query_store": "deshabilitado", "sugerencia": "ALTER DATABASE [db] SET QUERY_STORE = ON (OPERATION_MODE = READ_WRITE);"}
    expresion = _ORDENES_QS.get(orden, "a.cpu_total_ms")
    sql = _SQL_QUERY_STORE.replace("@top", str(int(top))).replace("@horas", str(int(horas))).replace("@orden", expresion)
    filas = conexion.consultar_dicts(sql)
    return {"query_store": estado[0], "horas": horas, "orden": orden, "consultas": filas}


@herramienta(
    "obtener_plan_cache",
    """Devuelve el plan de ejecución XML (estimado, del plan cache) de un plan_handle obtenido en
    `consultas_costosas_cache` o `sesiones_activas`. Incluye advertencias del plan si las hay.""",
    "consultas",
    plan_handle="plan_handle en formato hexadecimal 0x....",
    statement_start_offset="Offset de inicio de la sentencia (opcional, para planes de lotes grandes).",
    statement_end_offset="Offset de fin (opcional).",
)
def obtener_plan_cache(conexion: ConexionSql, plan_handle: str, statement_start_offset: int | None = None,
                       statement_end_offset: int | None = None) -> dict:
    if statement_start_offset is not None:
        sql = "SELECT CAST(query_plan AS NVARCHAR(MAX)) AS plan_xml FROM sys.dm_exec_text_query_plan(CONVERT(VARBINARY(64), ?, 1), ?, ?)"
        filas = conexion.consultar_dicts(sql, (plan_handle, statement_start_offset, statement_end_offset or -1))
    else:
        sql = "SELECT CAST(query_plan AS NVARCHAR(MAX)) AS plan_xml FROM sys.dm_exec_query_plan(CONVERT(VARBINARY(64), ?, 1))"
        filas = conexion.consultar_dicts(sql, (plan_handle,))
    if not filas or not filas[0]["plan_xml"]:
        return {"error": "El plan ya no está en caché."}
    from sqlpilot.analisis.planes import resumir_plan
    return resumir_plan(filas[0]["plan_xml"])


@herramienta(
    "plan_estimado",
    """Obtiene el plan de ejecución ESTIMADO de un SQL o de un procedimiento almacenado sin ejecutarlo
    (SET SHOWPLAN_XML ON). Devuelve un resumen: operadores más costosos, scans, índices faltantes,
    advertencias (conversiones implícitas, spills, sin join predicate), estimaciones de filas.""",
    "consultas",
    sql="Sentencia T-SQL o 'EXEC esquema.procedimiento @param = valor'.",
)
def plan_estimado(conexion: ConexionSql, sql: str) -> dict:
    from sqlpilot.analisis.planes import resumir_plan
    from sqlpilot.db.seguridad import evaluar_sql

    # Con SHOWPLAN_XML activo el motor compila pero NO ejecuta las sentencias; aun así se rechaza
    # todo lo que no sea SELECT o EXEC de un procedimiento (sin DML/DDL sueltos).
    sql_limpio = sql.strip()
    es_exec_sp = sql_limpio.lower().startswith(("exec ", "execute ")) and "sp_executesql" not in sql_limpio.lower()
    if not es_exec_sp and not evaluar_sql(sql).permitido:
        return {"error": evaluar_sql(sql).motivo}
    cursor = conexion.conn.cursor()
    try:
        cursor.execute("SET SHOWPLAN_XML ON")
        cursor.execute(sql)
        planes = []
        while True:
            if cursor.description:
                for fila in cursor.fetchall():
                    planes.append(fila[0])
            if not cursor.nextset():
                break
        cursor.execute("SET SHOWPLAN_XML OFF")
    finally:
        cursor.close()
    if not planes:
        return {"error": "No se obtuvo plan."}
    return resumir_plan(planes[0])
