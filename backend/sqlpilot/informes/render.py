"""Renderizado del informe de salud a Markdown y HTML (autocontenido, imprimible a PDF desde el navegador)."""

from __future__ import annotations

import html
import json
from typing import Any

_ETIQUETA = {"alta": "ALTA", "media": "MEDIA", "baja": "BAJA", "info": "INFO"}
_COLOR = {"alta": "#c62828", "media": "#ef6c00", "baja": "#1565c0", "info": "#616161"}

# Columnas que se muestran por sección en las tablas (para no volcar todo)
_COLUMNAS: dict[str, tuple[str, list[str]]] = {
    "estado_backups": ("backups", ["base_datos", "recuperacion", "ultimo_full", "horas_desde_full", "ultimo_diff", "ultimo_log", "minutos_desde_log", "alerta"]),
    "espacio": ("bases_datos", ["base_datos", "estado", "recuperacion", "datos_mb", "log_mb", "log_usado_pct", "log_reuse_wait"]),
    "esperas": ("esperas", ["wait_type", "espera_seg", "promedio_ms", "pct", "pct_acumulado"]),
    "consultas": ("consultas", ["objeto", "ejecuciones", "cpu_total_ms", "cpu_prom_ms", "duracion_prom_ms", "lecturas_prom", "sentencia"]),
    "query_store": ("consultas", ["objeto", "ejecuciones", "cpu_total_ms", "cpu_prom_ms", "duracion_prom_ms", "lecturas_prom", "planes_distintos", "sentencia"]),
    "regresiones": ("regresiones", ["objeto", "veces_mas_lento", "reciente_duracion_ms", "mejor_duracion_ms", "plan_reciente", "plan_mejor", "reciente_ejecuciones"]),
    "indices_faltantes": ("indices_faltantes", ["tabla", "equality_columns", "inequality_columns", "included_columns", "user_seeks", "avg_user_impact"]),
    "indices_no_usados": ("indices_no_usados", ["tabla", "indice", "escrituras", "filas", "tamano_mb"]),
    "indices_duplicados": ("solapamientos", ["tabla", "indice", "columnas_clave", "solapado_por", "clave_del_otro", "tipo"]),
    "estadisticas": ("estadisticas", ["tabla", "estadistica", "ultima_actualizacion", "filas", "pct_modificado"]),
    "jobs": ("fallos", ["job", "step_name", "fecha", "mensaje"]),
    "errores_log": ("entradas", ["fecha", "proceso", "texto"]),
    "seguridad": ("roles_servidor", ["rol", "login", "tipo", "deshabilitado"]),
    "integridad": ("checkdb", ["base_datos", "ultimo_checkdb", "dias_desde_checkdb"]),
    "tempdb": ("sesiones_top", ["session_id", "login_name", "program_name", "asignado_mb", "liberado_mb"]),
    "bloqueos": ("head_blockers", ["head_blocker", "login_name", "host_name", "program_name", "segundos_ocioso", "transacciones_abiertas", "sesiones_bloqueadas_directas"]),
}

_MAX_FILAS = 15
_MAX_CELDA = 160


def _celda(v: Any) -> str:
    if v is None:
        return ""
    t = " ".join(str(v).split())
    return t if len(t) <= _MAX_CELDA else t[: _MAX_CELDA - 1] + "…"


