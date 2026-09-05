"""Comparación de dos planes resumidos (ver `planes.resumir_plan`)."""

from __future__ import annotations

from typing import Any


def _sentencia(resumen: dict[str, Any]) -> dict[str, Any]:
    sentencias = resumen.get("sentencias") or []
    return sentencias[0] if sentencias else {}


def _firmas_operadores(s: dict[str, Any]) -> dict[str, dict[str, Any]]:
    firmas: dict[str, dict[str, Any]] = {}
    for op in s.get("operadores_costosos", []):
        clave = f"{op.get('operador')}|{op.get('objeto') or ''}"
        if clave not in firmas:
            firmas[clave] = op
    return firmas


def comparar_planes(resumen_a: dict[str, Any], resumen_b: dict[str, Any], etiqueta_a: str = "A", etiqueta_b: str = "B") -> dict[str, Any]:
    """Devuelve diferencias relevantes entre dos planes: costo, paralelismo, operadores nuevos/eliminados,
    scans/lookups, índices faltantes y advertencias."""
    a, b = _sentencia(resumen_a), _sentencia(resumen_b)
    if not a or not b:
        return {"error": "Alguno de los planes no tiene sentencias parseables."}

    ops_a, ops_b = _firmas_operadores(a), _firmas_operadores(b)
    nuevos = [ops_b[k] for k in ops_b if k not in ops_a]
    eliminados = [ops_a[k] for k in ops_a if k not in ops_b]

    def objetos(lista: list[dict[str, Any]]) -> set[str]:
        return {o.get("objeto") or "?" for o in lista}

    scans_a, scans_b = objetos(a.get("scans", [])), objetos(b.get("scans", []))
    lookups_a, lookups_b = objetos(a.get("lookups", [])), objetos(b.get("lookups", []))
    adv_a, adv_b = set(a.get("advertencias", [])), set(b.get("advertencias", []))

    costo_a, costo_b = a.get("costo_estimado") or 0, b.get("costo_estimado") or 0
    veredicto = "similares"
    if costo_a and costo_b:
        ratio = costo_b / costo_a
        if ratio > 1.3:
            veredicto = f"{etiqueta_b} es {ratio:.1f}x más costoso que {etiqueta_a}"
        elif ratio < 0.77:
            veredicto = f"{etiqueta_b} es {1 / ratio:.1f}x más barato que {etiqueta_a}"

    return {
        "veredicto": veredicto,
        "costo_estimado": {etiqueta_a: costo_a, etiqueta_b: costo_b},
        "filas_estimadas": {etiqueta_a: a.get("filas_estimadas"), etiqueta_b: b.get("filas_estimadas")},
        "grado_paralelismo": {etiqueta_a: a.get("grado_paralelismo"), etiqueta_b: b.get("grado_paralelismo")},
        "memoria_grant_kb": {etiqueta_a: a.get("memoria_grant_kb"), etiqueta_b: b.get("memoria_grant_kb")},
        "total_operadores": {etiqueta_a: a.get("total_operadores"), etiqueta_b: b.get("total_operadores")},
        "operadores_solo_en_" + etiqueta_b: nuevos,
        "operadores_solo_en_" + etiqueta_a: eliminados,
        "scans": {
            "solo_en_" + etiqueta_a: sorted(scans_a - scans_b),
            "solo_en_" + etiqueta_b: sorted(scans_b - scans_a),
            "comunes": sorted(scans_a & scans_b),
        },
        "lookups": {"solo_en_" + etiqueta_a: sorted(lookups_a - lookups_b), "solo_en_" + etiqueta_b: sorted(lookups_b - lookups_a)},
        "advertencias": {"solo_en_" + etiqueta_a: sorted(adv_a - adv_b), "solo_en_" + etiqueta_b: sorted(adv_b - adv_a), "comunes": sorted(adv_a & adv_b)},
        "indices_faltantes": {etiqueta_a: a.get("indices_faltantes", []), etiqueta_b: b.get("indices_faltantes", [])},
        "parametros": {etiqueta_a: a.get("parametros"), etiqueta_b: b.get("parametros")},
    }
