"""Deadlocks recientes desde la sesión de Extended Events system_health."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta

_SQL_DEADLOCKS = """
;WITH xe AS (
    SELECT CAST(event_data AS XML) AS datos
    FROM sys.fn_xe_file_target_read_file('system_health*.xel', NULL, NULL, NULL)
    WHERE object_name = 'xml_deadlock_report'
)
SELECT TOP (?)
    datos.value('(event/@timestamp)[1]', 'datetime2') AS fecha_utc,
    CAST(datos.query('event/data/value/deadlock') AS NVARCHAR(MAX)) AS deadlock_xml
FROM xe
ORDER BY fecha_utc DESC;
"""


def _parsear_deadlock(xml_texto: str) -> dict:
    try:
        raiz = ET.fromstring(xml_texto)
    except ET.ParseError:
        return {"error": "XML no parseable", "xml": xml_texto[:4000]}
    victimas = [v.get("id") for v in raiz.iter("victimProcess")]
    procesos = []
    for p in raiz.iter("process"):
        frame = p.find(".//executionStack/frame")
        procesos.append({
            "id": p.get("id"),
            "spid": p.get("spid"),
            "victima": p.get("id") in victimas,
            "login": p.get("loginname"),
            "host": p.get("hostname"),
            "programa": p.get("clientapp"),
            "aislamiento": p.get("isolationlevel"),
            "espera_recurso": p.get("waitresource"),
            "modo_bloqueo": p.get("lockMode"),
            "transaccion": p.get("transactionname"),
            "objeto": (frame.get("procname") if frame is not None else None),
            "buffer_entrada": (p.findtext("inputbuf") or "").strip()[:1500],
        })
    recursos = []
    for lista in raiz.iter("resource-list"):
        for r in list(lista):
            recursos.append({
                "tipo": r.tag,
                "objeto": r.get("objectname"),
                "indice": r.get("indexname"),
                "modo": r.get("mode"),
                "duenos": [o.get("id") + ":" + (o.get("mode") or "") for o in r.iter("owner")],
                "en_espera": [w.get("id") + ":" + (w.get("mode") or "") for w in r.iter("waiter")],
            })
    return {"victimas": victimas, "procesos": procesos, "recursos": recursos}


@herramienta(
    "deadlocks_recientes",
    """Últimos deadlocks registrados en la sesión system_health (Extended Events), parseados:
    procesos involucrados, víctima, recursos (tabla/índice/modo), sentencias. Con esto se identifica
    el patrón (orden de acceso cruzado, lookups, falta de índice, escalado de bloqueos).""",
    "deadlocks",
    top="Cantidad de deadlocks a devolver (por defecto 5).",
    incluir_xml="Si es true incluye el XML completo de cada deadlock.",
)
def deadlocks_recientes(conexion: ConexionSql, top: int = 5, incluir_xml: bool = False) -> dict:
    filas = conexion.consultar_dicts(_SQL_DEADLOCKS, (int(top),), max_filas=None)
    resultado = []
    for f in filas:
        item = {"fecha_utc": f["fecha_utc"], **_parsear_deadlock(f["deadlock_xml"] or "")}
        if incluir_xml:
            item["xml"] = f["deadlock_xml"]
        resultado.append(item)
    total = conexion.escalar(
        "SELECT COUNT(*) FROM sys.fn_xe_file_target_read_file('system_health*.xel', NULL, NULL, NULL) WHERE object_name = 'xml_deadlock_report'"
    )
    return {"total_en_system_health": total, "deadlocks": resultado}
