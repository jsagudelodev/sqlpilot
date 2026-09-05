"""Ejecución de SQL de lectura arbitrario (con barrera de seguridad) y propuesta de scripts."""

from __future__ import annotations

from sqlpilot.db.bitacora import registrar
from sqlpilot.db.conexion import ConexionSql
from sqlpilot.db.seguridad import evaluar_sql
from sqlpilot.herramientas.base import herramienta


@herramienta(
    "ejecutar_sql_lectura",
    """Ejecuta una consulta T-SQL de SOLO LECTURA (SELECT / DMVs / catálogos) cuando ninguna otra herramienta
    cubre lo que necesitas. Solo acepta SELECT/WITH y DBCC/sp_ de consulta; rechaza todo lo demás, incluido
    EXEC de procedimientos de usuario. Máximo `max_filas` filas. Sé preciso: filtra y usa TOP.""",
    "sql",
    sql="Consulta T-SQL de lectura.",
    max_filas="Máximo de filas (por defecto 100, tope 500).",
)
def ejecutar_sql_lectura(conexion: ConexionSql, sql: str, max_filas: int = 100) -> dict:
    veredicto = evaluar_sql(sql)
    if not veredicto.permitido:
        return {"error": veredicto.motivo, "sugerencia": "Si es una acción operativa, devuélvela al DBA como script con `proponer_accion`."}
    max_filas = max(1, min(int(max_filas), 500))
    resultado = conexion.consultar(sql, max_filas=max_filas)
    registrar("sql_lectura", perfil=conexion.perfil.nombre, sql=sql[:4000], filas=resultado.total_filas, tiempo_ms=resultado.tiempo_ms)
    return {
        "columnas": resultado.columnas,
        "filas": resultado.filas,
        "total_filas": resultado.total_filas,
        "truncado": resultado.truncado,
        "tiempo_ms": resultado.tiempo_ms,
        "mensajes": resultado.mensajes,
    }


@herramienta(
    "proponer_accion",
    """Registra una acción operativa PROPUESTA para que el DBA la revise y ejecute fuera de SQLPilot (KILL,
    CREATE INDEX, UPDATE STATISTICS, sp_configure, ALTER DATABASE...). SQLPilot NO tiene forma de ejecutarla:
    solo la escribe en la bitácora y se la muestra al DBA. Incluye siempre impacto, riesgo y cómo revertirla.""",
    "operacion",
    titulo="Título corto de la acción.",
    script="Script T-SQL completo listo para ejecutar.",
    justificacion="Por qué se propone, con la evidencia encontrada.",
    riesgo="bajo | medio | alto, y qué puede salir mal.",
    reversion="Cómo deshacerla.",
)
def proponer_accion(conexion: ConexionSql, titulo: str, script: str, justificacion: str, riesgo: str = "medio",
                    reversion: str = "") -> dict:
    propuesta = {"titulo": titulo, "script": script, "justificacion": justificacion, "riesgo": riesgo, "reversion": reversion}
    registrar("propuesta", perfil=conexion.perfil.nombre, **propuesta)
    return {"registrada": True, **propuesta}
