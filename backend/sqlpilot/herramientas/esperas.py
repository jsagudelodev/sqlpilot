"""Estadísticas de espera (wait stats): acumuladas y delta en una ventana de tiempo."""

from __future__ import annotations

import time

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta

# Esperas benignas que se descartan (lista de Paul Randal / Glenn Berry).
_ESPERAS_IGNORADAS = (
    "'BROKER_EVENTHANDLER','BROKER_RECEIVE_WAITFOR','BROKER_TASK_STOP','BROKER_TO_FLUSH','BROKER_TRANSMITTER',"
    "'CHECKPOINT_QUEUE','CHKPT','CLR_AUTO_EVENT','CLR_MANUAL_EVENT','CLR_SEMAPHORE','DBMIRROR_DBM_EVENT',"
    "'DBMIRROR_EVENTS_QUEUE','DBMIRROR_WORKER_QUEUE','DBMIRRORING_CMD','DIRTY_PAGE_POLL','DISPATCHER_QUEUE_SEMAPHORE',"
    "'EXECSYNC','FSAGENT','FT_IFTS_SCHEDULER_IDLE_WAIT','FT_IFTSHC_MUTEX','HADR_CLUSAPI_CALL','HADR_FILESTREAM_IOMGR_IOCOMPLETION',"
    "'HADR_LOGCAPTURE_WAIT','HADR_NOTIFICATION_DEQUEUE','HADR_TIMER_TASK','HADR_WORK_QUEUE','KSOURCE_WAKEUP','LAZYWRITER_SLEEP',"
    "'LOGMGR_QUEUE','MEMORY_ALLOCATION_EXT','ONDEMAND_TASK_QUEUE','PARALLEL_REDO_DRAIN_WORKER','PARALLEL_REDO_LOG_CACHE',"
    "'PARALLEL_REDO_TRAN_LIST','PARALLEL_REDO_WORKER_SYNC','PARALLEL_REDO_WORKER_WAIT_WORK','PREEMPTIVE_OS_FLUSHFILEBUFFERS',"
    "'PREEMPTIVE_XE_GETTARGETSTATE','PVS_PREALLOCATE','PWAIT_ALL_COMPONENTS_INITIALIZED','PWAIT_DIRECTLOGCONSUMER_GETNEXT',"
    "'PWAIT_EXTENSIBILITY_CLEANUP_TASK','QDS_ASYNC_QUEUE','QDS_CLEANUP_STALE_QUERIES_TASK_MAIN_LOOP_SLEEP','QDS_PERSIST_TASK_MAIN_LOOP_SLEEP',"
    "'QDS_SHUTDOWN_QUEUE','REDO_THREAD_PENDING_WORK','REQUEST_FOR_DEADLOCK_SEARCH','RESOURCE_QUEUE','SERVER_IDLE_CHECK',"
    "'SLEEP_BPOOL_FLUSH','SLEEP_DBSTARTUP','SLEEP_DCOMSTARTUP','SLEEP_MASTERDBREADY','SLEEP_MASTERMDREADY','SLEEP_MASTERUPGRADED',"
    "'SLEEP_MSDBSTARTUP','SLEEP_SYSTEMTASK','SLEEP_TASK','SLEEP_TEMPDBSTARTUP','SNI_HTTP_ACCEPT','SOS_WORK_DISPATCHER',"
    "'SP_SERVER_DIAGNOSTICS_SLEEP','SQLTRACE_BUFFER_FLUSH','SQLTRACE_INCREMENTAL_FLUSH_SLEEP','SQLTRACE_WAIT_ENTRIES',"
    "'VDI_CLIENT_OTHER','WAIT_FOR_RESULTS','WAITFOR','WAITFOR_TASKSHUTDOWN','WAIT_XTP_RECOVERY','WAIT_XTP_HOST_WAIT',"
    "'WAIT_XTP_OFFLINE_CKPT_NEW_LOG','WAIT_XTP_CKPT_CLOSE','XE_DISPATCHER_JOIN','XE_DISPATCHER_WAIT','XE_TIMER_EVENT',"
    "'XE_LIVE_TARGET_TVF','STARTUP_DEPENDENCY_MANAGER',"
    # Benignas en Azure SQL Managed Instance / Database
    "'HADR_FABRIC_CALLBACK','PREEMPTIVE_XE_DISPATCHER','RESOURCE_GOVERNOR_IDLE','PREEMPTIVE_HADR_LEASE_MECHANISM',"
    "'HADR_AG_MUTEX','PREEMPTIVE_OS_WRITEFILEGATHER','PWAIT_HADR_CLUSTER_INTEGRATION','SQLTRACE_FILE_BUFFER',"
    "'DBMIRROR_SEND','SOS_SCHEDULER_YIELD_IDLE','XE_FILE_TARGET_TVF','PREEMPTIVE_SP_SERVER_DIAGNOSTICS'"
)

