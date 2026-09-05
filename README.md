# SQLPilot

Copiloto de **diagnóstico y operación para DBAs de SQL Server**. Un agente LLM con herramientas tipadas sobre las DMVs que responde preguntas como *"¿por qué está lento el servidor?"*, *"¿quién bloquea a quién?"* o *"analiza este SP"* con evidencia real, y que **nunca ejecuta acciones de escritura**: las propone como script para que el DBA decida.

## Principios

- **Solo lectura, sin modo de escritura.** No existe en el código ninguna ruta que ejecute INSERT/UPDATE/DELETE/DDL/KILL: la conexión no expone método de escritura y el SQL libre pasa por una lista blanca (`SELECT`, `WITH`, DBCC/sp_ de consulta). Sesión con `READ UNCOMMITTED`, `LOCK_TIMEOUT 5s` y límite de filas. KILL, índices, estadísticas o configuración se devuelven solo como *propuestas* (texto + bitácora) con impacto, riesgo y reversión, para que el DBA las ejecute donde y cuando decida.
- **Evidencia antes que opinión.** El agente lee esperas, sesiones, planes, índices y estadísticas antes de concluir.
- **Multi-servidor.** Perfiles de conexión (Windows o SQL Auth) en `sqlpilot.toml`.
- **LLM configurable.** Anthropic (Claude) por defecto; OpenAI u Ollama vía API compatible.
- **Bitácora.** Todo lo ejecutado queda en `~/.sqlpilot/bitacora.jsonl`.

## Herramientas de diagnóstico (fase 1)

| Categoría | Herramientas |
|---|---|
| Actividad | `sesiones_activas`, `cadena_bloqueos`, `transacciones_abiertas`, `detalle_sesion` |
| Esperas | `esperas_acumuladas`, `esperas_en_ventana`, `presion_cpu_memoria_io` |
| Consultas | `consultas_costosas_cache`, `consultas_costosas_query_store`, `obtener_plan_cache`, `plan_estimado` |
| Índices | `indices_faltantes`, `indices_no_usados`, `indices_duplicados`, `fragmentacion_indices`, `indices_de_tabla` |
| Estadísticas | `estadisticas_desactualizadas`, `histograma_estadistica` |
| Deadlocks | `deadlocks_recientes` (parseados de `system_health`) |
| Espacio | `espacio_bases_datos`, `archivos_base_datos`, `tablas_mas_grandes`, `estado_tempdb` |
| Jobs / backups / log | `jobs_fallidos`, `estado_jobs`, `estado_backups`, `errores_log_sql` |
| Configuración | `configuracion_instancia` (con revisión de buenas prácticas), `configuracion_bases_datos` |
| Esquema | `buscar_objetos`, `describir_tabla`, `definicion_objeto` (sin truncar), `quien_usa_tabla` |
| SQL / operación | `ejecutar_sql_lectura`, `proponer_accion` |
| **Planes (fase 2)** | `regresiones_query_store`, `planes_de_consulta_query_store`, `comparar_planes_query_store`, `comparar_plan_estimado`, `plan_real` (SELECT o SP de lectura pura, dentro de transacción con ROLLBACK), `plan_cache_salud` |
| **Histórico (fase 2)** | `tomar_snapshot`, `listar_snapshots`, `comparar_snapshots` — "antes vs. ahora" de esperas, top queries, tamaños, I/O y contadores |
| **Informes** | `generar_informe_salud` — informe completo (21 secciones) con hallazgos priorizados y scripts, en HTML imprimible a PDF y Markdown; `sqlpilot informe --resumen-ia` agrega un resumen ejecutivo para no técnicos |
| **Seguridad / integridad (fase 2)** | `auditoria_seguridad` (sysadmin, sa, contraseñas débiles, CONTROL SERVER, TRUSTWORTHY, huérfanos, guest), `integridad_bases_datos` (último CHECKDB, suspect_pages) |

## Instalación

