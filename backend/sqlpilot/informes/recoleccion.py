"""Recolección de datos y consolidación de hallazgos para el informe de salud."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas import REGISTRO

# Secciones del informe: (clave, herramienta, argumentos, título)
SECCIONES: list[tuple[str, str, dict[str, Any], str]] = [
    ("configuracion_instancia", "configuracion_instancia", {}, "Configuración de la instancia"),
    ("configuracion_bases_datos", "configuracion_bases_datos", {}, "Configuración de bases de datos"),
    ("estado_backups", "estado_backups", {}, "Backups"),
    ("integridad", "integridad_bases_datos", {}, "Integridad"),
    ("espacio", "espacio_bases_datos", {}, "Espacio"),
    ("tempdb", "estado_tempdb", {}, "tempdb"),
    ("esperas", "esperas_acumuladas", {"top": 10}, "Esperas acumuladas"),
    ("presion", "presion_cpu_memoria_io", {}, "Presión de recursos"),
    ("bloqueos", "cadena_bloqueos", {}, "Bloqueos en curso"),
    ("consultas", "consultas_costosas_cache", {"top": 10}, "Consultas más costosas (plan cache)"),
    ("query_store", "consultas_costosas_query_store", {"top": 10, "horas": 24}, "Consultas más costosas (Query Store, 24 h)"),
    ("regresiones", "regresiones_query_store", {"top": 10}, "Regresiones de plan"),
    ("plan_cache", "plan_cache_salud", {"top": 10}, "Plan cache"),
    ("indices_faltantes", "indices_faltantes", {"top": 10}, "Índices faltantes"),
    ("indices_no_usados", "indices_no_usados", {"top": 10}, "Índices no usados"),
    ("indices_duplicados", "indices_duplicados", {}, "Índices duplicados"),
    ("estadisticas", "estadisticas_desactualizadas", {"top": 10}, "Estadísticas desactualizadas"),
    ("deadlocks", "deadlocks_recientes", {"top": 5}, "Deadlocks recientes"),
    ("jobs", "jobs_fallidos", {"horas": 24}, "Jobs fallidos (24 h)"),
    ("errores_log", "errores_log_sql", {"horas": 24, "top": 20}, "Errores del log (24 h)"),
    ("seguridad", "auditoria_seguridad", {}, "Seguridad"),
]

_ORDEN_SEV = {"alta": 0, "media": 1, "baja": 2, "info": 3}


def recolectar(conexion: ConexionSql, secciones: list[str] | None = None) -> dict[str, Any]:
    """Ejecuta las herramientas del informe; cada sección guarda su resultado o su error."""
    datos: dict[str, Any] = {
        "generado": datetime.now().isoformat(timespec="seconds"),  # noqa: DTZ005 — hora local del DBA
        "perfil": conexion.perfil.nombre,
        "servidor": conexion.info_servidor(),
        "secciones": {},
    }
    for clave, herramienta, args, titulo in SECCIONES:
        if secciones and clave not in secciones:
            continue
        try:
            resultado = REGISTRO[herramienta].invocar(conexion, **args)
            datos["secciones"][clave] = {"titulo": titulo, "herramienta": herramienta, "datos": resultado,
                                        "error": resultado.get("error") if isinstance(resultado, dict) else None}
        except Exception as ex:
            datos["secciones"][clave] = {"titulo": titulo, "herramienta": herramienta, "datos": None, "error": str(ex)[:500]}
    datos["hallazgos"] = consolidar_hallazgos(datos)
    return datos


def _h(sev: str, area: str, titulo: str, detalle: str = "", script: str = "", base_datos: str = "") -> dict[str, str]:
    return {"severidad": sev, "area": area, "titulo": titulo, "detalle": detalle or "", "script": script or "", "base_datos": base_datos or ""}


def consolidar_hallazgos(datos: dict[str, Any]) -> list[dict[str, str]]:
    """Unifica los hallazgos que ya traen las herramientas con los derivados de umbrales."""
    s = {k: (v.get("datos") or {}) for k, v in datos["secciones"].items() if v.get("datos") and not v.get("error")}
    hallazgos: list[dict[str, str]] = []
    es_azure = datos.get("servidor", {}).get("engine_edition") in (5, 8)  # Azure SQL Database / Managed Instance
    sistema = {"master", "model", "msdb", "tempdb"}

    # Hallazgos que las herramientas ya calculan
    for clave, area in (("configuracion_instancia", "Configuración"), ("configuracion_bases_datos", "Configuración BD"),
                        ("seguridad", "Seguridad"), ("integridad", "Integridad"), ("plan_cache", "Plan cache")):
        for h in s.get(clave, {}).get("hallazgos", []) or []:
            hallazgos.append(_h(h.get("severidad", "media"), area, h.get("titulo", ""), h.get("detalle", ""), h.get("script", ""), h.get("base_datos", "")))

    # Backups (en Azure los administra la plataforma: solo se reportan como información)
    if es_azure and s.get("estado_backups"):
        hallazgos.append(_h("info", "Backups", "Azure SQL administra los backups automáticos (FULL semanal, DIFF cada 12 h, LOG cada 5-10 min)",
                            "Los datos de msdb.dbo.backupset pueden no reflejar la política de la plataforma; revisar retención en el portal de Azure."))
    for b in s.get("estado_backups", {}).get("backups", []) or []:
        if es_azure or b.get("estado") != "ONLINE" or b["base_datos"] in sistema:
            continue
        if b.get("ultimo_full") is None:
            hallazgos.append(_h("alta", "Backups", "Sin ningún backup FULL registrado", "", f"BACKUP DATABASE [{b['base_datos']}] TO DISK = '...' WITH COMPRESSION, CHECKSUM;", b["base_datos"]))
        elif (b.get("horas_desde_full") or 0) > 48 and b.get("ultimo_diff") is None:
            hallazgos.append(_h("alta", "Backups", f"Último FULL hace {b['horas_desde_full']} h sin DIFF", "", "", b["base_datos"]))
        if b.get("alerta"):
            hallazgos.append(_h("alta", "Backups", b["alerta"], f"Último log: {b.get('ultimo_log')}", "", b["base_datos"]))

    # Espacio
    for d in s.get("espacio", {}).get("bases_datos", []) or []:
        pct = d.get("log_usado_pct") or 0
        if pct > 80:
            hallazgos.append(_h("alta", "Espacio", f"Log al {pct}% (log_reuse_wait = {d.get('log_reuse_wait')})", "", "", d["base_datos"]))
        # OLDEST_PAGE es el estado normal con checkpoint indirecto; LOG_BACKUP/CHECKPOINT son transitorios.
        if d.get("log_reuse_wait") not in (None, "NOTHING", "CHECKPOINT", "LOG_BACKUP", "OLDEST_PAGE") and d.get("estado") == "ONLINE":
            hallazgos.append(_h("media", "Espacio", f"El log no se trunca: {d['log_reuse_wait']}", "", "", d["base_datos"]))

    # Bloqueos
    hb = s.get("bloqueos", {}).get("head_blockers", []) or []
    if hb:
        ids = ", ".join(str(h.get("head_blocker")) for h in hb)
        hallazgos.append(_h("alta", "Bloqueos", f"{len(hb)} head blocker(s) activos (spids {ids})",
                            "Sesiones que bloquean a otras y no están bloqueadas; revisar si están ociosas con transacción abierta.",
                            "\n".join(f"-- KILL {h.get('head_blocker')};  -- {h.get('login_name')} @ {h.get('host_name')}" for h in hb)))

    # Presión
    p = s.get("presion", {})
    if p:
        if (p.get("runnable_total") or 0) > 0 and (p.get("pct_espera_senal") or 0) > 25:
            hallazgos.append(_h("media", "Rendimiento", f"Presión de CPU: {p['runnable_total']} tareas runnable, {p['pct_espera_senal']}% de espera de señal"))
        mem = p.get("memoria") or {}
        if (mem.get("memory_grants_pending") or 0) > 0:
            hallazgos.append(_h("alta", "Rendimiento", f"{mem['memory_grants_pending']} memory grants pendientes (presión de memoria)"))
        if mem.get("page_life_expectancy_seg") is not None and mem["page_life_expectancy_seg"] < 300:
            hallazgos.append(_h("media", "Rendimiento", f"Page Life Expectancy bajo: {mem['page_life_expectancy_seg']} s"))
        for io in (p.get("io_archivos_top") or [])[:3]:
            lr, lw = io.get("latencia_lectura_ms") or 0, io.get("latencia_escritura_ms") or 0
            if lr > 20 or lw > 20:
                hallazgos.append(_h("media", "I/O", f"Latencia alta en {io.get('archivo')}: lectura {lr} ms / escritura {lw} ms", "", "", io.get("base_datos", "")))

    # Regresiones de plan
    for r in (s.get("regresiones", {}).get("regresiones", []) or [])[:5]:
        hallazgos.append(_h("media", "Query Store", f"Regresión de plan en {r.get('objeto') or 'consulta ' + str(r.get('query_id'))}: {r.get('veces_mas_lento')}x más lento",
                            f"plan reciente {r.get('plan_reciente')} ({r.get('reciente_duracion_ms')} ms) vs mejor {r.get('plan_mejor')} ({r.get('mejor_duracion_ms')} ms)",
                            f"-- Evaluar forzar el plan bueno:\nEXEC sp_query_store_force_plan @query_id = {r.get('query_id')}, @plan_id = {r.get('plan_mejor')};"))

    # Índices
    for i in (s.get("indices_faltantes", {}).get("indices_faltantes", []) or [])[:5]:
        if (i.get("avg_user_impact") or 0) >= 70 and (i.get("user_seeks") or 0) >= 100:
            hallazgos.append(_h("media", "Índices", f"Índice faltante en {i['tabla']} (impacto {i['avg_user_impact']}%, {i['user_seeks']} seeks)",
                                f"Igualdad: {i.get('equality_columns')}; include: {i.get('included_columns')}", i.get("script", ""), ""))
    dup = s.get("indices_duplicados", {})
    if (dup.get("total") or 0) > 0:
        hallazgos.append(_h("baja", "Índices", f"{dup['total']} índices duplicados o solapados", "Cada uno cuesta escrituras y espacio sin aportar lecturas."))
    nu = s.get("indices_no_usados", {}).get("indices_no_usados", []) or []
    if nu:
        hallazgos.append(_h("baja", "Índices", f"{len(nu)}+ índices sin lecturas desde el reinicio", f"Estadísticas desde {s.get('indices_no_usados', {}).get('estadisticas_desde')}"))

    # Estadísticas
    est = s.get("estadisticas", {}).get("estadisticas", []) or []
    graves = [e for e in est if (e.get("pct_modificado") or 0) > 100]
    if graves:
        hallazgos.append(_h("media", "Estadísticas", f"{len(graves)} estadísticas con más del 100% de filas modificadas",
                            "; ".join(f"{e['tabla']} ({e['pct_modificado']}%)" for e in graves[:3]),
                            "\n".join(e.get("script", "") for e in graves[:5])))

    # Deadlocks / jobs / log
    dl = s.get("deadlocks", {})
    if (dl.get("total_en_system_health") or 0) > 0:
        hallazgos.append(_h("media", "Deadlocks", f"{dl['total_en_system_health']} deadlocks en system_health",
                            "; ".join(str(d.get("fecha_utc")) for d in (dl.get("deadlocks") or [])[:3])))
    jf = s.get("jobs", {}).get("fallos", []) or []
    if jf:
        hallazgos.append(_h("media", "Jobs", f"{len(jf)} fallos de jobs en 24 h", ", ".join(sorted({j['job'] for j in jf})[:5])))
    el = s.get("errores_log", {}).get("entradas", []) or []
    if el:
        hallazgos.append(_h("media", "Log de errores", f"{len(el)} entradas de error/advertencia en 24 h", (el[0].get("texto") or "")[:200]))

    # Secciones que no se pudieron evaluar
    for clave, sec in datos["secciones"].items():
        if sec.get("error"):
            hallazgos.append(_h("info", "Cobertura", f"No se pudo evaluar '{sec['titulo']}'", str(sec["error"])[:200]))

    if es_azure:
        # La memoria, el MAXDOP base y los parámetros de instancia los fija la plataforma.
        hallazgos = [h for h in hallazgos if not (h["area"] == "Configuración" and "max server memory" in h["titulo"])]

    hallazgos = _agrupar(hallazgos)
    hallazgos.sort(key=lambda h: (_ORDEN_SEV.get(h["severidad"], 9), h["area"]))
    return hallazgos


# prefijo del título → título del grupo cuando hay varios casos
_PREFIJOS_AGRUPABLES = {"Usuario huérfano ": "usuarios huérfanos", "Owner es ": "bases con owner distinto de sa"}


def _agrupar(hallazgos: list[dict[str, str]]) -> list[dict[str, str]]:
    """Colapsa hallazgos repetidos (mismo título en varias bases, o mismo tipo con distinto sujeto) en uno solo."""
    grupos: dict[tuple[str, str, str], dict[str, Any]] = {}
    salida: list[dict[str, str]] = []
    for h in hallazgos:
        clave_titulo = h["titulo"]
        sujeto = ""
        for prefijo in _PREFIJOS_AGRUPABLES:
            if h["titulo"].startswith(prefijo):
                clave_titulo = prefijo.strip()
                sujeto = h["titulo"][len(prefijo):]
                break
        clave = (h["severidad"], h["area"], clave_titulo)
        if clave not in grupos:
            grupos[clave] = {"base": dict(h), "bases": [], "sujetos": [], "scripts": []}
            salida.append(grupos[clave]["base"])
        g = grupos[clave]
        if h.get("base_datos"):
            g["bases"].append(h["base_datos"])
        if sujeto:
            g["sujetos"].append(sujeto)
        if h.get("script") and h["script"] not in g["scripts"]:
            g["scripts"].append(h["script"])
    for g in grupos.values():
        base = g["base"]
        n = max(len(g["bases"]), len(g["sujetos"]), 1)
        if n > 1:
            if g["sujetos"]:
                # "Usuario huérfano 'x'" ×5 → "5 usuarios huérfanos" con los sujetos en el detalle
                etiqueta = next(v for p, v in _PREFIJOS_AGRUPABLES.items() if base["titulo"].startswith(p))
                base["titulo"] = f"{n} {etiqueta}"
                base["detalle"] = "; ".join(g["sujetos"])[:600]
            else:
                base["titulo"] = f"{base['titulo']} ({n} bases)"
            if g["bases"]:
                base["base_datos"] = ", ".join(dict.fromkeys(g["bases"]))
            if len(g["scripts"]) > 1:
                base["script"] = "\n".join(g["scripts"])
    return salida
