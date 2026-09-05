import pyodbc
import pytest

from sqlpilot.config import PerfilConexion
from sqlpilot.db.conexion import ConexionSql
from sqlpilot.db.reintentos import es_transitorio, espera_backoff, requiere_reconexion


def _error(sqlstate: str, mensaje: str) -> pyodbc.Error:
    return pyodbc.Error(sqlstate, f"[{sqlstate}] {mensaje}")


@pytest.mark.parametrize(("sqlstate", "mensaje"), [
    ("40613", "Database 'x' on server 'y' is not currently available. (40613) (SQLDriverConnect)"),
    ("HY000", "Service has encountered an error processing your request. (40197)"),
    ("08S01", "Communication link failure"),
    ("08001", "Named Pipes Provider: Could not open a connection (2)"),
    ("HY000", "The service is currently busy. (49918)"),
    ("HY000", "Resource ID: 1. The request limit has been reached. (10928)"),
    ("40001", "Transaction was deadlocked on lock resources and has been chosen as the deadlock victim. (1205)"),
])
def test_transitorios(sqlstate, mensaje):
    assert es_transitorio(_error(sqlstate, mensaje))


@pytest.mark.parametrize(("sqlstate", "mensaje"), [
    ("28000", "Login failed for user 'ro'. (18456) (SQLDriverConnect)"),
    ("28000", 'Cannot open database "Dev" requested by the login. (4060)'),
    ("42000", "The EXECUTE permission was denied on the object 'agent_datetime'. (229)"),
    ("42S22", "Invalid column name 'x'. (207)"),
    ("42000", "Use of DISTINCT is not allowed with the OVER clause. (10759)"),
    ("42000", "Incorrect syntax near 'x'. (102)"),
])
def test_permanentes(sqlstate, mensaje):
    assert not es_transitorio(_error(sqlstate, mensaje))


def test_timeout_solo_al_conectar():
    ex = _error("HYT00", "Login timeout expired")
    assert es_transitorio(ex, incluir_timeout=True)
    assert not es_transitorio(ex)  # una consulta que agotó su timeout no se reintenta


def test_requiere_reconexion():
    assert requiere_reconexion(_error("08S01", "Communication link failure"))
    assert requiere_reconexion(_error("HY000", "not currently available (40613)"))
    assert not requiere_reconexion(_error("40001", "deadlock victim (1205)"))


def test_backoff_crece_y_tiene_techo():
    assert 0.3 < espera_backoff(1, 0.5) < 0.7
    assert espera_backoff(1, 0.5) < espera_backoff(4, 0.5)
    assert espera_backoff(20, 0.5) <= 8.0 * 1.3


def _conexion(reintentos: int = 2) -> ConexionSql:
    return ConexionSql(PerfilConexion(nombre="t", servidor="x", reintentos=reintentos, espera_reintento=0.001))


def test_reintenta_transitorio_y_termina_bien():
    c = _conexion()
    llamadas = []

    def operacion() -> str:
        llamadas.append(1)
        if len(llamadas) < 3:
            raise _error("08S01", "Communication link failure")
        return "ok"

    assert c._reintentar(operacion, "prueba") == "ok"
    assert len(llamadas) == 3 and c.reintentos_usados == 2


def test_no_reintenta_permanente():
    c = _conexion()
    llamadas = []

    def operacion() -> str:
        llamadas.append(1)
        raise _error("28000", "Login failed for user 'ro'. (18456)")

    with pytest.raises(pyodbc.Error):
        c._reintentar(operacion, "prueba")
    assert len(llamadas) == 1


def test_agota_reintentos_y_propaga():
    c = _conexion(reintentos=2)
    llamadas = []

    def operacion() -> str:
        llamadas.append(1)
        raise _error("08S01", "Communication link failure")

    with pytest.raises(pyodbc.Error):
        c._reintentar(operacion, "prueba")
    assert len(llamadas) == 3  # 1 intento + 2 reintentos


def test_no_reintenta_si_fue_cancelado():
    c = _conexion()
    c._cancelado = True
    llamadas = []

    def operacion() -> str:
        llamadas.append(1)
        raise _error("HY000", "Query canceled (0)")

    with pytest.raises(pyodbc.Error):
        c._reintentar(operacion, "prueba")
    assert len(llamadas) == 1
