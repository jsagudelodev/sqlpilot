"""Auditoría de seguridad e integridad: logins, roles, permisos peligrosos, huérfanos, CHECKDB, páginas sospechosas."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta


@herramienta(
    "auditoria_seguridad",
    """Revisión de seguridad de la instancia: miembros de sysadmin/securityadmin/serveradmin, 'sa' habilitado o
    con nombre original, logins SQL sin política/expiración de contraseña o con contraseña en blanco o igual al
    nombre, permisos CONTROL SERVER / IMPERSONATE, logins deshabilitados, guest habilitado, TRUSTWORTHY, db_owner
    excesivos en la base actual y usuarios huérfanos. Devuelve hallazgos con severidad y script de corrección.""",
    "seguridad",
)
def auditoria_seguridad(conexion: ConexionSql) -> dict:
    hallazgos: list[dict] = []

    def alerta(sev: str, titulo: str, detalle: str = "", script: str = "") -> None:
        hallazgos.append({"severidad": sev, "titulo": titulo, "detalle": detalle, "script": script})

    roles = conexion.consultar_dicts(
        """
        SELECT r.name AS rol, m.name AS login, m.type_desc AS tipo, m.is_disabled AS deshabilitado, m.create_date
        FROM sys.server_role_members rm
        JOIN sys.server_principals r ON r.principal_id = rm.role_principal_id
        JOIN sys.server_principals m ON m.principal_id = rm.member_principal_id
        WHERE r.name IN ('sysadmin', 'securityadmin', 'serveradmin', 'processadmin', 'bulkadmin')
        ORDER BY r.name, m.name
        """,
        max_filas=None,
    )
    sysadmins = [r for r in roles if r["rol"] == "sysadmin" and r["login"] not in ("sa",) and not r["login"].startswith("NT SERVICE\\")
                 and not r["login"].startswith("NT AUTHORITY\\")]
    if len(sysadmins) > 3:
        alerta("media", f"{len(sysadmins)} logins en sysadmin", ", ".join(r["login"] for r in sysadmins),
               "-- Revisar y quitar los innecesarios: ALTER SERVER ROLE sysadmin DROP MEMBER [login];")
    for r in roles:
        if r["rol"] == "sysadmin" and r["tipo"] == "WINDOWS_GROUP":
            alerta("media", f"Grupo de Windows '{r['login']}' es sysadmin", "Cualquier miembro del grupo en AD es sysadmin sin que lo veas aquí.")
        if r["rol"] == "securityadmin":
            alerta("alta", f"'{r['login']}' es securityadmin", "securityadmin puede otorgarse sysadmin a sí mismo; equivale a sysadmin.")

    sa = conexion.consultar_dicts("SELECT name, is_disabled FROM sys.server_principals WHERE sid = 0x01")
    if sa:
        if sa[0]["name"] == "sa":
            alerta("baja", "El login 'sa' conserva su nombre original", "Objetivo obvio de fuerza bruta.",
                   "ALTER LOGIN [sa] WITH NAME = [admin_renombrado];")
        if not sa[0]["is_disabled"]:
            alerta("media", f"El login '{sa[0]['name']}' (sa) está habilitado", "Si se usa Windows Auth para administrar, deshabilítalo.",
                   f"ALTER LOGIN [{sa[0]['name']}] DISABLE;")

    logins_sql = conexion.consultar_dicts(
        """
        SELECT name, is_policy_checked, is_expiration_checked, is_disabled,
               PWDCOMPARE('', password_hash) AS password_en_blanco,
               PWDCOMPARE(name, password_hash) AS password_igual_nombre,
               LOGINPROPERTY(name, 'DaysUntilExpiration') AS dias_para_expirar,
               LOGINPROPERTY(name, 'PasswordLastSetTime') AS password_desde
        FROM sys.sql_logins WHERE name NOT LIKE '##%'
        """,
        max_filas=None,
    )
    for l in logins_sql:
        if l["password_en_blanco"]:
            alerta("alta", f"Login '{l['name']}' con contraseña en blanco", script=f"ALTER LOGIN [{l['name']}] WITH PASSWORD = '<nueva>';")
        if l["password_igual_nombre"]:
            alerta("alta", f"Login '{l['name']}' con contraseña igual al nombre", script=f"ALTER LOGIN [{l['name']}] WITH PASSWORD = '<nueva>';")
        if not l["is_policy_checked"] and not l["is_disabled"]:
            alerta("baja", f"Login '{l['name']}' sin CHECK_POLICY", script=f"ALTER LOGIN [{l['name']}] WITH CHECK_POLICY = ON;")

    permisos = conexion.consultar_dicts(
        """
        SELECT pr.name AS login, pe.permission_name, pe.state_desc
        FROM sys.server_permissions pe JOIN sys.server_principals pr ON pr.principal_id = pe.grantee_principal_id
        WHERE pe.permission_name IN ('CONTROL SERVER', 'IMPERSONATE ANY LOGIN', 'ALTER ANY LOGIN', 'ALTER ANY SERVER ROLE', 'UNSAFE ASSEMBLY', 'EXTERNAL ACCESS ASSEMBLY')
          AND pe.state_desc IN ('GRANT', 'GRANT_WITH_GRANT_OPTION') AND pr.name NOT LIKE '##%'
        """,
        max_filas=None,
    )
    for p in permisos:
        alerta("alta", f"'{p['login']}' tiene {p['permission_name']}", "Permiso equivalente o cercano a sysadmin.",
               f"REVOKE {p['permission_name']} FROM [{p['login']}];")

    config = {f["name"]: f["value_in_use"] for f in conexion.consultar_dicts(
        "SELECT name, CAST(value_in_use AS INT) AS value_in_use FROM sys.configurations WHERE name IN ('xp_cmdshell','Ole Automation Procedures','Ad Hoc Distributed Queries','clr enabled','remote access','cross db ownership chaining')")}
    for nombre, sev in (("xp_cmdshell", "alta"), ("Ole Automation Procedures", "alta"), ("Ad Hoc Distributed Queries", "media"),
                        ("cross db ownership chaining", "media"), ("remote access", "baja")):
        if config.get(nombre) == 1:
            alerta(sev, f"'{nombre}' habilitado", script=f"EXEC sp_configure '{nombre}', 0; RECONFIGURE;")

    bases = conexion.consultar_dicts(
        "SELECT name, is_trustworthy_on, SUSER_SNAME(owner_sid) AS owner FROM sys.databases WHERE database_id > 4", max_filas=None)
    for b in bases:
        if b["is_trustworthy_on"]:
            alerta("alta", f"Base '{b['name']}' con TRUSTWORTHY ON", "Permite escalar de db_owner a sysadmin si el owner es sysadmin.",
                   f"ALTER DATABASE [{b['name']}] SET TRUSTWORTHY OFF;")
        if b["owner"] is None:
            alerta("media", f"Base '{b['name']}' sin owner válido (login eliminado)", script=f"ALTER AUTHORIZATION ON DATABASE::[{b['name']}] TO sa;")

    # Base de datos actual
    guest = conexion.escalar(
        "SELECT COUNT(*) FROM sys.database_permissions p JOIN sys.database_principals u ON u.principal_id = p.grantee_principal_id "
        "WHERE u.name = 'guest' AND p.permission_name = 'CONNECT' AND p.state_desc = 'GRANT'")
    if guest and conexion.perfil.base_datos.lower() not in ("master", "msdb", "tempdb"):
        alerta("media", f"Usuario guest habilitado en '{conexion.perfil.base_datos}'", script="REVOKE CONNECT FROM guest;")
    huerfanos = conexion.consultar_dicts(
        """
        SELECT dp.name AS usuario, dp.type_desc, dp.create_date
        FROM sys.database_principals dp LEFT JOIN sys.server_principals sp ON sp.sid = dp.sid
        WHERE dp.type IN ('S', 'U', 'G') AND dp.authentication_type <> 0 AND sp.sid IS NULL AND dp.name NOT IN ('dbo', 'guest', 'sys', 'INFORMATION_SCHEMA')
        """,
        max_filas=None,
    )
    for h in huerfanos:
        alerta("baja", f"Usuario huérfano '{h['usuario']}' en {conexion.perfil.base_datos}",
               script=f"-- si el login existe con otro SID: ALTER USER [{h['usuario']}] WITH LOGIN = [{h['usuario']}]; -- si no: DROP USER [{h['usuario']}];")
    db_owners = conexion.consultar_dicts(
        """
        SELECT m.name AS usuario FROM sys.database_role_members rm
        JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
        JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
        WHERE r.name = 'db_owner' AND m.name <> 'dbo'
        """,
        max_filas=None,
    )
    if len(db_owners) > 2:
        alerta("media", f"{len(db_owners)} miembros de db_owner en {conexion.perfil.base_datos}", ", ".join(d["usuario"] for d in db_owners))

    orden = {"alta": 0, "media": 1, "baja": 2}
    hallazgos.sort(key=lambda h: orden[h["severidad"]])
    return {"roles_servidor": roles, "logins_sql": logins_sql, "permisos_peligrosos": permisos, "usuarios_huerfanos": huerfanos,
            "db_owner": db_owners, "hallazgos": hallazgos}


@herramienta(
    "integridad_bases_datos",
    """Fecha del último DBCC CHECKDB exitoso por base (dbi_dbccLastKnownGood) y páginas sospechosas en
    msdb.dbo.suspect_pages (errores 823/824, checksum). Sin CHECKDB reciente, una corrupción puede pasar meses sin detectarse.""",
    "integridad",
    max_dias="Días máximos aceptables desde el último CHECKDB (por defecto 7).",
)
def integridad_bases_datos(conexion: ConexionSql, max_dias: int = 7) -> dict:
    bases = conexion.consultar_dicts(
        "SELECT name FROM sys.databases WHERE state_desc = 'ONLINE' AND name <> 'tempdb' AND HAS_DBACCESS(name) = 1", max_filas=None)
    es_azure = conexion.escalar("SELECT CAST(SERVERPROPERTY('EngineEdition') AS INT)") in (5, 8)
    resultado = []
    cursor = conexion.cursor()
    try:
        for b in bases:
            nombre = b["name"].replace("]", "]]")
            try:
                cursor.execute(f"DBCC DBINFO ([{nombre}]) WITH TABLERESULTS, NO_INFOMSGS")
                ultimo = None
                for fila in cursor.fetchall():
                    if fila[2] == "dbi_dbccLastKnownGood":  # columnas: ParentObject, Object, Field, VALUE
                        ultimo = fila[3]
                        break
                while cursor.nextset():
                    pass
                resultado.append({"base_datos": b["name"], "ultimo_checkdb": str(ultimo) if ultimo else None})
            except Exception as ex:  # sin permisos o base inaccesible
                resultado.append({"base_datos": b["name"], "error": str(ex)[:200]})
    finally:
        cursor.close()
    from datetime import datetime

    hallazgos = []
    for r in resultado:
        u = r.get("ultimo_checkdb")
        if r.get("error"):
            continue
        if not u or u.startswith("1900"):
            hallazgos.append({"severidad": "alta", "base_datos": r["base_datos"], "titulo": "Nunca se ha ejecutado DBCC CHECKDB",
                              "script": f"DBCC CHECKDB ([{r['base_datos']}]) WITH NO_INFOMSGS, ALL_ERRORMSGS;"})
        else:
            try:
                dias = (datetime.now() - datetime.fromisoformat(u.replace(" ", "T")[:19])).days  # noqa: DTZ005 — hora local del servidor
                r["dias_desde_checkdb"] = dias
                if dias > max_dias:
                    hallazgos.append({"severidad": "media" if dias < 30 else "alta", "base_datos": r["base_datos"],
                                      "titulo": f"Último CHECKDB hace {dias} días",
                                      "script": f"DBCC CHECKDB ([{r['base_datos']}]) WITH NO_INFOMSGS, ALL_ERRORMSGS;"})
            except ValueError:
                pass
    sospechosas = conexion.consultar_dicts(
        """
        SELECT DB_NAME(database_id) AS base_datos, file_id, page_id,
               CASE event_type WHEN 1 THEN '823/824 error' WHEN 2 THEN 'checksum incorrecto' WHEN 3 THEN 'página rota'
                    WHEN 4 THEN 'restaurada' WHEN 5 THEN 'reparada' WHEN 7 THEN 'desasignada' END AS evento,
               error_count, last_update_date
        FROM msdb.dbo.suspect_pages ORDER BY last_update_date DESC
        """,
        max_filas=100,
    )
    for s in sospechosas:
        if s["evento"] in ("823/824 error", "checksum incorrecto", "página rota"):
            hallazgos.append({"severidad": "alta", "base_datos": s["base_datos"], "titulo": f"Página sospechosa {s['file_id']}:{s['page_id']} ({s['evento']})",
                              "script": f"DBCC CHECKDB ([{s['base_datos']}]) WITH NO_INFOMSGS, ALL_ERRORMSGS; -- y revisar backups"})
    nota = None
    if es_azure:
        nota = ("Azure SQL (MI/DB): Microsoft ejecuta verificaciones de integridad automáticas y dbi_dbccLastKnownGood puede "
                "figurar como 1900-01-01 aunque la plataforma las haga; los hallazgos de CHECKDB aquí son orientativos. "
                "Las páginas sospechosas sí son señal real.")
        hallazgos = [h for h in hallazgos if "CHECKDB" not in h["titulo"].upper() or "sospechosa" in h["titulo"].lower()]
    return {"es_azure": es_azure, "nota": nota, "checkdb": resultado, "paginas_sospechosas": sospechosas, "hallazgos": hallazgos}


@herramienta(
    "plan_cache_salud",
    """Salud del plan cache: tamaño, planes de un solo uso (ad hoc bloat), consultas con muchos planes distintos
    (falta parametrización / sniffing por opciones SET), y % del cache desperdiciado.""",
    "consultas",
    top="Cantidad de consultas multi-plan a devolver.",
)
def plan_cache_salud(conexion: ConexionSql, top: int = 15) -> dict:
    resumen = conexion.consultar_dicts(
        """
        SELECT objtype, cacheobjtype, COUNT(*) AS planes, SUM(CAST(size_in_bytes AS BIGINT)) / 1048576 AS mb,
               SUM(CASE WHEN usecounts = 1 THEN 1 ELSE 0 END) AS un_solo_uso,
               SUM(CASE WHEN usecounts = 1 THEN CAST(size_in_bytes AS BIGINT) ELSE 0 END) / 1048576 AS mb_un_solo_uso
        FROM sys.dm_exec_cached_plans
        GROUP BY objtype, cacheobjtype ORDER BY mb DESC
        """,
        max_filas=None,
    )
    multiplan = conexion.consultar_dicts(
        """
        SELECT TOP (?) CONVERT(VARCHAR(64), qs.query_hash, 1) AS query_hash, COUNT(DISTINCT qs.query_plan_hash) AS planes_distintos,
               COUNT(*) AS entradas_cache, SUM(qs.execution_count) AS ejecuciones,
               MAX(LEFT(t.text, 300)) AS sentencia
        FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t
        GROUP BY qs.query_hash HAVING COUNT(*) > 5
        ORDER BY COUNT(*) DESC
        """,
        (int(top),),
    )
    total_mb = sum(r["mb"] or 0 for r in resumen) or 1
    un_uso_mb = sum(r["mb_un_solo_uso"] or 0 for r in resumen)
    adhoc = conexion.escalar("SELECT CAST(value_in_use AS INT) FROM sys.configurations WHERE name = 'optimize for ad hoc workloads'")
    hallazgos = []
    if un_uso_mb / total_mb > 0.3 and adhoc == 0:
        hallazgos.append({"severidad": "media", "titulo": f"{un_uso_mb} MB ({round(100 * un_uso_mb / total_mb)}%) del plan cache son planes de un solo uso",
                          "script": "EXEC sp_configure 'optimize for ad hoc workloads', 1; RECONFIGURE;"})
    if multiplan:
        hallazgos.append({"severidad": "baja", "titulo": f"{len(multiplan)} consultas con más de 5 entradas en caché (falta parametrización o distintas opciones SET)"})
    return {"total_mb": total_mb, "un_solo_uso_mb": un_uso_mb, "optimize_for_ad_hoc": adhoc, "por_tipo": resumen,
            "consultas_multiplan": multiplan, "hallazgos": hallazgos}
