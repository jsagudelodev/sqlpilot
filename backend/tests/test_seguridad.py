import pytest

from sqlpilot.db.seguridad import evaluar_sql


@pytest.mark.parametrize("sql", [
    "SELECT TOP 10 * FROM sys.dm_exec_requests",
    ";WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "DBCC SHOW_STATISTICS ('dbo.T', 'IX')",
    "DBCC TRACESTATUS(-1)",
    "EXEC sp_who2",
    "SELECT 1; SELECT 2",
    "SELECT 'delete from x' AS t -- update y",
])
def test_lecturas_permitidas(sql):
    assert evaluar_sql(sql).permitido


@pytest.mark.parametrize("sql", [
    "UPDATE dbo.Tabla SET x = 1",
    "DELETE FROM dbo.X",
    "INSERT INTO dbo.X VALUES (1)",
    "MERGE dbo.X AS t USING dbo.Y AS s ON 1=1 WHEN MATCHED THEN DELETE;",
    "KILL 55",
    "CREATE INDEX IX ON dbo.T (a)",
    "ALTER INDEX ALL ON dbo.T REBUILD",
    "DROP TABLE dbo.X",
    "TRUNCATE TABLE dbo.X",
    "UPDATE STATISTICS dbo.T",
    "EXEC sp_configure 'xp_cmdshell', 1",
    "EXEC xp_cmdshell 'dir'",
    "EXEC dbo.spCualquiera",                       # SPs de usuario no pasan: pueden escribir
    "EXEC sp_executesql N'DELETE FROM dbo.X'",
    "SELECT 1; DELETE FROM dbo.X",                 # lote con escritura al final
    "SELECT * INTO #t FROM dbo.X",
    "GRANT SELECT ON dbo.X TO alguien",
    "BEGIN TRAN",
    "ALTER DATABASE Demo SET AUTO_SHRINK OFF",
    "DBCC FREEPROCCACHE",
    "DBCC SHRINKFILE (1, 100)",
    "WAITFOR DELAY '00:01:00'",
])
def test_todo_lo_demas_se_rechaza(sql):
    v = evaluar_sql(sql)
    assert not v.permitido, sql
    assert v.motivo
