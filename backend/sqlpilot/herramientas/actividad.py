"""Actividad en vivo: sesiones activas, bloqueos, cadena de bloqueo, transacciones abiertas."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta

_SQL_ACTIVAS = """
SELECT TOP (@top)
    r.session_id,
    r.blocking_session_id,
    s.login_name,
    s.host_name,
    s.program_name,
    DB_NAME(r.database_id) AS base_datos,
    r.status,
    r.command,
    r.wait_type,
    r.wait_time AS wait_ms,
    r.wait_resource,
    r.cpu_time AS cpu_ms,
    r.total_elapsed_time AS transcurrido_ms,
    r.logical_reads,
    r.writes,
    r.open_transaction_count,
    r.percent_complete,
    SUBSTRING(t.text,
        (r.statement_start_offset / 2) + 1,
        ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(t.text) ELSE r.statement_end_offset END
          - r.statement_start_offset) / 2) + 1) AS sentencia_actual,
    OBJECT_NAME(t.objectid, t.dbid) AS objeto,
    r.plan_handle
FROM sys.dm_exec_requests r
JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE s.is_user_process = 1
  AND r.session_id <> @@SPID
  AND (@solo_bloqueadas = 0 OR r.blocking_session_id <> 0)
ORDER BY r.total_elapsed_time DESC;
"""

_SQL_BLOQUEADORES = """
;WITH activas AS (
    SELECT r.session_id, r.blocking_session_id
    FROM sys.dm_exec_requests r WHERE r.blocking_session_id <> 0
)
SELECT
    s.session_id AS head_blocker,
    s.login_name, s.host_name, s.program_name,
    s.status,
    s.last_request_start_time,
    s.last_request_end_time,
    DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) AS segundos_ocioso,
    tr.open_transaction_count AS transacciones_abiertas,
    (SELECT COUNT(*) FROM activas a WHERE a.blocking_session_id = s.session_id) AS sesiones_bloqueadas_directas,
    ib.event_info AS ultimo_lote
FROM sys.dm_exec_sessions s
LEFT JOIN (SELECT session_id, COUNT(*) AS open_transaction_count FROM sys.dm_tran_session_transactions GROUP BY session_id) tr
    ON tr.session_id = s.session_id
OUTER APPLY sys.dm_exec_input_buffer(s.session_id, NULL) ib
WHERE s.session_id IN (SELECT blocking_session_id FROM activas)
  AND s.session_id NOT IN (SELECT session_id FROM activas);
"""

_SQL_TRANSACCIONES = """
SELECT
    st.session_id,
    s.login_name, s.host_name, s.program_name,
    DB_NAME(dt.database_id) AS base_datos,
    at.transaction_begin_time AS inicio,
    DATEDIFF(SECOND, at.transaction_begin_time, GETDATE()) AS segundos_abierta,
    CASE at.transaction_state WHEN 0 THEN 'no inicializada' WHEN 1 THEN 'no iniciada' WHEN 2 THEN 'activa'
        WHEN 3 THEN 'terminada' WHEN 4 THEN 'commit en curso' WHEN 5 THEN 'preparada' WHEN 6 THEN 'commit'
        WHEN 7 THEN 'rollback' WHEN 8 THEN 'rollback en curso' END AS estado,
    dt.database_transaction_log_bytes_used AS log_bytes_usados,
    s.status AS estado_sesion,
    ib.event_info AS ultimo_lote
