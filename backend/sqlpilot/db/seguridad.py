"""Barrera de seguridad: SQLPilot es SOLO LECTURA. No existe modo de escritura.

Se rechaza cualquier sentencia que no sea SELECT / WITH / DBCC de consulta / EXEC de
procedimientos de sistema de lectura. Las acciones operativas (KILL, CREATE INDEX,
UPDATE STATISTICS, sp_configure...) solo se devuelven como texto para que el DBA las
revise y ejecute él mismo, fuera de SQLPilot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Palabras que invalidan el lote completo, aparezcan donde aparezcan.
_PROHIBIDAS = (
    "xp_cmdshell", "sp_oacreate", "sp_oamethod", "openrowset", "opendatasource", "openquery",
    "bulk insert", "shutdown", "sp_configure", "reconfigure", "sp_addsrvrolemember", "sp_addrolemember",
    "sp_password", "sp_rename", "sp_recompile", "sp_updatestats", "sp_executesql", "xp_regwrite", "xp_delete_file",
    "enable_broker", "set identity_insert", "into #", "into tempdb", "waitfor delay",
)

# Solo pasan sentencias que empiezan así.
_LECTURA = re.compile(
    r"^\s*(select|with|declare\s+@\w+\s+\w+[^;]*;?\s*select|values"
    r"|dbcc\s+(show_statistics|sqlperf|opentran|inputbuffer|useroptions|tracestatus|checkident\s*\([^)]*noreseed)"
    r"|exec(ute)?\s+(sys\.)?(sp_who2?|sp_lock|sp_spaceused|sp_helpindex|sp_helpstats|sp_help\b|sp_helptext|sp_helpdb|sp_helpfile"
    r"|sp_columns|sp_tables|sp_datatype_info|sp_helpconstraint|sp_depends|sp_helprotect|sp_helpuser|sp_helplogins"
    r"|xp_readerrorlog|xp_fixeddrives|xp_msver|sp_readerrorlog)\b"
    r"|set\s+(nocount|showplan_xml|showplan_text|statistics)\b)",
    re.IGNORECASE,
)

_COMENTARIOS = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)
_STRINGS = re.compile(r"'(?:''|[^'])*'")


@dataclass
class VeredictoSql:
    permitido: bool
    motivo: str = ""


def _limpiar(sql: str) -> str:
    sin_comentarios = _COMENTARIOS.sub(" ", sql)
    return _STRINGS.sub("''", sin_comentarios)


def evaluar_sql(sql: str) -> VeredictoSql:
    """Permite únicamente SQL de lectura. Cualquier otra cosa se rechaza."""
    limpio = _limpiar(sql).lower()
    for palabra in _PROHIBIDAS:
        if palabra in limpio:
            return VeredictoSql(False, f"La sentencia contiene '{palabra}'. SQLPilot es solo lectura y nunca la ejecuta.")

    for sentencia in re.split(r";|\bGO\b", limpio, flags=re.IGNORECASE):
        if not sentencia.strip():
            continue
        if not _LECTURA.match(sentencia):
            inicio = sentencia.strip().split()[0] if sentencia.strip() else ""
            return VeredictoSql(
                False,
                f"Sentencia '{inicio.upper()}' rechazada: SQLPilot solo ejecuta lecturas (SELECT, DMVs, catálogos). "
                "Si es una acción operativa, devuélvela al DBA como script para que la ejecute él.",
            )
    return VeredictoSql(True)


def es_lectura(sql: str) -> bool:
    return evaluar_sql(sql).permitido
