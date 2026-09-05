"""Jobs del Agente SQL, backups y errores del log."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta


def _sin_permiso_msdb(ex: Exception) -> dict | None:
    if "permission was denied" in str(ex).lower():
        return {"error": "El login no tiene SELECT sobre las tablas de jobs en msdb.",
                "solucion": "USE msdb; CREATE USER [login] FOR LOGIN [login]; ALTER ROLE SQLAgentReaderRole ADD MEMBER [login];"}
    return None


@herramienta(
    "jobs_fallidos",
    "Ejecuciones fallidas de jobs del SQL Agent en las últimas N horas, con el mensaje de error del paso.",
    "jobs",
    horas="Ventana hacia atrás (por defecto 24).",
    top="Cantidad a devolver.",
)
def jobs_fallidos(conexion: ConexionSql, horas: int = 24, top: int = 30) -> dict:
    try:
        return _jobs_fallidos(conexion, horas, top)
    except Exception as ex:
        amigable = _sin_permiso_msdb(ex)
        if amigable is None:
            raise
        return amigable


def _jobs_fallidos(conexion: ConexionSql, horas: int, top: int) -> dict:
    filas = conexion.consultar_dicts(
        """
        ;WITH h AS (
            SELECT job_id, step_id, step_name, run_duration, message, run_status,
                   -- equivalente a msdb.dbo.agent_datetime sin requerir EXECUTE sobre la función
                   CONVERT(DATETIME, CAST(run_date AS CHAR(8)) + ' '
                       + STUFF(STUFF(RIGHT('000000' + CAST(run_time AS VARCHAR(6)), 6), 3, 0, ':'), 6, 0, ':'), 120) AS fecha
            FROM msdb.dbo.sysjobhistory WHERE run_status = 0 AND run_date > 0
        )
        SELECT TOP (?)
            j.name AS job, h.step_id, h.step_name, h.fecha,
            h.run_duration AS duracion_hhmmss,
            LEFT(h.message, 1500) AS mensaje
        FROM h JOIN msdb.dbo.sysjobs j ON j.job_id = h.job_id
        WHERE h.fecha >= DATEADD(HOUR, -?, GETDATE())
        ORDER BY h.fecha DESC
        """,
        (int(top), int(horas)),
    )
    return {"horas": horas, "fallos": filas}


@herramienta(
    "estado_jobs",
    "Jobs del SQL Agent: habilitados, última ejecución y resultado, si están corriendo ahora, próxima ejecución.",
    "jobs",
)
def estado_jobs(conexion: ConexionSql) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT j.name AS job, j.enabled AS habilitado, c.name AS categoria,
               CASE ja.run_requested_date WHEN NULL THEN 0 ELSE CASE WHEN ja.stop_execution_date IS NULL AND ja.run_requested_date IS NOT NULL THEN 1 ELSE 0 END END AS corriendo,
               ja.run_requested_date AS ultimo_inicio, ja.stop_execution_date AS ultimo_fin,
               CASE h.run_status WHEN 0 THEN 'FALLO' WHEN 1 THEN 'OK' WHEN 2 THEN 'REINTENTO' WHEN 3 THEN 'CANCELADO' WHEN 4 THEN 'EN CURSO' END AS ultimo_resultado,
               ja.next_scheduled_run_date AS proxima_ejecucion
        FROM msdb.dbo.sysjobs j
        LEFT JOIN msdb.dbo.syscategories c ON c.category_id = j.category_id
        LEFT JOIN (SELECT job_id, MAX(session_id) AS session_id FROM msdb.dbo.sysjobactivity GROUP BY job_id) ult ON ult.job_id = j.job_id
        LEFT JOIN msdb.dbo.sysjobactivity ja ON ja.job_id = j.job_id AND ja.session_id = ult.session_id
        LEFT JOIN msdb.dbo.sysjobhistory h ON h.instance_id = ja.job_history_id
        ORDER BY j.name
        """,
        max_filas=None,
    )
    return {"jobs": filas}


