/* =====================================================================
   SQLPilot — Escenario de bloqueo en vivo
   Ejecutar en DOS ventanas de SSMS distintas sobre SQLPilotDemo.
   ===================================================================== */

-- ===== VENTANA 1: head blocker "ocioso con transacción abierta" (típico de una app sin COMMIT)
USE SQLPilotDemo;
BEGIN TRANSACTION;
UPDATE dbo.Pedidos SET Observacion = N'bloqueado por demo' WHERE PedidoID BETWEEN 1 AND 500;
-- NO ejecutes COMMIT todavía. Deja la ventana así.

-- ===== VENTANA 2: víctima
USE SQLPilotDemo;
SELECT COUNT(*) FROM dbo.Pedidos WHERE PedidoID BETWEEN 1 AND 1000;   -- se queda esperando (LCK_M_S)

-- ===== Mientras tanto, en la terminal:
--   sqlpilot ejecutar cadena_bloqueos -p demo
--   sqlpilot chat "hay usuarios quejándose de que todo está congelado, ¿qué pasa?" -p demo
--
-- El agente debe identificar el head blocker (ventana 1) como sesión ociosa con transacción
-- abierta y PROPONER `KILL <spid>` sin ejecutarlo.

-- ===== Para liberar: en la VENTANA 1
-- ROLLBACK;