_SQL_ACUMULADAS = f"""
;WITH esperas AS (
    SELECT wait_type, wait_time_ms, signal_wait_time_ms, waiting_tasks_count,
           100.0 * wait_time_ms / NULLIF(SUM(wait_time_ms) OVER(), 0) AS pct
    FROM sys.dm_os_wait_stats
    WHERE wait_type NOT IN ({_ESPERAS_IGNORADAS})
      AND wait_type NOT LIKE 'SLEEP_%' AND wait_type NOT LIKE 'BROKER_%' AND wait_type NOT LIKE 'PREEMPTIVE_OS_%'
      AND waiting_tasks_count > 0
)
SELECT TOP (@top)
    wait_type,
    wait_time_ms / 1000.0 AS espera_seg,
    (wait_time_ms - signal_wait_time_ms) / 1000.0 AS espera_recurso_seg,
    signal_wait_time_ms / 1000.0 AS espera_senal_seg,
    waiting_tasks_count AS tareas,
    CAST(wait_time_ms * 1.0 / NULLIF(waiting_tasks_count, 0) AS DECIMAL(18,2)) AS promedio_ms,
    CAST(pct AS DECIMAL(5,2)) AS pct,
    CAST(SUM(pct) OVER (ORDER BY wait_time_ms DESC ROWS UNBOUNDED PRECEDING) AS DECIMAL(5,2)) AS pct_acumulado
FROM esperas
ORDER BY wait_time_ms DESC;
"""

_SQL_SNAPSHOT = f"""
SELECT wait_type, wait_time_ms, signal_wait_time_ms, waiting_tasks_count
FROM sys.dm_os_wait_stats
WHERE wait_type NOT IN ({_ESPERAS_IGNORADAS})
  AND wait_type NOT LIKE 'SLEEP_%' AND wait_type NOT LIKE 'BROKER_%' AND wait_type NOT LIKE 'PREEMPTIVE_OS_%';
"""


@herramienta(
    "esperas_acumuladas",
    """Top de tipos de espera acumulados desde el último reinicio o limpieza de estadísticas
    (sys.dm_os_wait_stats), excluyendo esperas benignas. Sirve para saber el perfil histórico
    de cuellos de botella: CPU (SOS_SCHEDULER_YIELD), I/O (PAGEIOLATCH_*), bloqueos (LCK_M_*),
    paralelismo (CXPACKET/CXCONSUMER), log (WRITELOG), memoria (RESOURCE_SEMAPHORE), red (ASYNC_NETWORK_IO).""",
    "esperas",
    top="Cantidad de tipos de espera a devolver (por defecto 15).",
)
def esperas_acumuladas(conexion: ConexionSql, top: int = 15) -> dict:
    filas = conexion.consultar_dicts(_SQL_ACUMULADAS.replace("@top", str(int(top))))
    desde = conexion.escalar("SELECT sqlserver_start_time FROM sys.dm_os_sys_info")
    return {"desde": desde, "esperas": filas}


@herramienta(
    "esperas_en_ventana",
    """Delta de esperas en una ventana de N segundos: qué está esperando el servidor AHORA,
    no desde el reinicio. Mucho más útil que las acumuladas para un problema en curso.
    Toma dos snapshots de sys.dm_os_wait_stats separados por `segundos`.""",
    "esperas",
    segundos="Duración de la ventana de muestreo en segundos (por defecto 10, máximo 60).",
    top="Cantidad de tipos de espera a devolver.",
)
def esperas_en_ventana(conexion: ConexionSql, segundos: int = 10, top: int = 15) -> dict:
    segundos = max(1, min(int(segundos), 60))
    antes = {f["wait_type"]: f for f in conexion.consultar_dicts(_SQL_SNAPSHOT, max_filas=None)}
    time.sleep(segundos)
    despues = conexion.consultar_dicts(_SQL_SNAPSHOT, max_filas=None)
    deltas = []
    for f in despues:
        a = antes.get(f["wait_type"], {"wait_time_ms": 0, "signal_wait_time_ms": 0, "waiting_tasks_count": 0})
        espera = f["wait_time_ms"] - a["wait_time_ms"]
        tareas = f["waiting_tasks_count"] - a["waiting_tasks_count"]
        if espera <= 0:
            continue
        senal = f["signal_wait_time_ms"] - a["signal_wait_time_ms"]
        deltas.append({
            "wait_type": f["wait_type"],
            "espera_ms": espera,
            "espera_recurso_ms": espera - senal,
            "espera_senal_ms": senal,
            "tareas": tareas,
            "promedio_ms": round(espera / tareas, 2) if tareas else None,
        })
    total = sum(d["espera_ms"] for d in deltas) or 1
    deltas.sort(key=lambda d: d["espera_ms"], reverse=True)
    for d in deltas:
        d["pct"] = round(100.0 * d["espera_ms"] / total, 2)
    return {"ventana_segundos": segundos, "total_espera_ms": total, "esperas": deltas[: int(top)]}


