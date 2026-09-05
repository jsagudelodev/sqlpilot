"""Planes en Query Store: regresiones, planes por consulta, comparación A/B y plan real con rollback."""

from __future__ import annotations

import re

from sqlpilot.analisis.comparar import comparar_planes
from sqlpilot.analisis.planes import resumir_plan
from sqlpilot.db.conexion import ConexionSql
from sqlpilot.db.seguridad import evaluar_sql
from sqlpilot.herramientas.base import herramienta

_SQL_QS_ESTADO = "SELECT actual_state_desc FROM sys.database_query_store_options"


def _qs_activo(conexion: ConexionSql) -> bool:
    estado = conexion.escalar(_SQL_QS_ESTADO)
    return estado not in (None, "OFF")


def _sin_qs() -> dict:
    return {"query_store": "deshabilitado",
            "sugerencia": "ALTER DATABASE [db] SET QUERY_STORE = ON (OPERATION_MODE = READ_WRITE);"}


@herramienta(
    "regresiones_query_store",
    """Consultas cuyo plan más reciente rinde peor que un plan anterior (Query Store, últimas N horas).
    Detecta regresiones de plan (parameter sniffing, cambio de estadísticas, índice nuevo/eliminado).
    Devuelve ambos plan_id para compararlos con `comparar_planes_query_store`.""",
    "planes",
    horas="Ventana de análisis (por defecto 48).",
    min_ejecuciones="Mínimo de ejecuciones de cada plan para considerarlo (por defecto 5).",
    top="Cantidad de regresiones a devolver.",
)
def regresiones_query_store(conexion: ConexionSql, horas: int = 48, min_ejecuciones: int = 5, top: int = 15) -> dict:
    if not _qs_activo(conexion):
        return _sin_qs()
    filas = conexion.consultar_dicts(
        """
        ;WITH stats AS (
            SELECT p.query_id, p.plan_id, p.last_execution_time,
                   SUM(rs.count_executions) AS ejecuciones,
                   SUM(rs.avg_duration * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000.0 AS duracion_prom_ms,
                   SUM(rs.avg_cpu_time * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000.0 AS cpu_prom_ms,
                   SUM(rs.avg_logical_io_reads * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) AS lecturas_prom
            FROM sys.query_store_plan p
            JOIN sys.query_store_runtime_stats rs ON rs.plan_id = p.plan_id
            JOIN sys.query_store_runtime_stats_interval i ON i.runtime_stats_interval_id = rs.runtime_stats_interval_id
            WHERE i.start_time >= DATEADD(HOUR, -?, SYSUTCDATETIME())
            GROUP BY p.query_id, p.plan_id, p.last_execution_time
            HAVING SUM(rs.count_executions) >= ?
        ), ranking AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY query_id ORDER BY last_execution_time DESC) AS rn_reciente,
                      ROW_NUMBER() OVER (PARTITION BY query_id ORDER BY duracion_prom_ms ASC) AS rn_mejor
            FROM stats
        )
        SELECT TOP (?)
            r.query_id, OBJECT_NAME(q.object_id) AS objeto,
            r.plan_id AS plan_reciente, m.plan_id AS plan_mejor,
            CAST(r.duracion_prom_ms AS DECIMAL(18,2)) AS reciente_duracion_ms, CAST(m.duracion_prom_ms AS DECIMAL(18,2)) AS mejor_duracion_ms,
            CAST(r.duracion_prom_ms / NULLIF(m.duracion_prom_ms, 0) AS DECIMAL(10,2)) AS veces_mas_lento,
            CAST(r.cpu_prom_ms AS DECIMAL(18,2)) AS reciente_cpu_ms, CAST(m.cpu_prom_ms AS DECIMAL(18,2)) AS mejor_cpu_ms,
            CAST(r.lecturas_prom AS BIGINT) AS reciente_lecturas, CAST(m.lecturas_prom AS BIGINT) AS mejor_lecturas,
            r.ejecuciones AS reciente_ejecuciones, m.ejecuciones AS mejor_ejecuciones,
            LEFT(qt.query_sql_text, 400) AS sentencia
        FROM ranking r
        JOIN ranking m ON m.query_id = r.query_id AND m.rn_mejor = 1
        JOIN sys.query_store_query q ON q.query_id = r.query_id
        JOIN sys.query_store_query_text qt ON qt.query_text_id = q.query_text_id
        WHERE r.rn_reciente = 1 AND r.plan_id <> m.plan_id AND r.duracion_prom_ms > m.duracion_prom_ms * 1.5
        ORDER BY veces_mas_lento DESC
        """,
        (int(horas), int(min_ejecuciones), int(top)),
    )
    return {"horas": horas, "regresiones": filas}


