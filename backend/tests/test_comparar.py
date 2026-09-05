from sqlpilot.analisis.comparar import comparar_planes
from sqlpilot.analisis.planes import resumir_plan
from tests.test_planes import PLAN

PLAN_B = PLAN.replace('StatementSubTreeCost="12.5"', 'StatementSubTreeCost="0.5"') \
             .replace('PhysicalOp="Clustered Index Scan" LogicalOp="Clustered Index Scan"', 'PhysicalOp="Index Seek" LogicalOp="Index Seek"') \
             .replace("<Warnings><PlanAffectingConvert ConvertIssue=\"Seek Plan\" Expression=\"CONVERT_IMPLICIT(nvarchar(50),[x],0)\"/></Warnings>", "") \
             .replace("<MissingIndexes>", "<MissingIndexes><!--").replace("</MissingIndexes>", "--></MissingIndexes>")


def test_comparacion_detecta_mejora():
    c = comparar_planes(resumir_plan(PLAN), resumir_plan(PLAN_B), "malo", "bueno")
    assert "más barato" in c["veredicto"]
    assert c["scans"]["solo_en_malo"] == ["dbo.Despachos.PK_Despachos"]
    assert c["operadores_solo_en_bueno"][0]["operador"] == "Index Seek"
    assert any("Conversión implícita" in a for a in c["advertencias"]["solo_en_malo"])
    assert c["indices_faltantes"]["bueno"] == []


def test_comparacion_planes_iguales():
    c = comparar_planes(resumir_plan(PLAN), resumir_plan(PLAN))
    assert c["veredicto"] == "similares"
    assert c["operadores_solo_en_A"] == [] and c["operadores_solo_en_B"] == []
