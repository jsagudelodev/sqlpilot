# SQLPilot — Backlog de mejoras

Estado al 2026-09-04. Prioridad: **P1** = bloquea el uso diario · **P2** = valor alto · **P3** = deseable.
Convención: cada ítem se cierra con un commit `feat|fix(scope): ...` y se marca `[x]`.

---

## 1. Servidor MCP

### P1 — Eficiencia de contexto
- [ ] Recorte por defecto de resultados grandes (`max_filas`, `max_caracteres`) con aviso `"_omitidos": N` y parámetro para pedir más. Hoy `definicion_objeto` (SPs de 40 KB) o `indices_duplicados` (206 filas en producción) saturan el contexto del cliente.
- [ ] Devolver `structured_content` con esquema de salida en vez de `str` con JSON embebido.
- [ ] `detalle_sesion`: devolver el resumen del plan (no hasta 20 KB de XML crudo); XML solo bajo demanda (`incluir_xml=true`).
- [ ] Errores con `isError=True` en el resultado MCP (hoy van como `{"error": ...}` en texto).

### P1 — Operación del servidor
- [ ] Pool de conexiones por perfil (o conexión por llamada): una sola conexión pyodbc compartida falla si el cliente lanza herramientas en paralelo.
- [ ] `esperas_en_ventana` usa `time.sleep`: bloquea el proceso MCP/API. Ejecutar en hilo o `asyncio.sleep`.
- [ ] Timeout por herramienta y soporte de cancelación/progreso MCP (`fragmentacion_indices` en bases grandes tarda minutos).
- [ ] Logging a stderr con nivel configurable (stdio exige no tocar stdout) y `notifications/message` al cliente.

### P2 — Prompts y resources
- [ ] Prompts: `diagnosticar_bloqueos`, `revisar_indices_tabla(tabla)`, `explicar_deadlock`, `comparar_antes_ahora`.
- [ ] Resources con datos: `sqlpilot://{perfil}/salud`, `sqlpilot://{perfil}/snapshots`, `sqlpilot://{perfil}/propuestas` (acciones registradas con `proponer_accion`).
- [ ] Ordenar/redactar descripciones para que las 8 herramientas principales destaquen entre las 49.

### P2 — Distribución
- [ ] Instalable con `uvx sqlpilot mcp` / `pipx` (sin ruta absoluta al `.venv`).
- [ ] `.mcp.json` con ruta relativa o `${workspaceFolder}` para que funcione al clonar.
- [ ] Publicar en PyPI y en el registro MCP; Dockerfile para transporte HTTP.

### P3 — Seguridad del propio MCP
- [ ] Autenticación en transporte HTTP (token verifier / OAuth del SDK); hoy solo `127.0.0.1`.
- [ ] Perfiles permitidos por cliente y flag `solo_diagnostico` que oculte `ejecutar_sql_lectura`.
- [ ] Identificar en la bitácora qué cliente MCP hizo cada llamada.

---

## 2. Herramientas de diagnóstico

### P1 — Permisos y compatibilidad (hallado en Azure SQL MI)
- [ ] `jobs_fallidos`, `estado_jobs`, `errores_log_sql`: degradan con mensaje, pero falta documentar en README el rol `SQLAgentReaderRole` y `##MS_ServerStateReader##`.
- [ ] Detectar `EngineEdition` una vez por conexión y adaptar consultas (Azure MI/DB vs. on-prem): `sys.master_files`, `xp_readerrorlog`, `DBCC DBINFO`, `dm_server_services`.
- [ ] Probar contra SQL Server 2016/2017 on-prem (columnas que no existen: `dm_db_stats_properties` OK; `query_store_*` requiere 2016+; `STRING_AGG` no usado a propósito).

