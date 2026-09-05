"""Conexión a SQL Server vía pyodbc, con sesión endurecida para diagnóstico y
reintentos ante fallos transitorios (failover de Azure, throttling, cortes de red)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any, TypeVar

import pyodbc

from sqlpilot.config import PerfilConexion
from sqlpilot.db.reintentos import es_transitorio, espera_backoff, requiere_reconexion

log = logging.getLogger("sqlpilot.db")
_T = TypeVar("_T")

# Opciones de sesión: no bloquear al resto del servidor mientras diagnosticamos.
_SESION_SQL = """
SET NOCOUNT ON;
SET LOCK_TIMEOUT 5000;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SET ARITHABORT ON;
"""


@dataclass
class ResultadoSql:
    columnas: list[str]
    filas: list[list[Any]]
    total_filas: int
    truncado: bool
    tiempo_ms: float
    filas_afectadas: int | None = None
    mensajes: list[str] = field(default_factory=list)

    def como_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columnas, fila)) for fila in self.filas]


def _normalizar_valor(valor: Any) -> Any:
    """Convierte tipos de pyodbc a tipos serializables (JSON / LLM)."""
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, (datetime, date, dt_time)):
        return valor.isoformat(sep=" ") if isinstance(valor, datetime) else valor.isoformat()
    if isinstance(valor, (bytes, bytearray)):
        return f"0x{valor.hex()}" if len(valor) <= 32 else f"<binario {len(valor)} bytes>"
    return valor


class ConexionSql:
    """Envoltorio de pyodbc con timeouts y límite de filas. SOLO LECTURA: no existe método de escritura;
    todo SQL pasa por `consultar`, y el SQL libre además por `seguridad.evaluar_sql`."""

    def __init__(self, perfil: PerfilConexion):
        self.perfil = perfil
        self._conn: pyodbc.Connection | None = None
        self._cursor_activo: pyodbc.Cursor | None = None
        self._cancelado = False
        self.reintentos_usados = 0

    def cancelar(self) -> bool:
        """Cancela la sentencia en curso (seguro desde otro hilo). Devuelve True si había algo que cancelar."""
        cursor = self._cursor_activo
        if cursor is None:
            return False
        self._cancelado = True  # el error resultante no debe reintentarse
        try:
            cursor.cancel()
            return True
        except pyodbc.Error:
            return False

    # ----- reintentos ----------------------------------------------------
    def _reintentar(self, operacion: Callable[[], _T], descripcion: str, incluir_timeout: bool = False) -> _T:
        """Ejecuta `operacion` reintentando los fallos transitorios. Seguro porque todo es lectura."""
        intentos = max(0, self.perfil.reintentos) + 1
        for intento in range(1, intentos + 1):
            try:
                return operacion()
            except pyodbc.Error as ex:
                if self._cancelado or intento == intentos or not es_transitorio(ex, incluir_timeout):
                    raise
                espera = espera_backoff(intento, self.perfil.espera_reintento)
                log.warning("Fallo transitorio en %s (intento %d/%d): %s. Reintentando en %.1f s.",
                            descripcion, intento, intentos, str(ex)[:200], espera)
                self.reintentos_usados += 1
                if requiere_reconexion(ex):
                    self.cerrar()
                time.sleep(espera)
        raise RuntimeError("inalcanzable")  # pragma: no cover

    def cursor(self) -> pyodbc.Cursor:
        """Cursor rastreado: mientras esté abierto, `cancelar()` puede interrumpirlo."""
        cursor = self.conn.cursor()
        self._cursor_activo = cursor
        return cursor

    # ----- ciclo de vida -------------------------------------------------
    def abrir(self) -> ConexionSql:
        if self._conn is None:
            def conectar() -> pyodbc.Connection:
                conn = pyodbc.connect(self.perfil.cadena_conexion(), autocommit=True, timeout=self.perfil.timeout_conexion)
                conn.timeout = self.perfil.timeout_consulta
                conn.execute(_SESION_SQL)
                return conn

            # Al conectar sí se reintentan los timeouts: suelen ser cortes de red pasajeros.
            self._conn = self._reintentar(conectar, f"conexión a {self.perfil.servidor}", incluir_timeout=True)
        return self

    def cerrar(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> ConexionSql:
        return self.abrir()

    def __exit__(self, *_: object) -> None:
        self.cerrar()

    @property
    def conn(self) -> pyodbc.Connection:
        if self._conn is None:
            self.abrir()
        assert self._conn is not None
        return self._conn

    # ----- ejecución -----------------------------------------------------
    def consultar(
        self,
        sql: str,
        parametros: tuple | list | None = None,
        max_filas: int | None = 500,
        timeout: int | None = None,
    ) -> ResultadoSql:
        """Ejecuta SQL y devuelve el primer conjunto de resultados con columnas.
        Reintenta los fallos transitorios (es seguro: SQLPilot solo lee)."""
        return self._reintentar(lambda: self._ejecutar_consulta(sql, parametros, max_filas, timeout),
                                f"consulta ({sql.strip()[:60]}...)")

    def _ejecutar_consulta(
        self,
        sql: str,
        parametros: tuple | list | None,
        max_filas: int | None,
        timeout: int | None,
    ) -> ResultadoSql:
        inicio = time.perf_counter()
        mensajes: list[str] = []
        cursor = self.cursor()
        if timeout is not None:
            self.conn.timeout = timeout
        try:
            cursor.execute(sql, parametros or ())
            # Avanzar hasta el primer conjunto que traiga columnas (saltando PRINT/SET).
            filas_afectadas: int | None = None
            while cursor.description is None:
                if cursor.rowcount not in (-1, None):
                    filas_afectadas = cursor.rowcount
                if not cursor.nextset():
                    return ResultadoSql([], [], 0, False, _ms(inicio), filas_afectadas, mensajes)
            columnas = [d[0] for d in cursor.description]
            filas: list[list[Any]] = []
            truncado = False
            if max_filas is None:
                for fila in cursor.fetchall():
                    filas.append([_normalizar_valor(v) for v in fila])
            else:
                lote = cursor.fetchmany(max_filas + 1)
                truncado = len(lote) > max_filas
                for fila in lote[:max_filas]:
                    filas.append([_normalizar_valor(v) for v in fila])
            for m in cursor.messages or []:
                mensajes.append(str(m[1]))
            return ResultadoSql(columnas, filas, len(filas), truncado, _ms(inicio), None, mensajes)
        finally:
            self._cursor_activo = None
            cursor.close()
            if timeout is not None:
                self.conn.timeout = self.perfil.timeout_consulta

    def consultar_dicts(self, sql: str, parametros: tuple | list | None = None, max_filas: int | None = 500) -> list[dict[str, Any]]:
        return self.consultar(sql, parametros, max_filas).como_dicts()

    def escalar(self, sql: str, parametros: tuple | list | None = None) -> Any:
        r = self.consultar(sql, parametros, max_filas=1)
        return r.filas[0][0] if r.filas else None

    # ----- utilidades ----------------------------------------------------
    def info_servidor(self) -> dict[str, Any]:
        fila = self.consultar(
            """
            SELECT
                CAST(SERVERPROPERTY('MachineName') AS NVARCHAR(128)) AS maquina,
                @@SERVERNAME AS servidor,
                DB_NAME() AS base_datos,
                CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(32)) AS version,
                CAST(SERVERPROPERTY('ProductLevel') AS NVARCHAR(32)) AS nivel,
                CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)) AS edicion,
                CAST(SERVERPROPERTY('EngineEdition') AS INT) AS engine_edition,
                SUSER_SNAME() AS login_actual,
                (SELECT COUNT(*) FROM sys.databases WHERE state_desc = 'ONLINE') AS bases_online
            """,
            max_filas=1,
        ).como_dicts()
        return fila[0] if fila else {}

    def version_mayor(self) -> int:
        v = self.escalar("SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS INT)")
        return int(v or 0)


def _ms(inicio: float) -> float:
    return round((time.perf_counter() - inicio) * 1000, 2)
