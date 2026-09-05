"""Detección de fallos transitorios y política de reintentos.

SQLPilot **solo lee**, así que toda consulta es idempotente y reintentarla es seguro.
Azure SQL (Database y Managed Instance) exige esta lógica: los failover planificados,
el throttling y los límites de recursos se manifiestan como errores que se resuelven
solos en segundos. En redes con VPN o private link, además, la conexión se corta sin aviso.

Referencia de los códigos: docs de Microsoft "Troubleshooting transient connection errors".
"""

from __future__ import annotations

import random
import re

# Errores del motor que se resuelven reintentando.
NUMEROS_TRANSITORIOS = frozenset({
    40613,   # La base de datos no está disponible en este momento (failover)
    40197,   # Error del servicio procesando la petición (reconfiguración)
    40501,   # Servicio ocupado
    40143,   # Error de conexión del servicio
    40540,   # Error del servicio
    49918, 49919, 49920,  # No se puede procesar la petición / crear o actualizar (throttling)
    10928, 10929,  # Límites de recursos alcanzados
    10053, 10054, 10060, 11001,  # Red: conexión abortada, reiniciada, no se pudo establecer
    233, 64, 20, 121,  # Canal de conexión roto o semáforo expirado
    1205,    # Víctima de deadlock (en lectura: reintentar es correcto)
    1222,    # Tiempo de espera de bloqueo agotado (LOCK_TIMEOUT)
    -2,      # Timeout de ODBC
})

# Errores permanentes que jamás se reintentan (reintentar solo retrasa el mensaje al DBA).
NUMEROS_PERMANENTES = frozenset({
    18456,   # Login failed
    4060,    # No se puede abrir la base de datos solicitada por el login
    229, 230, 297, 300, 916, 262,  # Permisos denegados
    207, 208, 2812, 201,  # Objeto/columna inexistente, parámetro faltante
    102, 156, 10759,  # Sintaxis / T-SQL no soportado en este motor
})

SQLSTATES_TRANSITORIOS = frozenset({"08001", "08002", "08003", "08004", "08007", "08S01"})
SQLSTATES_TIMEOUT = frozenset({"HYT00", "HYT01"})
SQLSTATES_PERMANENTES = frozenset({"28000", "42000", "42S02", "42S22", "23000", "22001", "07002"})

_NUMERO = re.compile(r"\((-?\d{1,5})\)")


def _numeros(mensaje: str) -> set[int]:
    return {int(n) for n in _NUMERO.findall(mensaje)}


def es_transitorio(excepcion: BaseException, incluir_timeout: bool = False) -> bool:
    """True si conviene reintentar. `incluir_timeout` solo al conectar: reintentar una
    consulta que agotó su timeout haría esperar el timeout completo otra vez."""
    args = getattr(excepcion, "args", ()) or ()
    sqlstate = str(args[0]) if args else ""
    mensaje = str(excepcion)
    numeros = _numeros(mensaje)

    if numeros & NUMEROS_PERMANENTES:
        return False
    if numeros & NUMEROS_TRANSITORIOS:
        return True
    if sqlstate in SQLSTATES_TRANSITORIOS:
        return True
    if sqlstate in SQLSTATES_TIMEOUT:
        return incluir_timeout
    if sqlstate in SQLSTATES_PERMANENTES:
        return False
    # Sin código reconocible: solo los cortes de red evidentes.
    texto = mensaje.lower()
    return any(p in texto for p in ("connection is broken", "communication link failure",
                                    "existing connection was forcibly closed", "transport-level error"))


def requiere_reconexion(excepcion: BaseException) -> bool:
    """True si el error dejó la conexión inservible y hay que reabrirla."""
    args = getattr(excepcion, "args", ()) or ()
    sqlstate = str(args[0]) if args else ""
    if sqlstate.startswith("08") or sqlstate in SQLSTATES_TIMEOUT:
        return True
    return bool(_numeros(str(excepcion)) & {40613, 40197, 40143, 10053, 10054, 233, 64, 20, 121, -2})


def espera_backoff(intento: int, base: float = 0.5, techo: float = 8.0) -> float:
    """Backoff exponencial con jitter: 0.5s, 1s, 2s, 4s... ±30 %."""
    espera = min(base * (2 ** (intento - 1)), techo)
    return espera * (0.7 + random.random() * 0.6)
