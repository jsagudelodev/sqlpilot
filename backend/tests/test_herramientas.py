from sqlpilot.herramientas import REGISTRO, listar


def test_registro_carga_herramientas():
    assert len(REGISTRO) >= 30


def test_esquemas_json_validos():
    for h in listar():
        assert h.parametros["type"] == "object"
        for nombre, prop in h.parametros["properties"].items():
            assert prop["type"] in ("string", "integer", "number", "boolean"), (h.nombre, nombre)
        assert h.descripcion


def test_parametros_requeridos_detectados():
    assert "session_id" in REGISTRO["detalle_sesion"].parametros["required"]
    assert "required" not in REGISTRO["sesiones_activas"].parametros
