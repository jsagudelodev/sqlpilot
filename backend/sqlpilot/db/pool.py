"""Pool pequeño de conexiones por perfil, seguro para llamadas concurrentes (MCP / API)."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlpilot.config import Configuracion
from sqlpilot.db.conexion import ConexionSql

log = logging.getLogger("sqlpilot.pool")


class PoolConexiones:
    """Una cola LIFO de conexiones abiertas por perfil; crea bajo demanda hasta `maximo`.

    pyodbc no permite usar la misma conexión desde dos hilos a la vez, así que cada
    invocación toma una conexión exclusiva y la devuelve al terminar.
    """

    def __init__(self, config: Configuracion, maximo: int = 4):
        self.config = config
        self.maximo = maximo
        self._colas: dict[str, queue.LifoQueue[ConexionSql]] = {}
        self._creadas: dict[str, int] = {}
        self._lock = threading.Lock()

    def _cola(self, nombre: str) -> queue.LifoQueue[ConexionSql]:
        with self._lock:
            if nombre not in self._colas:
                self._colas[nombre] = queue.LifoQueue()
                self._creadas[nombre] = 0
            return self._colas[nombre]

    @contextmanager
    def tomar(self, perfil: str | None, espera_segundos: float = 30) -> Iterator[ConexionSql]:
        p = self.config.perfil(perfil)
        cola = self._cola(p.nombre)
        conexion: ConexionSql | None = None
        try:
            conexion = cola.get_nowait()
            try:
                conexion.escalar("SELECT 1")
            except Exception:  # conexión caída: se descarta y se crea otra
                log.info("Conexión caída en el pool '%s'; se reabre.", p.nombre)
                conexion.cerrar()
                conexion = None
        except queue.Empty:
            pass
        if conexion is None:
            with self._lock:
                puede_crear = self._creadas[p.nombre] < self.maximo
                if puede_crear:
                    self._creadas[p.nombre] += 1
            if puede_crear:
                try:
                    conexion = ConexionSql(p).abrir()
                except Exception:
                    with self._lock:
                        self._creadas[p.nombre] -= 1
                    raise
            else:
                conexion = cola.get(timeout=espera_segundos)
        try:
            yield conexion
        finally:
            cola.put(conexion)

    def cerrar_todas(self) -> None:
        for nombre, cola in self._colas.items():
            while True:
                try:
                    cola.get_nowait().cerrar()
                except queue.Empty:
                    break
            self._creadas[nombre] = 0
