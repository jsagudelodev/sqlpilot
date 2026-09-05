"""Configuración de la instancia y revisión contra buenas prácticas."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta


@herramienta(
    "configuracion_instancia",
    """Configuración clave de la instancia (sys.configurations, sys.dm_os_sys_info) con revisión automática
    contra buenas prácticas: MAXDOP, cost threshold for parallelism, max server memory, optimize for ad hoc,
    backup compression, remote admin, xp_cmdshell, trace flags, IFI, LPIM, versión/CU.""",
    "configuracion",
)
def configuracion_instancia(conexion: ConexionSql) -> dict:
    conf = {
        f["name"]: {"valor": f["value_in_use"], "configurado": f["value"], "dinamico": f["is_dynamic"]}
        for f in conexion.consultar_dicts(
            "SELECT name, CAST(value AS INT) AS value, CAST(value_in_use AS INT) AS value_in_use, is_dynamic FROM sys.configurations",
            max_filas=None,
        )
    }
    sistema = conexion.consultar_dicts(
        """
        SELECT cpu_count, hyperthread_ratio, cpu_count / hyperthread_ratio AS sockets,
               physical_memory_kb / 1024 AS memoria_fisica_mb,
               committed_target_kb / 1024 AS memoria_objetivo_mb,
               sqlserver_start_time, max_workers_count, scheduler_count,
               softnuma_configuration_desc, sql_memory_model_desc, virtual_machine_type_desc
        FROM sys.dm_os_sys_info
        """
    )[0]
    version = conexion.consultar_dicts(
        "SELECT @@VERSION AS version, CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(32)) AS build, "
        "CAST(SERVERPROPERTY('ProductLevel') AS NVARCHAR(32)) AS nivel, CAST(SERVERPROPERTY('ProductUpdateLevel') AS NVARCHAR(32)) AS cu, "
        "CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)) AS edicion, CAST(SERVERPROPERTY('IsClustered') AS INT) AS cluster, "
        "CAST(SERVERPROPERTY('IsHadrEnabled') AS INT) AS hadr"
    )[0]
    trace_flags = conexion.consultar_dicts("DBCC TRACESTATUS(-1) WITH NO_INFOMSGS")
    ifi = conexion.consultar_dicts(
        "SELECT instant_file_initialization_enabled AS ifi FROM sys.dm_server_services WHERE servicename LIKE 'SQL Server (%'"
    )

    hallazgos: list[dict] = []

    def alerta(sev: str, titulo: str, detalle: str, script: str = "") -> None:
        hallazgos.append({"severidad": sev, "titulo": titulo, "detalle": detalle, "script": script})

    v = lambda n: conf.get(n, {}).get("valor")

    cpus = sistema["cpu_count"]
    maxdop = v("max degree of parallelism")
    if maxdop == 0 and cpus > 8:
        alerta("media", "MAXDOP = 0 con muchos núcleos", f"La instancia tiene {cpus} CPUs lógicas. Recomendado 8 (o núcleos por NUMA).",
               "EXEC sp_configure 'max degree of parallelism', 8; RECONFIGURE;")
    ctp = v("cost threshold for parallelism")
    if ctp is not None and ctp <= 5:
        alerta("media", "Cost threshold for parallelism en valor por defecto", f"Valor actual {ctp}; el default 5 provoca paralelismo excesivo (CXPACKET). Recomendado 25-50.",
               "EXEC sp_configure 'cost threshold for parallelism', 50; RECONFIGURE;")
    max_mem = v("max server memory (MB)")
    fisica = sistema["memoria_fisica_mb"]
    if max_mem is not None and fisica and max_mem >= fisica * 0.95:
        alerta("alta", "max server memory no limitado", f"max server memory = {max_mem} MB con {fisica} MB físicos: el SO puede quedarse sin memoria. "
               "Dejar 4 GB + 1 GB por cada 8 GB adicionales al SO.",
               f"EXEC sp_configure 'max server memory (MB)', {max(1024, int(fisica * 0.85))}; RECONFIGURE;")
    if v("optimize for ad hoc workloads") == 0:
        alerta("baja", "optimize for ad hoc workloads desactivado", "Reduce el plan cache bloat en cargas con muchas consultas ad hoc.",
               "EXEC sp_configure 'optimize for ad hoc workloads', 1; RECONFIGURE;")
    if v("backup compression default") == 0:
        alerta("baja", "Compresión de backups desactivada por defecto", "Backups más pequeños y rápidos en casi todos los casos.",
               "EXEC sp_configure 'backup compression default', 1; RECONFIGURE;")
    if v("xp_cmdshell") == 1:
        alerta("alta", "xp_cmdshell habilitado", "Superficie de ataque innecesaria salvo que un proceso lo requiera explícitamente.",
               "EXEC sp_configure 'xp_cmdshell', 0; RECONFIGURE;")
    if v("remote admin connections") == 0:
        alerta("baja", "DAC remoto deshabilitado", "La Dedicated Admin Connection remota permite entrar cuando la instancia no responde.",
               "EXEC sp_configure 'remote admin connections', 1; RECONFIGURE;")
    if ifi and ifi[0].get("ifi") == "N":
        alerta("media", "Instant File Initialization deshabilitado", "El crecimiento de archivos de datos y los restores son más lentos. "
               "Otorgar 'Perform volume maintenance tasks' a la cuenta de servicio.")
    if sistema["sql_memory_model_desc"] == "CONVENTIONAL" and fisica and fisica > 32768:
        alerta("baja", "Lock Pages in Memory no habilitado", "En servidores dedicados con > 32 GB evita que el SO pagine la memoria de SQL.")
    if v("priority boost") == 1:
        alerta("alta", "priority boost habilitado", "Puede dejar sin CPU al SO; Microsoft recomienda no usarlo.",
               "EXEC sp_configure 'priority boost', 0; RECONFIGURE;")

    return {
        "version": version,
        "sistema": sistema,
        "configuracion_relevante": {k: conf[k] for k in conf if k in (
            "max degree of parallelism", "cost threshold for parallelism", "max server memory (MB)", "min server memory (MB)",
            "optimize for ad hoc workloads", "backup compression default", "xp_cmdshell", "remote admin connections",
            "priority boost", "fill factor (%)", "max worker threads", "blocked process threshold (s)", "clr enabled",
            "Ad Hoc Distributed Queries", "Database Mail XPs", "Agent XPs", "default trace enabled", "remote access")},
        "trace_flags": trace_flags,
        "ifi": ifi[0] if ifi else None,
        "hallazgos": hallazgos,
    }


@herramienta(
    "configuracion_bases_datos",
    "Opciones por base de datos que afectan rendimiento/estabilidad: auto_close, auto_shrink, compat level, page verify, stats async, Query Store, RCSI, owner.",
    "configuracion",
)
def configuracion_bases_datos(conexion: ConexionSql) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT d.name AS base_datos, SUSER_SNAME(d.owner_sid) AS owner, d.compatibility_level, d.state_desc, d.recovery_model_desc,
               d.is_auto_close_on, d.is_auto_shrink_on, d.page_verify_option_desc, d.is_auto_create_stats_on, d.is_auto_update_stats_on,
               d.is_auto_update_stats_async_on, d.is_read_committed_snapshot_on, d.snapshot_isolation_state_desc,
               d.is_query_store_on, d.is_trustworthy_on, d.is_db_chaining_on, d.delayed_durability_desc, d.target_recovery_time_in_seconds,
               d.is_encrypted, d.log_reuse_wait_desc
        FROM sys.databases d ORDER BY d.name
        """,
        max_filas=None,
    )
    hallazgos = []
    for f in filas:
        if f["is_auto_close_on"]:
            hallazgos.append({"base_datos": f["base_datos"], "severidad": "media", "titulo": "AUTO_CLOSE activado", "script": f"ALTER DATABASE [{f['base_datos']}] SET AUTO_CLOSE OFF;"})
        if f["is_auto_shrink_on"]:
            hallazgos.append({"base_datos": f["base_datos"], "severidad": "alta", "titulo": "AUTO_SHRINK activado (fragmenta y consume I/O)", "script": f"ALTER DATABASE [{f['base_datos']}] SET AUTO_SHRINK OFF;"})
        if f["page_verify_option_desc"] != "CHECKSUM":
            hallazgos.append({"base_datos": f["base_datos"], "severidad": "alta", "titulo": f"PAGE_VERIFY = {f['page_verify_option_desc']} (debe ser CHECKSUM)", "script": f"ALTER DATABASE [{f['base_datos']}] SET PAGE_VERIFY CHECKSUM;"})
        if f["owner"] != "sa" and f["base_datos"] not in ("master", "model", "msdb", "tempdb"):
            hallazgos.append({"base_datos": f["base_datos"], "severidad": "baja", "titulo": f"Owner es '{f['owner']}' (si el login se elimina, la base queda sin owner)", "script": f"ALTER AUTHORIZATION ON DATABASE::[{f['base_datos']}] TO sa;"})
        if f["is_trustworthy_on"] and f["base_datos"] != "msdb":
            hallazgos.append({"base_datos": f["base_datos"], "severidad": "alta", "titulo": "TRUSTWORTHY ON (riesgo de escalada de privilegios)", "script": f"ALTER DATABASE [{f['base_datos']}] SET TRUSTWORTHY OFF;"})
    return {"bases_datos": filas, "hallazgos": hallazgos}