@herramienta(
    "presion_cpu_memoria_io",
    """Indicadores rápidos de presión de recursos: runnable tasks por scheduler (CPU), señal vs. recurso,
    Page Life Expectancy, memory grants pendientes, latencia de I/O por archivo (top 10). Un vistazo
    de 'qué recurso está saturado'.""",
    "esperas",
)
def presion_cpu_memoria_io(conexion: ConexionSql) -> dict:
    cpu = conexion.consultar_dicts(
        """
        SELECT scheduler_id, current_tasks_count, runnable_tasks_count, work_queue_count, pending_disk_io_count,
               current_workers_count, active_workers_count
        FROM sys.dm_os_schedulers WHERE status = 'VISIBLE ONLINE'
        """
    )
    senal = conexion.consultar_dicts(
        """
        SELECT CAST(100.0 * SUM(signal_wait_time_ms) / NULLIF(SUM(wait_time_ms), 0) AS DECIMAL(5,2)) AS pct_espera_senal
        FROM sys.dm_os_wait_stats
        """
    )
    memoria = conexion.consultar_dicts(
        """
        SELECT
          (SELECT cntr_value FROM sys.dm_os_performance_counters
             WHERE object_name LIKE '%Buffer Manager%' AND counter_name = 'Page life expectancy') AS page_life_expectancy_seg,
          (SELECT cntr_value FROM sys.dm_os_performance_counters
             WHERE object_name LIKE '%Memory Manager%' AND counter_name = 'Memory Grants Pending') AS memory_grants_pending,
          (SELECT cntr_value FROM sys.dm_os_performance_counters
             WHERE object_name LIKE '%Memory Manager%' AND counter_name = 'Target Server Memory (KB)') / 1024 AS target_mb,
          (SELECT cntr_value FROM sys.dm_os_performance_counters
             WHERE object_name LIKE '%Memory Manager%' AND counter_name = 'Total Server Memory (KB)') / 1024 AS total_mb,
          (SELECT physical_memory_kb / 1024 FROM sys.dm_os_sys_info) AS memoria_fisica_mb,
          (SELECT system_memory_state_desc FROM sys.dm_os_sys_memory) AS estado_memoria_so
        """
    )
    io = conexion.consultar_dicts(
        """
        SELECT TOP 10
            DB_NAME(vfs.database_id) AS base_datos, mf.name AS archivo, mf.type_desc,
            CAST(vfs.io_stall_read_ms * 1.0 / NULLIF(vfs.num_of_reads, 0) AS DECIMAL(10,2)) AS latencia_lectura_ms,
            CAST(vfs.io_stall_write_ms * 1.0 / NULLIF(vfs.num_of_writes, 0) AS DECIMAL(10,2)) AS latencia_escritura_ms,
            vfs.num_of_reads, vfs.num_of_writes, vfs.size_on_disk_bytes / 1048576 AS tamano_mb
        FROM sys.dm_io_virtual_file_stats(NULL, NULL) vfs
        JOIN sys.master_files mf ON mf.database_id = vfs.database_id AND mf.file_id = vfs.file_id
        ORDER BY (vfs.io_stall_read_ms + vfs.io_stall_write_ms) DESC
        """
    )
    return {
        "schedulers": cpu,
        "runnable_total": sum(s["runnable_tasks_count"] for s in cpu),
        "pct_espera_senal": senal[0]["pct_espera_senal"] if senal else None,
        "memoria": memoria[0] if memoria else None,
        "io_archivos_top": io,
        "guia": "runnable_tasks > 0 sostenido o señal > 20-25% = presión de CPU; PLE bajo (< 300s por cada 4 GB de buffer) "
                "o grants pendientes = presión de memoria; latencia > 20 ms datos / > 5 ms log = presión de I/O.",
    }