def _filas_seccion(clave: str, datos: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    if clave not in _COLUMNAS or not isinstance(datos, dict):
        return [], []
    lista_clave, columnas = _COLUMNAS[clave]
    filas = datos.get(lista_clave) or []
    if not filas:
        return [], []
    cols = [c for c in columnas if any(c in f for f in filas)]
    return cols, [[_celda(f.get(c)) for c in cols] for f in filas[:_MAX_FILAS]]


def _resumen_seccion(clave: str, datos: Any) -> str:
    """Una línea de contexto por sección (lo que no cabe en tabla)."""
    if not isinstance(datos, dict):
        return ""
    if clave == "configuracion_instancia":
        sis, ver = datos.get("sistema", {}), datos.get("version", {})
        return f"{ver.get('edicion', '')} build {ver.get('build', '')} · {sis.get('cpu_count')} CPUs · {sis.get('memoria_fisica_mb')} MB · inicio {sis.get('sqlserver_start_time')}"
    if clave == "plan_cache":
        return f"{datos.get('total_mb')} MB en caché, {datos.get('un_solo_uso_mb')} MB en planes de un solo uso · optimize for ad hoc = {datos.get('optimize_for_ad_hoc')}"
    if clave == "presion":
        m = datos.get("memoria") or {}
        return f"runnable: {datos.get('runnable_total')} · señal {datos.get('pct_espera_senal')}% · PLE {m.get('page_life_expectancy_seg')} s · grants pendientes {m.get('memory_grants_pending')}"
    if clave == "tempdb":
        u = datos.get("uso") or {}
        return f"{u.get('total_mb')} MB total · usuario {u.get('objetos_usuario_mb')} · interno {u.get('objetos_internos_mb')} · version store {u.get('version_store_mb')} · libre {u.get('libre_mb')}"
    if clave == "deadlocks":
        return f"{datos.get('total_en_system_health', 0)} deadlocks registrados en system_health"
    if clave == "bloqueos":
        return f"{len(datos.get('head_blockers') or [])} head blockers · {len(datos.get('sesiones_bloqueadas') or [])} sesiones bloqueadas"
    if clave == "indices_duplicados":
        return f"{datos.get('total', 0)} solapamientos"
    if clave == "integridad" and datos.get("nota"):
        return datos["nota"]
    if clave in ("query_store", "regresiones") and datos.get("query_store") == "deshabilitado":
        return "Query Store deshabilitado en esta base de datos."
    return ""


# --------------------------------------------------------------------------- Markdown
def a_markdown(informe: dict[str, Any], resumen_ia: str | None = None) -> str:
    srv = informe["servidor"]
    hallazgos = informe["hallazgos"]
    conteo = {s: sum(1 for h in hallazgos if h["severidad"] == s) for s in ("alta", "media", "baja", "info")}
    out: list[str] = []
    out.append(f"# Informe de salud SQL Server — {srv.get('servidor')}")
    out.append("")
    out.append(f"**Base de datos:** {srv.get('base_datos')} · **Versión:** {srv.get('version')} {srv.get('edicion')} · "
               f"**Login:** {srv.get('login_actual')} · **Generado:** {informe['generado']} · **Perfil:** {informe['perfil']}")
    out.append("")
    out.append(f"**Hallazgos:** {conteo['alta']} altos · {conteo['media']} medios · {conteo['baja']} bajos · {conteo['info']} informativos")
    out.append("")
    if resumen_ia:
        out.append("## Resumen ejecutivo")
        out.append("")
        out.append(resumen_ia.strip())
        out.append("")
    out.append("## Hallazgos priorizados")
    out.append("")
    if not hallazgos:
        out.append("Sin hallazgos.")
    for i, h in enumerate(hallazgos, 1):
        bd = f" — `{h['base_datos']}`" if h.get("base_datos") else ""
        out.append(f"### {i}. [{_ETIQUETA[h['severidad']]}] {h['area']}: {h['titulo']}{bd}")
        if h.get("detalle"):
            out.append("")
            out.append(h["detalle"])
        if h.get("script"):
            out.append("")
            out.append("```sql")
            out.append(h["script"].strip())
            out.append("```")
        out.append("")
    out.append("## Detalle por sección")
    out.append("")
    for clave, sec in informe["secciones"].items():
        out.append(f"### {sec['titulo']}")
        out.append("")
        if sec.get("error"):
            out.append(f"> No disponible: {sec['error']}")
            out.append("")
            continue
        resumen = _resumen_seccion(clave, sec["datos"])
        if resumen:
            out.append(resumen)
            out.append("")
        cols, filas = _filas_seccion(clave, sec["datos"])
        if cols:
            out.append("| " + " | ".join(cols) + " |")
            out.append("|" + "---|" * len(cols))
            for f in filas:
                out.append("| " + " | ".join(c.replace("|", "\\|") for c in f) + " |")
            out.append("")
    out.append("---")
    out.append("*Generado por SQLPilot (solo lectura). Los scripts son propuestas: revísalos y ejecútalos tú.*")
    return "\n".join(out)


# --------------------------------------------------------------------------- HTML
_CSS = """
:root{--alta:#c62828;--media:#ef6c00;--baja:#1565c0;--info:#616161;--ink:#1f2933;--muted:#616e7c;--line:#e4e7eb;--bg:#fff}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
.wrap{max-width:1100px;margin:0 auto;padding:32px 40px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 12px;border-bottom:2px solid var(--line);padding-bottom:6px}
h3{font-size:15px;margin:20px 0 6px}.meta{color:var(--muted);font-size:13px}
.kpis{display:flex;gap:12px;margin:16px 0 8px;flex-wrap:wrap}.kpi{border:1px solid var(--line);border-radius:8px;padding:10px 16px;min-width:110px}
.kpi b{display:block;font-size:24px}.kpi.alta b{color:var(--alta)}.kpi.media b{color:var(--media)}.kpi.baja b{color:var(--baja)}.kpi.info b{color:var(--info)}
.badge{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px;color:#fff;vertical-align:middle;margin-right:6px}
.hallazgo{border-left:4px solid var(--line);padding:8px 14px;margin:10px 0;background:#fafbfc;border-radius:0 6px 6px 0}
.hallazgo.alta{border-color:var(--alta)}.hallazgo.media{border-color:var(--media)}.hallazgo.baja{border-color:var(--baja)}.hallazgo.info{border-color:var(--info)}
.hallazgo .t{font-weight:600}.hallazgo .d{color:var(--muted);margin:4px 0}.bd{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--muted)}
details summary{cursor:pointer;color:var(--baja);font-size:13px;margin-top:4px}pre{background:#0f172a;color:#e2e8f0;padding:10px 12px;border-radius:6px;overflow:auto;font-size:12px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:8px 0 16px}th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f5f7fa;font-weight:600}tr:nth-child(even) td{background:#fbfcfd}.resumen{background:#f5f7fa;border-radius:8px;padding:14px 18px;white-space:pre-wrap}
.nota{color:var(--muted);font-size:12px}.na{color:var(--muted);font-style:italic}
footer{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px}
@media print{.wrap{padding:0}details{display:block}details summary{display:none}details pre{display:block}h2{page-break-after:avoid}.hallazgo,table{page-break-inside:avoid}}
"""


def a_html(informe: dict[str, Any], resumen_ia: str | None = None) -> str:
    e = html.escape
    srv = informe["servidor"]
    hallazgos = informe["hallazgos"]
    conteo = {s: sum(1 for h in hallazgos if h["severidad"] == s) for s in ("alta", "media", "baja", "info")}
    p: list[str] = []
    p.append(f"<!doctype html><html lang='es'><head><meta charset='utf-8'><title>Informe de salud — {e(str(srv.get('servidor')))}</title><style>{_CSS}</style></head><body><div class='wrap'>")
    p.append(f"<h1>Informe de salud SQL Server — {e(str(srv.get('servidor')))}</h1>")
    p.append(f"<div class='meta'>Base de datos <b>{e(str(srv.get('base_datos')))}</b> · Versión {e(str(srv.get('version')))} {e(str(srv.get('edicion') or ''))} · "
             f"Login {e(str(srv.get('login_actual')))} · Generado {e(informe['generado'])} · Perfil {e(informe['perfil'])}</div>")
    p.append("<div class='kpis'>" + "".join(
        f"<div class='kpi {s}'><b>{conteo[s]}</b>{_ETIQUETA[s].capitalize()}</div>" for s in ("alta", "media", "baja", "info")) + "</div>")
    if resumen_ia:
        p.append("<h2>Resumen ejecutivo</h2>")
        p.append(f"<div class='resumen'>{e(resumen_ia.strip())}</div>")
    p.append("<h2>Hallazgos priorizados</h2>")
    if not hallazgos:
        p.append("<p class='na'>Sin hallazgos.</p>")
    for i, h in enumerate(hallazgos, 1):
        sev = h["severidad"]
        p.append(f"<div class='hallazgo {sev}'><div class='t'><span class='badge' style='background:{_COLOR[sev]}'>{_ETIQUETA[sev]}</span>"
                 f"{i}. {e(h['area'])}: {e(h['titulo'])}" + (f" <span class='bd'>{e(h['base_datos'])}</span>" if h.get("base_datos") else "") + "</div>")
        if h.get("detalle"):
            p.append(f"<div class='d'>{e(h['detalle'])}</div>")
        if h.get("script"):
            p.append(f"<details><summary>Script propuesto</summary><pre>{e(h['script'].strip())}</pre></details>")
        p.append("</div>")
    p.append("<h2>Detalle por sección</h2>")
    for clave, sec in informe["secciones"].items():
        p.append(f"<h3>{e(sec['titulo'])}</h3>")
        if sec.get("error"):
            p.append(f"<p class='na'>No disponible: {e(str(sec['error']))}</p>")
            continue
        resumen = _resumen_seccion(clave, sec["datos"])
        if resumen:
            p.append(f"<p class='nota'>{e(resumen)}</p>")
        cols, filas = _filas_seccion(clave, sec["datos"])
        if cols:
            p.append("<table><thead><tr>" + "".join(f"<th>{e(c)}</th>" for c in cols) + "</tr></thead><tbody>")
            for f in filas:
                p.append("<tr>" + "".join(f"<td>{e(c)}</td>" for c in f) + "</tr>")
            p.append("</tbody></table>")
        elif not resumen:
            p.append("<p class='na'>Sin datos relevantes.</p>")
    p.append("<footer>Generado por SQLPilot (solo lectura). Los scripts son propuestas: revísalos y ejecútalos tú. "
             "Para PDF: imprimir esta página → Guardar como PDF.</footer>")
    p.append("</div></body></html>")
    return "".join(p)


def a_json(informe: dict[str, Any]) -> str:
    return json.dumps(informe, ensure_ascii=False, indent=2, default=str)
