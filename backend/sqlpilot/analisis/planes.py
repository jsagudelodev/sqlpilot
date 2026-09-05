"""Resumen de planes de ejecución XML de SQL Server en algo que un LLM (y un humano) pueda leer."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

_NS = {"p": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}


def _f(valor: str | None) -> float | None:
    try:
        return float(valor) if valor is not None else None
    except ValueError:
        return None


def _nombre_objeto(nodo: ET.Element) -> str | None:
    obj = nodo.find(".//p:Object", _NS)
    if obj is None:
        return None
    partes = [obj.get("Schema"), obj.get("Table"), obj.get("Index")]
    return ".".join(p.strip("[]") for p in partes if p)


def resumir_plan(plan_xml: str, max_operadores: int = 12) -> dict[str, Any]:
    """Extrae del plan: costo, operadores más costosos, scans, advertencias, índices faltantes, parámetros."""
    try:
        raiz = ET.fromstring(plan_xml)
    except ET.ParseError as ex:
        return {"error": f"Plan XML no parseable: {ex}", "plan_xml": plan_xml[:5000]}

    resumen: dict[str, Any] = {"sentencias": []}
    for stmt in raiz.iter(f"{{{_NS['p']}}}StmtSimple"):
        s: dict[str, Any] = {
            "texto": (stmt.get("StatementText") or "")[:600],
            "costo_estimado": _f(stmt.get("StatementSubTreeCost")),
            "filas_estimadas": _f(stmt.get("StatementEstRows")),
            "tipo_optimizacion": stmt.get("StatementOptmLevel"),
            "razon_terminacion": stmt.get("StatementOptmEarlyAbortReason"),
            "cardinality_estimator": stmt.get("CardinalityEstimationModelVersion"),
        }
        plan = stmt.find(".//p:QueryPlan", _NS)
        if plan is not None:
            s["grado_paralelismo"] = plan.get("DegreeOfParallelism")
            s["memoria_grant_kb"] = plan.get("MemoryGrant")
            s["tiempo_compilacion_ms"] = plan.get("CompileTime")
            s["razon_no_paralelo"] = plan.get("NonParallelPlanReason")

        # Índices faltantes
        faltantes = []
        for mig in stmt.iter(f"{{{_NS['p']}}}MissingIndexGroup"):
            for mi in mig.findall("p:MissingIndex", _NS):
                cols: dict[str, list[str]] = {"EQUALITY": [], "INEQUALITY": [], "INCLUDE": []}
                for cg in mi.findall("p:ColumnGroup", _NS):
                    cols[cg.get("Usage", "INCLUDE")] = [c.get("Name", "").strip("[]") for c in cg.findall("p:Column", _NS)]
                faltantes.append({
                    "tabla": f"{mi.get('Schema', '').strip('[]')}.{mi.get('Table', '').strip('[]')}",
                    "impacto_pct": _f(mig.get("Impact")),
                    "igualdad": cols["EQUALITY"], "desigualdad": cols["INEQUALITY"], "incluidas": cols["INCLUDE"],
                })
        s["indices_faltantes"] = faltantes

        # Advertencias
        advertencias: list[str] = []
        for w in stmt.iter(f"{{{_NS['p']}}}Warnings"):
            for hijo in w:
                tag = hijo.tag.split("}")[-1]
                detalle = {k: v for k, v in hijo.attrib.items()}
                if tag == "PlanAffectingConvert":
                    advertencias.append(f"Conversión implícita que afecta el plan: {detalle.get('Expression')} ({detalle.get('ConvertIssue')})")
                elif tag == "SpillToTempDb":
                    advertencias.append(f"Spill a tempdb (nivel {detalle.get('SpillLevel')}, {detalle.get('SpilledThreadCount')} hilos)")
                elif tag == "ColumnsWithNoStatistics":
                    advertencias.append("Columnas sin estadísticas: " + ", ".join(c.get("Column", "") for c in hijo.iter(f"{{{_NS['p']}}}ColumnReference")))
                elif tag == "NoJoinPredicate" or w.get("NoJoinPredicate") == "true":
                    advertencias.append("Join sin predicado (producto cartesiano)")
                elif tag == "UnmatchedIndexes":
                    advertencias.append("Índice filtrado no usado por parámetros (UnmatchedIndexes)")
                elif tag == "MemoryGrantWarning":
                    advertencias.append(f"Memory grant: {detalle.get('GrantWarningKind')} (solicitado {detalle.get('RequestedMemory')} KB, usado {detalle.get('MaxUsedMemory')} KB)")
                else:
                    advertencias.append(f"{tag}: {detalle}")
            if w.get("NoJoinPredicate") == "true" and "Join sin predicado (producto cartesiano)" not in advertencias:
                advertencias.append("Join sin predicado (producto cartesiano)")
        s["advertencias"] = advertencias

        # Operadores
        operadores = []
        for op in stmt.iter(f"{{{_NS['p']}}}RelOp"):
            fisico = op.get("PhysicalOp")
            logico = op.get("LogicalOp")
            info: dict[str, Any] = {
                "operador": fisico,
                "logico": logico if logico != fisico else None,
                "costo": _f(op.get("EstimatedTotalSubtreeCost")),
                "costo_io": _f(op.get("EstimateIO")),
                "costo_cpu": _f(op.get("EstimateCPU")),
                "filas_estimadas": _f(op.get("EstimateRows")),
                "ejecuciones_estimadas": _f(op.get("EstimateRebinds")) or 0 + (_f(op.get("EstimateRewinds")) or 0) + 1,
                "paralelo": op.get("Parallel") == "1",
                "objeto": _nombre_objeto(op),
            }
            rt = op.find("p:RunTimeInformation/p:RunTimeCountersPerThread", _NS)
            if rt is not None:
                reales = [ _f(r.get("ActualRows")) or 0 for r in op.findall("p:RunTimeInformation/p:RunTimeCountersPerThread", _NS)]
                info["filas_reales"] = sum(reales)
            pred = op.find(".//p:Predicate/p:ScalarOperator", _NS)
            if pred is not None and pred.get("ScalarString"):
                info["predicado"] = pred.get("ScalarString")[:300]
            seek = op.find(".//p:SeekPredicates", _NS)
            if seek is not None:
                info["seek"] = True
            if fisico in ("Table Scan", "Clustered Index Scan", "Index Scan") or (logico and "Scan" in logico):
                info["es_scan"] = True
            if fisico in ("Key Lookup", "RID Lookup") or op.get("Lookup") == "1":
                info["es_lookup"] = True
            operadores.append(info)
        operadores.sort(key=lambda o: (o["costo"] or 0), reverse=True)
        s["operadores_costosos"] = operadores[:max_operadores]
        s["scans"] = [o for o in operadores if o.get("es_scan")]
        s["lookups"] = [o for o in operadores if o.get("es_lookup")]
        s["total_operadores"] = len(operadores)

        # Parámetros (sniffing)
        params = []
        for pr in stmt.iter(f"{{{_NS['p']}}}ColumnReference"):
            if pr.get("ParameterCompiledValue") is not None or pr.get("ParameterRuntimeValue") is not None:
                params.append({"parametro": pr.get("Column"), "valor_compilado": pr.get("ParameterCompiledValue"),
                               "valor_runtime": pr.get("ParameterRuntimeValue")})
        if params:
            s["parametros"] = params

        resumen["sentencias"].append(s)

    resumen["total_sentencias"] = len(resumen["sentencias"])
    resumen["plan_xml_longitud"] = len(plan_xml)
    return resumen