@herramienta(
    "planes_de_consulta_query_store",
    "Todos los planes que Query Store tiene para un query_id (u objeto), con estadísticas por plan: ejecuciones, duración, CPU, lecturas, forzado.",
    "planes",
    query_id="query_id de Query Store (opcional si das objeto).",
    objeto="Nombre del SP/función (opcional; devuelve los planes de todas sus sentencias).",
)
def planes_de_consulta_query_store(conexion: ConexionSql, query_id: int | None = None, objeto: str | None = None) -> dict:
    if not _qs_activo(conexion):
        return _sin_qs()
    if query_id is None and not objeto:
        return {"error": "Indica query_id u objeto."}
    filas = conexion.consultar_dicts(
        """
        SELECT q.query_id, p.plan_id, OBJECT_NAME(q.object_id) AS objeto, p.is_forced_plan, p.force_failure_count,
               p.compatibility_level, p.engine_version, p.initial_compile_start_time, p.last_execution_time, p.count_compiles,
               SUM(rs.count_executions) AS ejecuciones,
               CAST(SUM(rs.avg_duration * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000.0 AS DECIMAL(18,2)) AS duracion_prom_ms,
               CAST(SUM(rs.avg_cpu_time * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000.0 AS DECIMAL(18,2)) AS cpu_prom_ms,
               CAST(SUM(rs.avg_logical_io_reads * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) AS BIGINT) AS lecturas_prom,
               CAST(MAX(rs.max_duration) / 1000.0 AS DECIMAL(18,2)) AS duracion_max_ms,
               LEFT(qt.query_sql_text, 300) AS sentencia
        FROM sys.query_store_query q
        JOIN sys.query_store_query_text qt ON qt.query_text_id = q.query_text_id
        JOIN sys.query_store_plan p ON p.query_id = q.query_id
        LEFT JOIN sys.query_store_runtime_stats rs ON rs.plan_id = p.plan_id
        WHERE (? IS NULL OR q.query_id = ?) AND (? IS NULL OR q.object_id = OBJECT_ID(?))
        GROUP BY q.query_id, p.plan_id, q.object_id, p.is_forced_plan, p.force_failure_count, p.compatibility_level, p.engine_version,
                 p.initial_compile_start_time, p.last_execution_time, p.count_compiles, qt.query_sql_text
        ORDER BY q.query_id, p.last_execution_time DESC
        """,
        (query_id, query_id, objeto, objeto),
        max_filas=None,
    )
    return {"query_id": query_id, "objeto": objeto, "planes": filas}


@herramienta(
    "comparar_planes_query_store",
    """Compara dos planes de Query Store (plan_id A vs B): costo, paralelismo, operadores que aparecen/desaparecen,
    scans, lookups, índices faltantes, advertencias y valores de parámetros compilados (sniffing).
    Úsalo tras `regresiones_query_store` o `planes_de_consulta_query_store`.""",
    "planes",
    plan_id_a="plan_id del plan de referencia (normalmente el bueno).",
    plan_id_b="plan_id del plan a comparar (normalmente el reciente/malo).",
)
def comparar_planes_query_store(conexion: ConexionSql, plan_id_a: int, plan_id_b: int) -> dict:
    if not _qs_activo(conexion):
        return _sin_qs()
    planes = {
        f["plan_id"]: f["plan_xml"]
        for f in conexion.consultar_dicts(
            "SELECT plan_id, CAST(query_plan AS NVARCHAR(MAX)) AS plan_xml FROM sys.query_store_plan WHERE plan_id IN (?, ?)",
            (int(plan_id_a), int(plan_id_b)),
        )
    }
    faltan = [p for p in (plan_id_a, plan_id_b) if p not in planes]
    if faltan:
        return {"error": f"No existen en Query Store los plan_id {faltan}."}
    resumen_a, resumen_b = resumir_plan(planes[plan_id_a]), resumir_plan(planes[plan_id_b])
    return {
        "comparacion": comparar_planes(resumen_a, resumen_b, f"plan_{plan_id_a}", f"plan_{plan_id_b}"),
        f"plan_{plan_id_a}": resumen_a,
        f"plan_{plan_id_b}": resumen_b,
        "nota": "Si el plan bueno es estable, el DBA puede forzarlo: EXEC sp_query_store_force_plan @query_id, @plan_id (propónlo, no lo ejecutes).",
    }


@herramienta(
    "comparar_plan_estimado",
    "Compara el plan estimado de dos SQL (por ejemplo la versión actual de una consulta y una reescritura propuesta) sin ejecutarlos.",
    "planes",
    sql_a="Consulta o EXEC de referencia.",
    sql_b="Consulta o EXEC alternativo.",
)
def comparar_plan_estimado(conexion: ConexionSql, sql_a: str, sql_b: str) -> dict:
    from sqlpilot.herramientas.consultas_costosas import plan_estimado

    a, b = plan_estimado(conexion, sql_a), plan_estimado(conexion, sql_b)
    if "error" in a or "error" in b:
        return {"error_a": a.get("error"), "error_b": b.get("error")}
    return {"comparacion": comparar_planes(a, b, "A", "B"), "plan_A": a, "plan_B": b}