Requisitos: Python 3.11+, ODBC Driver 17/18 for SQL Server.

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
copy sqlpilot.toml.ejemplo sqlpilot.toml   # y edita los perfiles
set ANTHROPIC_API_KEY=sk-ant-...
```

## Uso

```bash
sqlpilot perfiles                              # perfiles configurados
sqlpilot salud -p produccion                   # prueba de conexión
sqlpilot revisar -p produccion                 # chequeo de salud sin LLM: hallazgos priorizados en pantalla
sqlpilot informe -p produccion --resumen-ia --abrir   # informe exportable HTML (imprimible a PDF) + Markdown con scripts
sqlpilot herramientas                          # catálogo de herramientas
sqlpilot ejecutar esperas_en_ventana segundos=5 -p produccion
sqlpilot ejecutar definicion_objeto nombre=dbo.spLRP_Despachos_Listar --json
sqlpilot sql "SELECT TOP 5 name FROM sys.databases"
sqlpilot chat -p produccion                    # sesión interactiva con el agente
sqlpilot chat "¿por qué está lento el servidor ahora?" -p produccion
sqlpilot servir --puerto 8000                  # API HTTP para el frontend web
```

## Servidor MCP (úsalo desde cualquier IA)

Las mismas 36 herramientas, expuestas por [Model Context Protocol](https://modelcontextprotocol.io) para Claude Code, Claude Desktop, Cursor, VS Code, etc. Todas marcadas `readOnlyHint`. Incluye prompts `diagnostico_lentitud`, `analizar_procedimiento` y `revision_salud`, y el recurso `sqlpilot://perfiles`.

```bash
sqlpilot mcp                       # stdio (lo que usan los clientes de escritorio)
sqlpilot mcp --http --puerto 8765  # streamable-http, para clientes remotos
```

**Claude Code** — ya hay un [.mcp.json](.mcp.json) en la raíz del repo; abre Claude Code en esta carpeta y aparece `sqlpilot`. Para usarlo desde cualquier proyecto:

```bash
claude mcp add sqlpilot -s user -- J:/ProyectosGithub/sqlpilot/backend/.venv/Scripts/sqlpilot.exe mcp
```

**Claude Desktop** — en `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sqlpilot": {
      "command": "J:/ProyectosGithub/sqlpilot/backend/.venv/Scripts/sqlpilot.exe",
      "args": ["mcp"],
      "cwd": "J:/ProyectosGithub/sqlpilot/backend"
    }
  }
}
```

Los perfiles y credenciales se leen de `backend/.env` y `backend/sqlpilot.toml` (por eso el `cwd`). Cada herramienta acepta `perfil` para elegir servidor y `max_caracteres` para controlar el tamaño de la respuesta (los resultados largos se recortan y se marcan con `_omitidos_<clave>` / `_recortado`).

Detalles de operación: pool de conexiones por perfil (`[agente] conexiones_por_perfil`, 4 por defecto) para llamadas concurrentes, cada herramienta corre en un hilo con timeout (`[agente] timeout_herramienta`, 120 s), respuestas con `structured_content`, errores como `isError`, logs a stderr.

## Permisos mínimos recomendados para el login de SQLPilot

```sql
CREATE LOGIN sqlpilot_ro WITH PASSWORD = '...';
GRANT VIEW SERVER STATE, VIEW ANY DEFINITION, VIEW ANY DATABASE TO sqlpilot_ro;
-- por cada base a diagnosticar:
USE [LrpDB]; CREATE USER sqlpilot_ro FOR LOGIN sqlpilot_ro;
GRANT VIEW DATABASE STATE, VIEW DEFINITION TO sqlpilot_ro;
ALTER ROLE db_datareader ADD MEMBER sqlpilot_ro;   -- solo si quieres consultas sobre datos de negocio
-- msdb para jobs/backups:
USE msdb; CREATE USER sqlpilot_ro FOR LOGIN sqlpilot_ro; ALTER ROLE SQLAgentReaderRole ADD MEMBER sqlpilot_ro;
```

## Roadmap

1. ✅ Núcleo: conexión segura, catálogo DMV, agente, CLI, API, servidor MCP.
2. ✅ Comparación de planes (Query Store, estimado, real con rollback), snapshots "antes vs. ahora", auditoría de seguridad e integridad.
3. Always On / replicación, alertas sobre snapshots, contenedor SQL Server en CI para probar las DMVs.
4. Frontend Angular: panel en vivo (bloqueos, esperas, top queries) — en pausa mientras el MCP cubra el uso diario.