### P2 — Nuevas herramientas
- [ ] **Always On / HA**: estado de AG, réplicas, latencia de sincronización, cola de redo (`dm_hadr_*`).
- [ ] **Bloqueos históricos**: `blocked process report` desde XE si está configurado el `blocked process threshold`.
- [ ] **Parameter sniffing**: comparar plan compilado vs. valores típicos (`histograma_estadistica` + parámetros del plan).
- [ ] **Índices**: uso por partición, índices filtrados no aprovechados, columnstore fragmentado.
- [ ] **Espacio por tabla en el tiempo** (usar snapshots) y proyección de llenado de disco.
- [ ] **Sesiones por aplicación/host** (quién consume qué, agrupado por `program_name`/`host_name`).
- [ ] **Consultas en curso con plan real en vivo** (`dm_exec_query_statistics_xml`, 2016 SP1+).
- [ ] **Compilaciones/recompilaciones** por SP (`dm_exec_procedure_stats`, `plan_generation_num`).
- [ ] `comparar_perfiles(herramienta, perfiles)`: ejecutar lo mismo en N instancias y devolver tabla comparativa.
- [ ] Grupos de perfiles (`produccion`, `qa`) y perfil `todos`.

### P3
- [ ] Alertas sobre snapshots (umbrales: PLE, log %, backups, bloqueos) con salida para cron/Task Scheduler.
- [ ] Exportar hallazgos a Markdown/HTML (informe de salud entregable).

---

## 3. Agente propio (CLI / API)

- [ ] **P2** Streaming del texto final (hoy llega completo al terminar).
- [ ] **P2** Persistir conversaciones (`~/.sqlpilot/sesiones/`) y reanudarlas.
- [ ] **P2** `fallbacks` de refusal para Claude Opus 5 (beta `server-side-fallback`), pendiente de validar firma del SDK.
- [ ] **P3** API: expiración y límite de sesiones de chat en memoria.
- [ ] **P3** Selección de herramientas por categoría para reducir tokens del prompt (Gemini: ~3.8k tokens de definiciones por turno).

---

## 4. Calidad y pruebas

- [ ] **P1** Tests de integración con contenedor `mcr.microsoft.com/mssql/server` en CI (GitHub Actions) para ejercer las 46 herramientas contra DMVs reales.
- [ ] **P2** Tests unitarios de `comparar_snapshots` con fixtures JSON.
- [ ] **P2** Tests del wrapper MCP con cliente stdio real (hoy solo in-process).
- [ ] **P3** `mypy` en CI.

---

## 5. Frontend (en pausa)

Se retoma solo si el MCP en Claude Code/Desktop no cubre el uso diario.
- [ ] Panel en vivo: bloqueos, esperas en ventana, top queries (Angular + PrimeNG, SSE desde `/api/v1/chat` y polling de herramientas).
- [ ] Chat con historial y vista de propuestas pendientes.

---

## Hallazgos abiertos en la instancia probada (sqlmi-pdn-logit / LRP_Produccion)

No son del producto, pero salieron en las pruebas y conviene no perderlos:
- `logit_lrp` es miembro de **bulkadmin** y no tiene `CHECK_POLICY`; para diagnóstico sobra.
- Regresión de plan en `spLRP_OrdenDeCompraxNumero` (plan 11688404 vs 11688387, 107× más lento) — el `INSERT INTO #tbl_DetalleProductos`.
- `spLRP_ProductosAduana_ConsultarLeadTimexProducto`: 5 s promedio, 137k lecturas; índice faltante en `tbl_DetalleLeadTimeProveedorxProducto (LeadTimexProveedorxProductoID, Estado) INCLUDE (Dias, ConfiguracionLeadTimeID)` con impacto 97 %.
- Plan cache: 1.745 MB de 2.956 MB (59 %) son planes ad hoc de un solo uso; `optimize for ad hoc workloads` = 0.
- 206 índices duplicados/solapados en `LRP_Produccion`; estadísticas con > 3.800 % de modificaciones en `tbl_DatosGeneralesTransporteTerrestreDetalleLog`.
- `WatchDog_WatchLog` (6,9 GB) y `tbl_AWSSES_CorreosxEnviar` (4,5 GB) son las tablas más grandes: candidatas a purga/archivado.