FROM sys.dm_tran_session_transactions st
JOIN sys.dm_tran_active_transactions at ON at.transaction_id = st.transaction_id
LEFT JOIN sys.dm_tran_database_transactions dt ON dt.transaction_id = st.transaction_id
JOIN sys.dm_exec_sessions s ON s.session_id = st.session_id
OUTER APPLY sys.dm_exec_input_buffer(s.session_id, NULL) ib
WHERE s.is_user_process = 1
ORDER BY at.transaction_begin_time;
"""


@herramienta(
    "sesiones_activas",
    """Lista las peticiones que están ejecutándose ahora mismo (equivalente a sp_WhoIsActive):
    sesión, quién bloquea a quién, esperas, CPU, lecturas, sentencia actual. Úsala como primer paso
    ante 'el servidor está lento' o 'hay bloqueos'.""",
    "actividad",
    top="Máximo de sesiones a devolver (por defecto 30).",
    solo_bloqueadas="Si es true, devuelve únicamente sesiones bloqueadas por otra.",
)
def sesiones_activas(conexion: ConexionSql, top: int = 30, solo_bloqueadas: bool = False) -> dict:
    filas = conexion.consultar_dicts(
        _SQL_ACTIVAS.replace("@top", str(int(top))).replace("@solo_bloqueadas", "1" if solo_bloqueadas else "0")
    )
    for f in filas:
        f.pop("plan_handle", None)
        if f.get("sentencia_actual"):
            f["sentencia_actual"] = f["sentencia_actual"][:1500]
    return {"total": len(filas), "sesiones": filas}


@herramienta(
    "cadena_bloqueos",
    """Identifica los *head blockers*: sesiones que bloquean a otras y no están bloqueadas por nadie.
    Muestra si están ociosas con transacción abierta (caso típico: aplicación que no hizo COMMIT),
    cuántas sesiones tienen detrás y su último lote ejecutado.""",
    "actividad",
)
def cadena_bloqueos(conexion: ConexionSql) -> dict:
    cabezas = conexion.consultar_dicts(_SQL_BLOQUEADORES)
    bloqueadas = conexion.consultar_dicts(
        _SQL_ACTIVAS.replace("@top", "200").replace("@solo_bloqueadas", "1")
    )
    for f in bloqueadas:
        f.pop("plan_handle", None)
        if f.get("sentencia_actual"):
            f["sentencia_actual"] = f["sentencia_actual"][:800]
    return {
        "head_blockers": cabezas,
        "sesiones_bloqueadas": bloqueadas,
        "nota": "Para liberar un head blocker ocioso con transacción abierta se usa KILL <session_id>; "
                "SQLPilot solo lo propone, el DBA lo ejecuta.",
    }


@herramienta(
    "transacciones_abiertas",
    "Transacciones de usuario abiertas en la instancia, con antigüedad y bytes de log que retienen.",
    "actividad",
)
def transacciones_abiertas(conexion: ConexionSql) -> dict:
    filas = conexion.consultar_dicts(_SQL_TRANSACCIONES)
    return {"total": len(filas), "transacciones": filas}


@herramienta(
    "detalle_sesion",
    "Detalle completo de una sesión: login, programa, esperas, transacciones, buffer de entrada y plan actual.",
    "actividad",
    session_id="ID de la sesión (spid).",
)
def detalle_sesion(conexion: ConexionSql, session_id: int) -> dict:
    sesion = conexion.consultar_dicts(
        """
        SELECT s.session_id, s.login_name, s.host_name, s.program_name, s.client_interface_name,
               s.status, s.login_time, s.last_request_start_time, s.last_request_end_time,
               s.cpu_time, s.memory_usage, s.reads, s.writes, s.logical_reads, s.row_count,
               s.transaction_isolation_level, s.open_transaction_count,
               DB_NAME(s.database_id) AS base_datos, c.client_net_address, c.net_transport,
               ib.event_info AS buffer_entrada
        FROM sys.dm_exec_sessions s
        LEFT JOIN sys.dm_exec_connections c ON c.session_id = s.session_id
        OUTER APPLY sys.dm_exec_input_buffer(s.session_id, NULL) ib
        WHERE s.session_id = ?
        """,
        (session_id,),
    )
    if not sesion:
        return {"error": f"No existe la sesión {session_id}."}
    peticion = conexion.consultar_dicts(
        """
        SELECT r.status, r.command, r.wait_type, r.wait_time, r.wait_resource, r.blocking_session_id,
               r.cpu_time, r.total_elapsed_time, r.logical_reads, r.writes, r.percent_complete,
               r.estimated_completion_time, t.text AS sql_completo,
               CAST(p.query_plan AS NVARCHAR(MAX)) AS plan_xml
        FROM sys.dm_exec_requests r
        OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
        OUTER APPLY sys.dm_exec_query_plan(r.plan_handle) p
        WHERE r.session_id = ?
        """,
        (session_id,),
    )
    esperas = conexion.consultar_dicts(
        "SELECT wait_type, waiting_task_address, wait_duration_ms, resource_description, blocking_session_id "
        "FROM sys.dm_os_waiting_tasks WHERE session_id = ?",
        (session_id,),
    )
    detalle = {"sesion": sesion[0], "peticion_actual": peticion[0] if peticion else None, "esperas": esperas}
    if peticion and peticion[0].get("plan_xml"):
        detalle["peticion_actual"]["plan_xml"] = peticion[0]["plan_xml"][:20000]
    return detalle