@herramienta(
    "estado_backups",
    """Último backup FULL, DIFF y LOG de cada base, horas desde cada uno, y bases en FULL recovery sin
    backup de log reciente (riesgo de log lleno y de pérdida de datos). Evalúa el RPO real.""",
    "backups",
)
def estado_backups(conexion: ConexionSql) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT d.name AS base_datos, d.recovery_model_desc AS recuperacion, d.state_desc AS estado,
               MAX(CASE WHEN b.type = 'D' THEN b.backup_finish_date END) AS ultimo_full,
               DATEDIFF(HOUR, MAX(CASE WHEN b.type = 'D' THEN b.backup_finish_date END), GETDATE()) AS horas_desde_full,
               MAX(CASE WHEN b.type = 'I' THEN b.backup_finish_date END) AS ultimo_diff,
               MAX(CASE WHEN b.type = 'L' THEN b.backup_finish_date END) AS ultimo_log,
               DATEDIFF(MINUTE, MAX(CASE WHEN b.type = 'L' THEN b.backup_finish_date END), GETDATE()) AS minutos_desde_log,
               MAX(CASE WHEN b.type = 'D' THEN b.backup_size END) / 1048576 AS tamano_full_mb,
               MAX(CASE WHEN b.type = 'D' THEN b.compressed_backup_size END) / 1048576 AS full_comprimido_mb,
               CASE WHEN d.recovery_model_desc = 'FULL' AND d.name <> 'master'
                         AND ISNULL(DATEDIFF(MINUTE, MAX(CASE WHEN b.type = 'L' THEN b.backup_finish_date END), GETDATE()), 99999) > 120
                    THEN 'FULL sin backup de log en > 2 h' ELSE '' END AS alerta
        FROM sys.databases d
        LEFT JOIN msdb.dbo.backupset b ON b.database_name = d.name
        WHERE d.name <> 'tempdb'
        GROUP BY d.name, d.recovery_model_desc, d.state_desc
        ORDER BY d.name
        """,
        max_filas=None,
    )
    return {"backups": filas}


@herramienta(
    "errores_log_sql",
    "Errores y advertencias recientes del ERRORLOG de SQL Server (xp_readerrorlog), filtrando ruido de backups exitosos y logins.",
    "log",
    horas="Ventana hacia atrás (por defecto 24).",
    top="Cantidad de entradas.",
)
def errores_log_sql(conexion: ConexionSql, horas: int = 24, top: int = 50) -> dict:
    cursor = conexion.cursor()
    try:
        cursor.execute("CREATE TABLE #log (fecha DATETIME, proceso NVARCHAR(100), texto NVARCHAR(MAX));")
        cursor.execute("INSERT INTO #log EXEC xp_readerrorlog 0, 1;")
    except Exception as ex:
        cursor.close()
        if "permission was denied" in str(ex).lower():
            return {"error": "El login no tiene EXECUTE sobre xp_readerrorlog (en Azure SQL Managed Instance requiere ser miembro "
                             "de ##MS_ServerStateReader## o tener permisos de sysadmin).",
                    "alternativa": "Usa deadlocks_recientes, estado_backups y jobs_fallidos; el log de errores queda fuera con este login."}
        raise
    cursor.execute(
        """
        SELECT TOP (?) fecha, proceso, LEFT(texto, 1000) AS texto FROM #log
        WHERE fecha >= DATEADD(HOUR, -?, GETDATE())
          AND (texto LIKE '%error%' OR texto LIKE '%fail%' OR texto LIKE '%warning%' OR texto LIKE '%deadlock%'
               OR texto LIKE '%I/O requests taking longer%' OR texto LIKE '%memory%' OR texto LIKE '%corrupt%'
               OR texto LIKE '%Severity: 1%' OR texto LIKE '%Severity: 2%')
          AND texto NOT LIKE 'Log was backed up%' AND texto NOT LIKE 'Database backed up%'
          AND texto NOT LIKE 'Login failed for user%' AND texto NOT LIKE '%This is an informational message%'
        ORDER BY fecha DESC
        """,
        (int(top), int(horas)),
    )
    columnas = [d[0] for d in cursor.description]
    filas = [dict(zip(columnas, [str(v) if v is not None else None for v in f])) for f in cursor.fetchall()]
    cursor.execute("DROP TABLE #log;")
    cursor.close()
    return {"horas": horas, "entradas": filas, "nota": "Los 'Login failed' se excluyen; pídelos aparte si te interesan."}
