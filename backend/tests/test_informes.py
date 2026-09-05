from sqlpilot.informes.recoleccion import consolidar_hallazgos
from sqlpilot.informes.render import a_html, a_markdown


def _datos() -> dict:
    return {
        "generado": "2026-09-05T08:00:00",
        "perfil": "test",
        "servidor": {"servidor": "SRV01", "base_datos": "Demo", "version": "16.0.4", "edicion": "Standard", "login_actual": "ro"},
        "secciones": {
            "estado_backups": {"titulo": "Backups", "herramienta": "estado_backups", "error": None, "datos": {"backups": [
                {"base_datos": "Demo", "estado": "ONLINE", "recuperacion": "FULL", "ultimo_full": None, "horas_desde_full": None,
                 "ultimo_diff": None, "ultimo_log": None, "minutos_desde_log": None, "alerta": "FULL sin backup de log en > 2 h"}]}},
            "espacio": {"titulo": "Espacio", "herramienta": "espacio_bases_datos", "error": None, "datos": {"bases_datos": [
                {"base_datos": "Demo", "estado": "ONLINE", "recuperacion": "FULL", "datos_mb": 100, "log_mb": 50, "log_usado_pct": 91.5, "log_reuse_wait": "LOG_BACKUP"}]}},
            "bloqueos": {"titulo": "Bloqueos", "herramienta": "cadena_bloqueos", "error": None, "datos": {
                "head_blockers": [{"head_blocker": 57, "login_name": "app", "host_name": "web01"}], "sesiones_bloqueadas": [{}, {}]}},
            "configuracion_instancia": {"titulo": "Configuración", "herramienta": "configuracion_instancia", "error": None, "datos": {
                "hallazgos": [{"severidad": "media", "titulo": "MAXDOP = 0", "detalle": "16 CPUs", "script": "EXEC sp_configure 'max degree of parallelism', 8;"}],
                "sistema": {"cpu_count": 16, "memoria_fisica_mb": 65536, "sqlserver_start_time": "2026-09-01"}, "version": {"edicion": "Standard", "build": "16.0.4"}}},
            "jobs": {"titulo": "Jobs", "herramienta": "jobs_fallidos", "error": "permission denied", "datos": None},
        },
    }


def test_consolidar_hallazgos_prioriza_y_cubre_umbrales():
    datos = _datos()
    h = consolidar_hallazgos(datos)
    titulos = [x["titulo"] for x in h]
    assert any("Sin ningún backup FULL" in t for t in titulos)
    assert any("Log al 91.5%" in t for t in titulos)
    assert any("head blocker" in t for t in titulos)
    assert "MAXDOP = 0" in titulos
    assert any("No se pudo evaluar" in t for t in titulos)
    severidades = [x["severidad"] for x in h]
    assert severidades == sorted(severidades, key=lambda s: {"alta": 0, "media": 1, "baja": 2, "info": 3}[s])
    kill = next(x for x in h if "head blocker" in x["titulo"])
    assert "KILL 57" in kill["script"]


def test_render_markdown_y_html():
    datos = _datos()
    datos["hallazgos"] = consolidar_hallazgos(datos)
    md = a_markdown(datos, resumen_ia="Estado general: en riesgo.")
    assert md.startswith("# Informe de salud SQL Server — SRV01")
    assert "## Resumen ejecutivo" in md and "[ALTA]" in md and "```sql" in md
    assert "| base_datos |" in md  # tabla de backups
    html = a_html(datos)
    assert "<!doctype html>" in html and "Informe de salud" in html
    assert "Script propuesto" in html and "No disponible: permission denied" in html
    assert "<script" not in html  # autocontenido, sin JS