# --------------------------------------------------------------------------- plan real
_DML = re.compile(r"\b(insert|update|delete|merge|truncate|alter|create|drop|exec(ute)?\s+(?!sp_executesql\s+n?'\s*select)|sp_executesql|"
                  r"openrowset|xp_|bulk|into\s+#?\w)\b", re.IGNORECASE)


def _sp_es_solo_lectura(conexion: ConexionSql, nombre: str) -> tuple[bool, str]:
    """Un SP se considera de lectura si su definición no contiene DML/DDL ni EXEC, y ninguna dependencia es escrita."""
    definicion = conexion.escalar("SELECT definition FROM sys.sql_modules WHERE object_id = OBJECT_ID(?)", (nombre,))
    if definicion is None:
        return False, f"No se encontró el procedimiento '{nombre}'."
    sin_comentarios = re.sub(r"(--[^\n]*)|(/\*.*?\*/)", " ", definicion, flags=re.DOTALL)
    sin_strings = re.sub(r"'(?:''|[^'])*'", "''", sin_comentarios)
    m = _DML.search(sin_strings)
    if m:
        return False, f"El procedimiento contiene '{m.group(0).strip()}'; solo se obtiene plan real de SPs de lectura pura."
    escribe = conexion.consultar_dicts(
        "SELECT referenced_entity_name FROM sys.dm_sql_referenced_entities(?, 'OBJECT') WHERE is_updated = 1",
        (nombre if "." in nombre else f"dbo.{nombre}",),
    )
    if escribe:
        return False, f"El procedimiento escribe en: {[e['referenced_entity_name'] for e in escribe]}."
    return True, ""


@herramienta(
    "plan_real",
    """Ejecuta un SELECT (o un SP de LECTURA PURA) y devuelve el plan REAL con filas reales vs. estimadas,
    spills, tiempo y lecturas por operador (SET STATISTICS XML ON). Para diagnosticar parameter sniffing y
    estimaciones erradas. Seguridad: solo acepta SELECT o SPs sin DML/DDL/EXEC en su código, se ejecuta dentro
    de una transacción que SIEMPRE se revierte, con timeout y sin devolver el conjunto de resultados.""",
    "planes",
    sql="SELECT ... o EXEC esquema.procedimiento @param = valor.",
    timeout_segundos="Tiempo máximo de ejecución (por defecto 60).",
)
def plan_real(conexion: ConexionSql, sql: str, timeout_segundos: int = 60) -> dict:
    limpio = sql.strip()
    es_exec = limpio.lower().startswith(("exec ", "execute "))
    if es_exec:
        nombre = re.split(r"\s+", limpio, maxsplit=2)[1].rstrip(";")
        ok, motivo = _sp_es_solo_lectura(conexion, nombre)
        if not ok:
            return {"error": motivo, "alternativa": "Usa plan_estimado, que no ejecuta nada."}
    else:
        veredicto = evaluar_sql(sql)
        if not veredicto.permitido:
            return {"error": veredicto.motivo}

    conn = conexion.conn
    cursor = conexion.cursor()
    planes: list[str] = []
    filas_leidas = 0
    try:
        conn.autocommit = False
        conn.timeout = max(1, int(timeout_segundos))
        cursor.execute("SET STATISTICS XML ON")
        cursor.execute(sql)
        while True:
            if cursor.description:
                lote = cursor.fetchall()
                # El plan llega como un conjunto de una sola columna XML <ShowPlanXML ...>
                if len(cursor.description) == 1 and lote and isinstance(lote[0][0], str) and lote[0][0].lstrip().startswith("<ShowPlanXML"):
                    planes.extend(f[0] for f in lote)
                else:
                    filas_leidas += len(lote)
            if not cursor.nextset():
                break
        cursor.execute("SET STATISTICS XML OFF")
    finally:
        try:
            conn.rollback()
        finally:
            conn.autocommit = True
            conn.timeout = conexion.perfil.timeout_consulta
            cursor.close()
    if not planes:
        return {"error": "No se obtuvo plan real."}
    resumen = resumir_plan(planes[-1] if es_exec else planes[0])
    # Filas reales vs estimadas por operador (lo más valioso del plan real)
    desviaciones = []
    for s in resumen.get("sentencias", []):
        for op in s.get("operadores_costosos", []):
            est, real = op.get("filas_estimadas"), op.get("filas_reales")
            if est and real is not None and (real > est * 10 or (real < est / 10 and est > 100)):
                desviaciones.append({"operador": op["operador"], "objeto": op.get("objeto"), "estimadas": est, "reales": real,
                                     "factor": round(real / est, 1) if est else None})
    return {"transaccion": "revertida (ROLLBACK)", "filas_devueltas_por_la_consulta": filas_leidas,
            "planes_capturados": len(planes), "desviaciones_estimacion": desviaciones, **resumen}
