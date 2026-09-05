from sqlpilot.analisis.planes import resumir_plan

PLAN = """<?xml version="1.0" encoding="utf-16"?>
<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan" Version="1.5" Build="15.0.2000.5">
 <BatchSequence><Batch><Statements>
  <StmtSimple StatementText="SELECT * FROM dbo.Despachos WHERE EstadoID = 3" StatementSubTreeCost="12.5" StatementEstRows="40000" StatementOptmLevel="FULL">
   <QueryPlan DegreeOfParallelism="1" MemoryGrant="1024">
    <MissingIndexes><MissingIndexGroup Impact="87.3"><MissingIndex Database="[LrpDB]" Schema="[dbo]" Table="[Despachos]">
      <ColumnGroup Usage="EQUALITY"><Column Name="[EstadoID]" ColumnId="5"/></ColumnGroup>
      <ColumnGroup Usage="INCLUDE"><Column Name="[Fecha]" ColumnId="7"/></ColumnGroup>
    </MissingIndex></MissingIndexGroup></MissingIndexes>
    <Warnings><PlanAffectingConvert ConvertIssue="Seek Plan" Expression="CONVERT_IMPLICIT(nvarchar(50),[x],0)"/></Warnings>
    <RelOp PhysicalOp="Clustered Index Scan" LogicalOp="Clustered Index Scan" EstimateRows="40000" EstimatedTotalSubtreeCost="12.5" EstimateIO="10" EstimateCPU="2.5" Parallel="0">
      <IndexScan><Object Schema="[dbo]" Table="[Despachos]" Index="[PK_Despachos]"/></IndexScan>
    </RelOp>
   </QueryPlan>
  </StmtSimple>
 </Statements></Batch></BatchSequence>
</ShowPlanXML>"""


def test_resumir_plan_extrae_lo_esencial():
    r = resumir_plan(PLAN)
    s = r["sentencias"][0]
    assert s["costo_estimado"] == 12.5
    assert s["indices_faltantes"][0]["tabla"] == "dbo.Despachos"
    assert s["indices_faltantes"][0]["impacto_pct"] == 87.3
    assert s["indices_faltantes"][0]["igualdad"] == ["EstadoID"]
    assert any("Conversión implícita" in a for a in s["advertencias"])
    assert s["scans"][0]["objeto"] == "dbo.Despachos.PK_Despachos"


def test_plan_invalido_no_revienta():
    assert "error" in resumir_plan("<no es xml")
