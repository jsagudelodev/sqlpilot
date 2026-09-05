/* =====================================================================
   SQLPilot — Escenario de prueba
   Crea la base SQLPilotDemo con ~300k pedidos y dos SPs:
     - dbo.spPedidos_Buscar_Malo : lleno de problemas clásicos para diagnosticar
     - dbo.spPedidos_Buscar_Bueno: la versión corregida, para comparar planes
   Ejecutar completo en SSMS (tarda ~30-60 s por la carga de datos).
   ===================================================================== */
SET NOCOUNT ON;
GO
IF DB_ID('SQLPilotDemo') IS NULL
    CREATE DATABASE SQLPilotDemo;
GO
ALTER DATABASE SQLPilotDemo SET QUERY_STORE = ON (OPERATION_MODE = READ_WRITE, QUERY_CAPTURE_MODE = ALL);
GO
USE SQLPilotDemo;
GO
-- ---------------------------------------------------------------- tablas
IF OBJECT_ID('dbo.PedidoDetalle') IS NOT NULL DROP TABLE dbo.PedidoDetalle;
IF OBJECT_ID('dbo.Pedidos')       IS NOT NULL DROP TABLE dbo.Pedidos;
IF OBJECT_ID('dbo.Clientes')      IS NOT NULL DROP TABLE dbo.Clientes;
IF OBJECT_ID('dbo.Productos')     IS NOT NULL DROP TABLE dbo.Productos;
GO
CREATE TABLE dbo.Clientes (
    ClienteID   INT IDENTITY(1,1) PRIMARY KEY,
    Nit         VARCHAR(20)   NOT NULL,          -- VARCHAR a propósito (conversión implícita)
    Nombre      NVARCHAR(120) NOT NULL,
    Ciudad      NVARCHAR(60)  NOT NULL,
    Activo      BIT           NOT NULL DEFAULT 1
);
CREATE TABLE dbo.Productos (
    ProductoID  INT IDENTITY(1,1) PRIMARY KEY,
    Codigo      VARCHAR(30)   NOT NULL,
    Descripcion NVARCHAR(200) NOT NULL,
    Precio      DECIMAL(18,2) NOT NULL
);
CREATE TABLE dbo.Pedidos (
    PedidoID    INT IDENTITY(1,1) PRIMARY KEY,
    ClienteID   INT           NOT NULL,          -- sin índice a propósito
    Fecha       DATETIME      NOT NULL,
    EstadoID    TINYINT       NOT NULL,          -- 1 nuevo, 2 aprobado, 3 despachado, 4 anulado
    Total       DECIMAL(18,2) NOT NULL,
    Observacion NVARCHAR(500) NULL,
    CONSTRAINT FK_Pedidos_Clientes FOREIGN KEY (ClienteID) REFERENCES dbo.Clientes(ClienteID)
);
CREATE TABLE dbo.PedidoDetalle (
    PedidoDetalleID INT IDENTITY(1,1) PRIMARY KEY,
    PedidoID    INT           NOT NULL,
    ProductoID  INT           NOT NULL,
    Cantidad    INT           NOT NULL,
    PrecioUnit  DECIMAL(18,2) NOT NULL,
    CONSTRAINT FK_Detalle_Pedidos   FOREIGN KEY (PedidoID)   REFERENCES dbo.Pedidos(PedidoID),
    CONSTRAINT FK_Detalle_Productos FOREIGN KEY (ProductoID) REFERENCES dbo.Productos(ProductoID)
);
GO
-- ---------------------------------------------------------------- datos
;WITH n AS (SELECT TOP (5000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i FROM sys.all_columns a CROSS JOIN sys.all_columns b)
INSERT INTO dbo.Clientes (Nit, Nombre, Ciudad, Activo)
SELECT RIGHT('800000000' + CAST(i AS VARCHAR), 9) + '-' + CAST(i % 10 AS VARCHAR),
       N'Cliente ' + CAST(i AS NVARCHAR),
       CHOOSE(i % 6 + 1, N'Bogotá', N'Medellín', N'Cali', N'Barranquilla', N'Cartagena', N'Bucaramanga'),
       CASE WHEN i % 20 = 0 THEN 0 ELSE 1 END
FROM n;

;WITH n AS (SELECT TOP (800) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i FROM sys.all_columns)
INSERT INTO dbo.Productos (Codigo, Descripcion, Precio)
SELECT 'PRD-' + RIGHT('00000' + CAST(i AS VARCHAR), 5), N'Producto ' + CAST(i AS NVARCHAR), (i % 97 + 1) * 1250.0
FROM n;

;WITH n AS (SELECT TOP (300000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i FROM sys.all_columns a CROSS JOIN sys.all_columns b CROSS JOIN sys.all_columns c)
INSERT INTO dbo.Pedidos (ClienteID, Fecha, EstadoID, Total, Observacion)
SELECT ABS(CHECKSUM(NEWID())) % 5000 + 1,
       DATEADD(MINUTE, -(ABS(CHECKSUM(NEWID())) % (60 * 24 * 730)), GETDATE()),   -- últimos 2 años
       CASE WHEN i % 100 < 3 THEN 1 WHEN i % 100 < 10 THEN 2 WHEN i % 100 < 97 THEN 3 ELSE 4 END,  -- sesgo: 87% despachados, 3% nuevos
       0,
       CASE WHEN i % 7 = 0 THEN N'Entregar en portería' ELSE NULL END
FROM n;

INSERT INTO dbo.PedidoDetalle (PedidoID, ProductoID, Cantidad, PrecioUnit)
SELECT p.PedidoID, ABS(CHECKSUM(NEWID())) % 800 + 1, ABS(CHECKSUM(NEWID())) % 10 + 1, (ABS(CHECKSUM(NEWID())) % 97 + 1) * 1250.0
FROM dbo.Pedidos p CROSS JOIN (VALUES (1), (2), (3)) v(x)
WHERE p.PedidoID % 3 <> v.x % 3 OR v.x = 1;

UPDATE p SET Total = d.Suma
FROM dbo.Pedidos p
JOIN (SELECT PedidoID, SUM(Cantidad * PrecioUnit) AS Suma FROM dbo.PedidoDetalle GROUP BY PedidoID) d ON d.PedidoID = p.PedidoID;
GO
-- Índice inútil (nunca se usará) y estadísticas viejas a propósito
CREATE NONCLUSTERED INDEX IX_Pedidos_Observacion ON dbo.Pedidos (Observacion);
CREATE NONCLUSTERED INDEX IX_Pedidos_Fecha ON dbo.Pedidos (Fecha);
CREATE NONCLUSTERED INDEX IX_Pedidos_Fecha_Dup ON dbo.Pedidos (Fecha, EstadoID);   -- solapa con el anterior
GO

-- ---------------------------------------------------------------- SP MALO
IF OBJECT_ID('dbo.spPedidos_Buscar_Malo') IS NOT NULL DROP PROCEDURE dbo.spPedidos_Buscar_Malo;
GO
/* Problemas sembrados (para que SQLPilot los encuentre):
   1. Conversión implícita: @pNit es NVARCHAR y Clientes.Nit es VARCHAR.
   2. Predicados no SARGables: YEAR(Fecha), LTRIM(RTRIM()), ISNULL(@p, col) = col.
   3. SELECT * con columnas NVARCHAR(500) que no se usan.
   4. Sin índice en Pedidos.ClienteID ni Pedidos.EstadoID → scans.
   5. Cursor para calcular algo que se hace con un JOIN/agregado.
   6. Tabla temporal sin índice y sin estadísticas útiles.
   7. Parameter sniffing: @pEstadoID = 1 (3% de filas) vs 3 (87%).
   8. NOLOCK en todo, OPTION (RECOMPILE) olvidado, sin SET NOCOUNT.
*/
CREATE PROCEDURE dbo.spPedidos_Buscar_Malo
    @pNit        NVARCHAR(20) = NULL,
    @pEstadoID   INT          = NULL,
    @pAnio       INT          = NULL,
    @pCiudad     NVARCHAR(60) = NULL
AS
BEGIN
    DECLARE @tmp TABLE (PedidoID INT, ClienteID INT, Fecha DATETIME, Total DECIMAL(18,2), Observacion NVARCHAR(500));

    INSERT INTO @tmp
    SELECT p.PedidoID, p.ClienteID, p.Fecha, p.Total, p.Observacion
    FROM dbo.Pedidos p WITH (NOLOCK)
    JOIN dbo.Clientes c WITH (NOLOCK) ON c.ClienteID = p.ClienteID
    WHERE (@pNit IS NULL OR LTRIM(RTRIM(c.Nit)) = @pNit)
      AND ISNULL(@pEstadoID, p.EstadoID) = p.EstadoID
      AND (@pAnio IS NULL OR YEAR(p.Fecha) = @pAnio)
      AND (@pCiudad IS NULL OR UPPER(c.Ciudad) LIKE '%' + UPPER(@pCiudad) + '%');

    -- Cursor para contar líneas de cada pedido (debería ser un JOIN con agregado)
    DECLARE @PedidoID INT, @Lineas INT;
    DECLARE @res TABLE (PedidoID INT, Lineas INT);
    DECLARE cur CURSOR LOCAL FAST_FORWARD FOR SELECT PedidoID FROM @tmp;
    OPEN cur; FETCH NEXT FROM cur INTO @PedidoID;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        SELECT @Lineas = COUNT(*) FROM dbo.PedidoDetalle WITH (NOLOCK) WHERE PedidoID = @PedidoID;
        INSERT INTO @res VALUES (@PedidoID, @Lineas);
        FETCH NEXT FROM cur INTO @PedidoID;
    END
    CLOSE cur; DEALLOCATE cur;

    SELECT t.*, c.*, r.Lineas,
           (SELECT TOP 1 pr.Descripcion FROM dbo.PedidoDetalle d JOIN dbo.Productos pr ON pr.ProductoID = d.ProductoID
            WHERE d.PedidoID = t.PedidoID ORDER BY d.PrecioUnit DESC) AS ProductoMasCaro
    FROM @tmp t
    JOIN dbo.Clientes c WITH (NOLOCK) ON c.ClienteID = t.ClienteID
    LEFT JOIN @res r ON r.PedidoID = t.PedidoID
    ORDER BY t.Fecha DESC;
END
GO

-- ---------------------------------------------------------------- SP BUENO (para comparar)
IF OBJECT_ID('dbo.spPedidos_Buscar_Bueno') IS NOT NULL DROP PROCEDURE dbo.spPedidos_Buscar_Bueno;
GO
CREATE PROCEDURE dbo.spPedidos_Buscar_Bueno
    @pNit        VARCHAR(20) = NULL,
    @pEstadoID   TINYINT     = NULL,
    @pAnio       INT         = NULL,
    @pCiudad     NVARCHAR(60) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @Desde DATETIME = CASE WHEN @pAnio IS NULL THEN NULL ELSE DATEFROMPARTS(@pAnio, 1, 1) END;
    DECLARE @Hasta DATETIME = CASE WHEN @pAnio IS NULL THEN NULL ELSE DATEFROMPARTS(@pAnio + 1, 1, 1) END;

    SELECT p.PedidoID, p.Fecha, p.EstadoID, p.Total,
           c.ClienteID, c.Nit, c.Nombre, c.Ciudad,
           d.Lineas,
           pm.Descripcion AS ProductoMasCaro
    FROM dbo.Pedidos p
    JOIN dbo.Clientes c ON c.ClienteID = p.ClienteID
    CROSS APPLY (SELECT COUNT(*) AS Lineas FROM dbo.PedidoDetalle x WHERE x.PedidoID = p.PedidoID) d
    OUTER APPLY (SELECT TOP 1 pr.Descripcion FROM dbo.PedidoDetalle x JOIN dbo.Productos pr ON pr.ProductoID = x.ProductoID
                 WHERE x.PedidoID = p.PedidoID ORDER BY x.PrecioUnit DESC) pm
    WHERE (@pNit IS NULL OR c.Nit = @pNit)
      AND (@pEstadoID IS NULL OR p.EstadoID = @pEstadoID)
      AND (@Desde IS NULL OR (p.Fecha >= @Desde AND p.Fecha < @Hasta))
      AND (@pCiudad IS NULL OR c.Ciudad = @pCiudad)
    ORDER BY p.Fecha DESC
    OPTION (RECOMPILE);
END
GO

-- ---------------------------------------------------------------- calentar caché / Query Store
EXEC dbo.spPedidos_Buscar_Malo @pEstadoID = 1;                       -- plan compilado con el 3% (sniffing)
EXEC dbo.spPedidos_Buscar_Malo @pEstadoID = 3, @pAnio = 2025;        -- reutiliza ese plan con el 87%
EXEC dbo.spPedidos_Buscar_Malo @pNit = N'800000001-1';
EXEC dbo.spPedidos_Buscar_Bueno @pEstadoID = 3, @pAnio = 2025;
EXEC dbo.spPedidos_Buscar_Bueno @pNit = '800000001-1';
GO
PRINT 'Listo. Prueba: sqlpilot chat "analiza el rendimiento de dbo.spPedidos_Buscar_Malo y compáralo con dbo.spPedidos_Buscar_Bueno" -p demo';
