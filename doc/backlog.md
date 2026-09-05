# SQLPilot — Backlog de mejoras

Estado al 2026-09-04. Prioridad: **P1** = bloquea el uso diario · **P2** = valor alto · **P3** = deseable.
Convención: cada ítem se cierra con un commit `feat|fix(scope): ...` y se marca `[x]`.

---

## 1. Servidor MCP

### P1 — Eficiencia de contexto
- [x] Recorte por defecto de resultados grandes (`max_caracteres`, default 12.000, ajustable por llamada) con `_omitidos_<clave>` y `_recortado`. *(2026-09-04)*
- [x] `structured_content` con esquema de salida (`dict[str, Any]`) en todas las herramientas. *(2026-09-04)*
- [x] `detalle_sesion`: resumen del plan; XML solo con `incluir_xml=true`. *(2026-09-04)*
- [x] Errores como `ToolError` → `isError=true` en el cliente. *(2026-09-04)*
- [ ] Recorte por filas (`max_filas`) además de por caracteres, y recorte de strings largos dentro de listas (hoy solo en el nivel raíz).

### P1 — Operación del servidor
- [x] Pool de conexiones por perfil (`db/pool.py`, `conexiones_por_perfil`, default 4); probado con 6 herramientas en paralelo contra Azure MI. *(2026-09-04)*
- [x] Herramientas ejecutadas en hilo (`anyio.to_thread`): `esperas_en_ventana` ya no bloquea el resto. *(2026-09-04)*
- [x] Timeout por herramienta (`timeout_herramienta`, default 120 s). *(2026-09-04)*
- [x] Cancelación real: en timeout o cancelación del cliente se llama `cursor.cancel()` sobre la conexión en uso (`ConexionSql.cancelar`); probado contra Azure MI: la consulta pesada desaparece del servidor y la conexión vuelve al pool usable. *(2026-09-04)*
- [x] Logging a stderr con nivel configurable. *(2026-09-04)*
- [x] Notificaciones al cliente vía `Context.report_progress` con mensaje (inicio, recorte aplicado, timeout, error). La capacidad `logging` (`notifications/message`) quedó deprecada en la spec MCP (SEP-2577), por eso no se usa. *(2026-09-04)*
- [ ] Progreso intermedio en herramientas largas (`esperas_en_ventana` por segundo, `fragmentacion_indices` por tabla).

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
- [x] Exportar hallazgos a Markdown/HTML (informe de salud entregable): `sqlpilot informe`, herramienta `generar_informe_salud`, `sqlpilot/informes/`. HTML autocontenido imprimible a PDF; resumen ejecutivo opcional con el LLM. Probado contra Azure MI (46 hallazgos). *(2026-09-05)*
- [ ] PDF nativo (sin pasar por el navegador) si algún cliente lo exige; hoy: HTML → Imprimir → PDF.
- [ ] Informe diferencial: comparar dos informes/snapshots y reportar solo lo que cambió.

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
