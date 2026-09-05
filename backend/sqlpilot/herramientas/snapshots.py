"""Snapshots históricos por perfil (JSON en ~/.sqlpilot/snapshots/<perfil>/) para comparar 'antes vs. ahora'."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta
from sqlpilot.herramientas.esperas import _SQL_SNAPSHOT

_SQL_QUERIES = """
SELECT TOP 300
    CONVERT(VARCHAR(64), qs.query_hash, 1) AS query_hash,
    MAX(OBJECT_NAME(t.objectid, t.dbid)) AS objeto,
    SUM(qs.execution_count) AS ejecuciones,
    SUM(qs.total_worker_time) / 1000 AS cpu_ms,
    SUM(qs.total_elapsed_time) / 1000 AS duracion_ms,
    SUM(qs.total_logical_reads) AS lecturas,
    MIN(qs.creation_time) AS plan_desde,
    MAX(LEFT(SUBSTRING(t.text, (qs.statement_start_offset / 2) + 1,
        ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(t.text) ELSE qs.statement_end_offset END - qs.statement_start_offset) / 2) + 1), 300)) AS sentencia
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t
GROUP BY qs.query_hash
ORDER BY SUM(qs.total_worker_time) DESC;
"""

_SQL_BASES = """
SELECT d.name AS base_datos,
       SUM(CASE WHEN mf.type = 0 THEN mf.size END) * 8 / 1024 AS datos_mb,
       SUM(CASE WHEN mf.type = 1 THEN mf.size END) * 8 / 1024 AS log_mb
FROM sys.databases d JOIN sys.master_files mf ON mf.database_id = d.database_id
GROUP BY d.name;
"""

_SQL_IO = """
SELECT DB_NAME(vfs.database_id) + ':' + mf.name AS archivo, vfs.num_of_reads, vfs.num_of_writes, vfs.io_stall_read_ms, vfs.io_stall_write_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL) vfs
JOIN sys.master_files mf ON mf.database_id = vfs.database_id AND mf.file_id = vfs.file_id;
"""

_SQL_GLOBAL = """
SELECT (SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE is_user_process = 1) AS sesiones_usuario,
       (SELECT COUNT(*) FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id WHERE s.is_user_process = 1) AS peticiones_activas,
       (SELECT COUNT(*) FROM sys.dm_exec_requests WHERE blocking_session_id <> 0) AS bloqueadas,
       (SELECT cntr_value FROM sys.dm_os_performance_counters WHERE object_name LIKE '%Buffer Manager%' AND counter_name = 'Page life expectancy') AS ple,
       (SELECT cntr_value FROM sys.dm_os_performance_counters WHERE object_name LIKE '%SQL Statistics%' AND counter_name = 'Batch Requests/sec') AS batch_requests_acum,
       (SELECT cntr_value FROM sys.dm_os_performance_counters WHERE object_name LIKE '%SQL Statistics%' AND counter_name = 'SQL Compilations/sec') AS compilaciones_acum,
       (SELECT cntr_value FROM sys.dm_os_performance_counters WHERE object_name LIKE '%General Statistics%' AND counter_name = 'User Connections') AS conexiones,
       (SELECT sqlserver_start_time FROM sys.dm_os_sys_info) AS inicio_sql,
       SYSUTCDATETIME() AS ahora_utc;
