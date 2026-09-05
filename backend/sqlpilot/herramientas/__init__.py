"""Catálogo de herramientas de diagnóstico. Importar este paquete registra todas."""

from sqlpilot.herramientas import (  # noqa: F401  (registro por efecto secundario)
    actividad,
    configuracion,
    consultas_costosas,
    deadlocks,
    espacio,
    esperas,
    esquema,
    estadisticas,
    indices,
    informe,
    jobs_backups,
    planes_qs,
    seguridad_instancia,
    snapshots,
    sql_libre,
)
from sqlpilot.herramientas.base import REGISTRO, Herramienta, listar, obtener

__all__ = ["REGISTRO", "Herramienta", "listar", "obtener"]