"""


def _directorio(perfil: str) -> Path:
    base = Path(os.environ.get("SQLPILOT_HOME", Path.home() / ".sqlpilot")) / "snapshots" / perfil
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cargar(perfil: str, nombre: str) -> dict[str, Any] | None:
    ruta = _directorio(perfil) / f"{nombre}.json"
    if not ruta.exists():
        return None
    return json.loads(ruta.read_text(encoding="utf-8"))


def _listar(perfil: str) -> list[dict[str, Any]]:
    salida = []
    for ruta in sorted(_directorio(perfil).glob("*.json")):
        try:
            d = json.loads(ruta.read_text(encoding="utf-8"))
            salida.append({"nombre": ruta.stem, "tomado_utc": d.get("tomado_utc"), "etiqueta": d.get("etiqueta"),
                           "inicio_sql": d.get("global", {}).get("inicio_sql")})
        except (OSError, json.JSONDecodeError):
            continue
    return salida


@herramienta(
    "tomar_snapshot",
    """Guarda un snapshot del estado de la instancia (esperas, top 300 consultas por query_hash, tamaños de bases,
    I/O por archivo, contadores globales) en disco local, para comparar después con `comparar_snapshots`.
    Tómalo en un momento 'normal' como línea base y otro cuando haya problemas.""",
    "historico",
    etiqueta="Texto descriptivo (ej. 'linea_base_lunes', 'incidente_cierre_mes').",
)
def tomar_snapshot(conexion: ConexionSql, etiqueta: str = "") -> dict:
    ahora = datetime.now(UTC)
    datos = {
        "tomado_utc": ahora.isoformat(),
        "etiqueta": etiqueta,
        "perfil": conexion.perfil.nombre,
        "servidor": conexion.info_servidor(),
        "global": conexion.consultar_dicts(_SQL_GLOBAL)[0],
        "esperas": conexion.consultar_dicts(_SQL_SNAPSHOT, max_filas=None),
        "consultas": conexion.consultar_dicts(_SQL_QUERIES, max_filas=None),
        "bases": conexion.consultar_dicts(_SQL_BASES, max_filas=None),
        "io": conexion.consultar_dicts(_SQL_IO, max_filas=None),
    }
    nombre = ahora.strftime("%Y%m%d_%H%M%S") + (f"_{etiqueta}" if etiqueta else "")
    nombre = "".join(c if c.isalnum() or c in "_-" else "_" for c in nombre)
    ruta = _directorio(conexion.perfil.nombre) / f"{nombre}.json"
    ruta.write_text(json.dumps(datos, ensure_ascii=False, default=str), encoding="utf-8")
    return {"snapshot": nombre, "ruta": str(ruta), "esperas": len(datos["esperas"]), "consultas": len(datos["consultas"]),
            "global": datos["global"]}


@herramienta(
    "listar_snapshots",
    "Snapshots guardados para el perfil actual, del más antiguo al más reciente.",
    "historico",
)
def listar_snapshots(conexion: ConexionSql) -> dict:
    return {"perfil": conexion.perfil.nombre, "snapshots": _listar(conexion.perfil.nombre)}


@herramienta(
    "comparar_snapshots",
    """Compara dos snapshots ('antes' vs 'ahora'): esperas por segundo, consultas cuyo CPU/lecturas/ejecuciones
    cambiaron más (por query_hash), consultas nuevas, crecimiento de bases, latencia de I/O, sesiones y
    batch requests/seg. Si omites `hasta`, toma un snapshot en vivo ahora mismo. Si omites `desde`, usa el más reciente guardado.""",
    "historico",
    desde="Nombre del snapshot inicial (ver listar_snapshots).",
    hasta="Nombre del snapshot final (opcional: en vivo).",
    top="Cantidad de consultas y esperas a mostrar.",
)
def comparar_snapshots(conexion: ConexionSql, desde: str | None = None, hasta: str | None = None, top: int = 15) -> dict:
    perfil = conexion.perfil.nombre
    if desde is None:
        lista = _listar(perfil)
        if not lista:
            return {"error": "No hay snapshots guardados. Usa tomar_snapshot primero."}
        desde = lista[-1]["nombre"]
    a = _cargar(perfil, desde)
    if a is None:
        return {"error": f"No existe el snapshot '{desde}'."}
    if hasta:
        b = _cargar(perfil, hasta)
        if b is None:
            return {"error": f"No existe el snapshot '{hasta}'."}
    else:
        b = {
            "tomado_utc": datetime.now(UTC).isoformat(), "etiqueta": "(en vivo)",
            "global": conexion.consultar_dicts(_SQL_GLOBAL)[0],
            "esperas": conexion.consultar_dicts(_SQL_SNAPSHOT, max_filas=None),
            "consultas": conexion.consultar_dicts(_SQL_QUERIES, max_filas=None),
            "bases": conexion.consultar_dicts(_SQL_BASES, max_filas=None),
            "io": conexion.consultar_dicts(_SQL_IO, max_filas=None),
        }

    reinicio = str(a["global"].get("inicio_sql")) != str(b["global"].get("inicio_sql"))
    t_a = datetime.fromisoformat(a["tomado_utc"])
    t_b = datetime.fromisoformat(b["tomado_utc"])
    segundos = max(1.0, (t_b - t_a).total_seconds())

    # Esperas
    ea = {e["wait_type"]: e for e in a["esperas"]}
    esperas = []
    for e in b["esperas"]:
        prev = ea.get(e["wait_type"], {"wait_time_ms": 0, "waiting_tasks_count": 0, "signal_wait_time_ms": 0})
        d_ms = e["wait_time_ms"] - prev["wait_time_ms"]
        if d_ms <= 0 or reinicio:
            continue
        d_t = e["waiting_tasks_count"] - prev["waiting_tasks_count"]
        esperas.append({"wait_type": e["wait_type"], "espera_ms": d_ms, "ms_por_segundo": round(d_ms / segundos, 1),
                        "tareas": d_t, "promedio_ms": round(d_ms / d_t, 2) if d_t else None})
    esperas.sort(key=lambda x: x["espera_ms"], reverse=True)
    total_espera = sum(x["espera_ms"] for x in esperas) or 1
    for x in esperas:
        x["pct"] = round(100 * x["espera_ms"] / total_espera, 1)

    # Consultas
    ca = {c["query_hash"]: c for c in a["consultas"]}
    cambios, nuevas = [], []
    for c in b["consultas"]:
        prev = ca.get(c["query_hash"])
        if prev is None or reinicio or str(prev.get("plan_desde")) != str(c.get("plan_desde")):
            nuevas.append({"query_hash": c["query_hash"], "objeto": c["objeto"], "ejecuciones": c["ejecuciones"], "cpu_ms": c["cpu_ms"],
                           "lecturas": c["lecturas"], "sentencia": c["sentencia"]})
            continue
        d_cpu = c["cpu_ms"] - prev["cpu_ms"]
        d_ej = c["ejecuciones"] - prev["ejecuciones"]
        d_lec = c["lecturas"] - prev["lecturas"]
        d_dur = c["duracion_ms"] - prev["duracion_ms"]
        if d_ej <= 0:
            continue
        cambios.append({
            "query_hash": c["query_hash"], "objeto": c["objeto"], "ejecuciones_delta": d_ej, "cpu_ms_delta": d_cpu,
            "lecturas_delta": d_lec, "duracion_ms_delta": d_dur,
            "cpu_prom_ms_antes": round(prev["cpu_ms"] / prev["ejecuciones"], 2) if prev["ejecuciones"] else None,
            "cpu_prom_ms_periodo": round(d_cpu / d_ej, 2),
            "duracion_prom_ms_periodo": round(d_dur / d_ej, 2),
            "sentencia": c["sentencia"],
        })
    cambios.sort(key=lambda x: x["cpu_ms_delta"], reverse=True)
    nuevas.sort(key=lambda x: x["cpu_ms"], reverse=True)

    # Bases
    ba = {x["base_datos"]: x for x in a["bases"]}
    crecimiento = []
    for x in b["bases"]:
        prev = ba.get(x["base_datos"])
        if prev:
            d_d, d_l = (x["datos_mb"] or 0) - (prev["datos_mb"] or 0), (x["log_mb"] or 0) - (prev["log_mb"] or 0)
            if d_d or d_l:
                crecimiento.append({"base_datos": x["base_datos"], "datos_mb_delta": d_d, "log_mb_delta": d_l})
    crecimiento.sort(key=lambda x: abs(x["datos_mb_delta"]) + abs(x["log_mb_delta"]), reverse=True)

    # I/O
    ia = {x["archivo"]: x for x in a["io"]}
    io = []
    for x in b["io"]:
        prev = ia.get(x["archivo"])
        if not prev or reinicio:
            continue
        r, w = x["num_of_reads"] - prev["num_of_reads"], x["num_of_writes"] - prev["num_of_writes"]
        sr, sw = x["io_stall_read_ms"] - prev["io_stall_read_ms"], x["io_stall_write_ms"] - prev["io_stall_write_ms"]
        if r + w <= 0:
            continue
        io.append({"archivo": x["archivo"], "lecturas": r, "escrituras": w,
                   "latencia_lectura_ms": round(sr / r, 2) if r else None, "latencia_escritura_ms": round(sw / w, 2) if w else None})
    io.sort(key=lambda x: (x["latencia_lectura_ms"] or 0) + (x["latencia_escritura_ms"] or 0), reverse=True)

    ga, gb = a["global"], b["global"]
    globales = {
        "sesiones_usuario": {"antes": ga.get("sesiones_usuario"), "ahora": gb.get("sesiones_usuario")},
        "peticiones_activas": {"antes": ga.get("peticiones_activas"), "ahora": gb.get("peticiones_activas")},
        "bloqueadas": {"antes": ga.get("bloqueadas"), "ahora": gb.get("bloqueadas")},
        "ple": {"antes": ga.get("ple"), "ahora": gb.get("ple")},
        "batch_requests_por_seg": None if reinicio else round(((gb.get("batch_requests_acum") or 0) - (ga.get("batch_requests_acum") or 0)) / segundos, 1),
        "compilaciones_por_seg": None if reinicio else round(((gb.get("compilaciones_acum") or 0) - (ga.get("compilaciones_acum") or 0)) / segundos, 2),
    }
    return {
        "desde": {"nombre": desde, "tomado_utc": a["tomado_utc"], "etiqueta": a.get("etiqueta")},
        "hasta": {"nombre": hasta or "(en vivo)", "tomado_utc": b["tomado_utc"], "etiqueta": b.get("etiqueta")},
        "segundos_entre_snapshots": round(segundos),
        "reinicio_entre_snapshots": reinicio,
        "globales": globales,
        "esperas_top": esperas[: int(top)],
        "consultas_mas_cpu_en_el_periodo": cambios[: int(top)],
        "consultas_nuevas_o_recompiladas": nuevas[: int(top)],
        "crecimiento_bases_mb": crecimiento[: int(top)],
        "io_top_latencia": io[:10],
    }
