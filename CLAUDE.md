# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Por qué hay varios CLAUDE.md
Cada subcarpeta tiene su propio `CLAUDE.md` con el contexto específico de ese proyecto.
Claude carga automáticamente el `CLAUDE.md` de la carpeta donde está trabajando,
más este raíz. No hace falta duplicar información entre ellos.

## Bases de Datos (compartidas por todos los proyectos)

| Alias | Servidor | Base de datos | Usuario | Propósito |
|---|---|---|---|---|
| `rms` | 192.168.1.113 | `RetailDataSHOE` | retailuser | POS/ventas tiendas (RECEIPT, VOUCHER, ITEM…) |
| `dbferrini` | 192.168.1.113 | `dbferrini` | retailuser | Tablas propias: margen, GRE_Serie, GRE_Documento, GL_*, AppBF_* |
| `ferrini` | 192.168.1.10 | `FERRINI` | sa | ERP interno — stock, kardex, ventas |
| `dbferrini_vps` | 190.187.176.69 | `DBFERRINI` | — | Espejo de dbferrini en VPS (usado por AppBF en producción) |
| `va_web` | 190.116.71.100 | `VA_WEB`, `CrossChex_Vasist`, `VASEGURIDAD` | sa/`Abcd1234$$2026*` | Personal, marcaciones, usuarios (AppBF marcador). **Pwd de `sa` cambiada 2026-06-04** (era `abcd1234`, débil — vector del malware que infectó el server, ver abajo). |
| `dyspow` | 192.168.1.114 | `BD_DYSPOW_BFX` | DyspowUser/Dyspow | ERP Dyspow — letras, OCs, productos (GeneradorLetras_web) |
| `bdfasea` | 192.168.1.114 | `BDFASEA` | UserBF/abcd1234$$2026 | Copia 1:1 de BD_DYSPOW_BFX optimizada (2.2 GB, PAGE compression, recovery SIMPLE). Creada y optimizada 29-may-2026. UserBF es owner, db_owner implícito. |

Contraseña común: `retail` (retailuser en ambos servidores 192.168.1.113).

### Servidor 192.168.1.114 (BFSERVERDB02 — SQL Server 2022 Enterprise)

Hospeda **dos BDs separadas**: `BD_DYSPOW_BFX` (producción ERP, NO TOCAR) y `BDFASEA` (sandbox/integraciones BF).

**Modelo de acceso a BDFASEA (lockdown aplicado 29-may-2026)**:
- ✅ **UserBF** entra como db_owner — único login autorizado para uso normal
- ⚠️ **sa** entra como dbo — sysadmin estructural, no se puede bloquear
- ❌ **DyspowUser** bloqueado (tras reducción de permisos, ver abajo)
- ❌ **retailuser, DYSPOW\Administrador** con DENY CONNECT explícito
- ❌ Cualquier login normal nuevo: sin acceso (sin mapeo)

**Reducción de permisos de DyspowUser (29-may-2026)**: el instalador de Dyspow había dejado a `DyspowUser` con TODOS los fixed server roles (sysadmin, securityadmin, serveradmin, etc.) y como owner de 5 BDs no relacionadas con Dyspow. Se redujo a solo `db_owner` sobre `BD_DYSPOW_BFX`. El ERP Dyspow sigue funcionando (sus 58 sesiones .NET desde DCASTILLO 192.168.1.9 son operaciones db_owner normales), pero **el proveedor de Dyspow** (que usaba DyspowUser+SSMS desde IPs públicas 190.117.50.70 / 38.25.53.238 / hosts DTP1G1001 y DTP1G1002 para soporte) **ya no puede administrar el server**. Cuando contacten, opciones:
1. Darles `sa/Abcd1234$$2026*` temporalmente (pwd cambiada 2026-06-04)
2. Crear un login admin nuevo solo para soporte (`BFSupport`)
3. Sesión remota guiada

**Bugs aprendidos en SQL Server 2022**:

1. **`##MS_DatabaseConnector##`** — rol sin documentación clara que permite conectar a CUALQUIER BD sin mapeo, pasando por encima de DENY CONNECT/guest. Para bloquear de verdad un login:
   ```sql
   ALTER SERVER ROLE [##MS_DatabaseConnector##] DROP MEMBER [login];
   ```

2. **Mapeo orphan en master con `db_denydatareader`** — instaladores (como Dyspow) crean al login un USER en `master` con `db_denydatareader`/`db_denydatawriter`. Esto bloquea silenciosamente la lectura de DMVs (`sys.dm_os_host_info` etc.) aunque tengas `VIEW SERVER STATE` y todos los GRANTs. Error típico: `Se denegó SELECT en dm_os_host_info, base de datos mssqlsystemresource`. SSMS no puede conectar. **Fix**: `USE master; DROP USER [login]` para que conecte como guest. Cuando auditás un login, no solo mirar `sys.server_principals` — también `master.sys.database_principals`.

3. **DENY VIEW ANY DATABASE oculta BDs en Object Explorer aunque el login tenga db_owner** — un login con `DENY VIEW ANY DATABASE` no ve ninguna BD en el explorer de SSMS, salvo aquellas de las que es OWNER. No basta con db_owner como rol. **Fix**: `ALTER AUTHORIZATION ON DATABASE::BDName TO [login]` para hacer al login owner (y queda como dbo dentro de la BD automáticamente).

4. **Trigger DDL que enmascara errores como 208 "objeto no válido"** — si hay un trigger DDL a nivel BD que falla en su INSERT (ej. violación NOT NULL al loguear), SQL Server revierte la operación original y reporta el error 208 "objeto no válido" en lugar del error real 23000. Detección: revisar `sys.triggers WHERE parent_class_desc = 'DATABASE'` y `sys.server_triggers`. **Fix**: `DISABLE TRIGGER [name] ON DATABASE` antes de DROP/UPDATE STATISTICS, o eliminar el trigger si no se necesita. Bug encontrado en BDFASEA con `TRG_T_001_DDL_AUDITORIA` durante la optimización.

Scripts: lockdown y rollback en `AppBF/scripts/_archive/dyspow_copy_step*.py`, `reduce_dyspowuser_permissions.py`, `rollback_dyspowuser_permissions.py`, y los `fix_dyspowuser_ssms_v*.py` (documentan cada hipótesis fallida hasta encontrar la causa real en `check_denys_master.py`). Snapshot del estado original en `dyspowuser_snapshot.json` permite revertir todo en ~5 segundos.

**Incidente malware (2026-06-04) — servidor 190.116.71.100 / 192.168.1.114 comprometido**: el server (que hospeda BD_DYSPOW_BFX + BDFASEA + VA_WEB/CrossChex/VASEGURIDAD) estuvo infectado por un cryptominer Monero desde **24-abr-2026**. Vector: puerto **1433 expuesto a internet** + `sa` con pwd débil (`abcd1234`). Hallazgos: minero `sysjzmn57svc.exe` como servicio `sys6ag8x8` (pool `gulf.moneroocean.stream`, carpeta falsa `C:\ProgramData\Microsoft\DeviceSync`), cargador `svchost.exe` falso en `C:\Windows\Temp` + watchdog `sysjmgrmgr.exe`, persistencia (tareas `AppDataCacheMonitor/Service` + clave Run `AppDataCacheMgr`), ~3,002 jobs SQL Agent `SO_*` maliciosos + 2 logins backdoor sysadmin (`MSSQLSERVER`, `SQLAGENT`), `xp_cmdshell`/`Ole Automation`/`Ad Hoc`/`allow updates` habilitados, y Windows Defender apagado vía política de registro. **Erradicado el 2026-06-04**: contención SQL (jobs/logins/configs OFF), limpieza Windows (minero+persistencia eliminados, Defender reactivado), `sa` cambiada a `Abcd1234$$2026*`. **PENDIENTE: cerrar/restringir el puerto 1433 en el FortiGate** (en gestión). Detalle completo en `AppBF/INCIDENTE-DYSPOW-2026-06-04.md`. Coincide con que el servicio de migración de ventas RMS→Dyspow (`IntegracionBFX Worker`, ver AppBF/CLAUDE.md) se detuvo el 25-may por servidor inestable.

## Proyectos

### [AppBF](AppBF/CLAUDE.md)
`F:\vcode\AppBF\` — Dashboard Bruno Ferrini (sistema integral; reemplaza la app PHP legacy)
- **Backend**: FastAPI (Python 3.14) + pyodbc + JWT (`backend/app/main.py`); virtualenv en `backend/Venv/`
- **Frontend**: React 19 + Vite + Tailwind CSS 4 + React Router 7 + Lucide React
- **BD principal**: `DBFERRINI` en 190.187.176.69 (VPS), más conexiones a FERRINI (.10), RetailDataSHOE (.113), VA_WEB/CrossChex/VASEGURIDAD (190.116.71.100)
- **Arranque**: `restart_backend.bat` (raíz) → `http://localhost:8000`
- **Módulos**: dashboard, reportes (margen, comparativos, KPI inventario, stock/venta horizontal), tools (comparador, guías, modelos, kardex, traspasos paso 1/2/3, catálogo BF), pedidos web, clientes, consulta, compras, tienda, tickets, personal, giftcard, cobranza, traspasos, cxp, alquileres, almacén, **marcador** (Anviz SDK ctypes — reemplaza app `marcador/` standalone), **camaras** (DVRs Hikvision via ISAPI), **web** (dashboard tipo Power BI con mapa Perú), **sync_dyspow** (crear vendedores en Dyspow T_059 desde RMS EMPLOYEE) + integración Dyspow productos en módulo Compras (botón "Dyspow" crea productos T_040 desde rango de OC, idempotente, INSERT con todos los campos + UPDATE barcode EAN-13)
- **Permisos**: registro central en `backend/app/permissions.py`; super-admins en `AppBF_SuperAdmins`
- **Auth**: JWT + bcrypt; super-admin bypass; permisos granulares por módulo
- **Tareas standalone**: `marcador_sync/` (sync relojes ANVIZ desde Windows Server 2012, sin depender del backend)
- Ver `AppBF/CLAUDE.md` para detalle exhaustivo de cada submódulo, schemas, SPs, y rutas frontend

### [MIGRAR A SA](MIGRAR A SA/CLAUDE.md)
`F:\vcode\MIGRAR A SA\migrar_sa.py`
App **PySide2** (Qt 5) que migra diariamente desde RetailDataSHOE → FERRINI:
- Ventas (cabventa, detventa, kardex, cobros)
- Guías de salida NS entre tiendas
- Ingresos NI por transferencia (auto-fix y revisión)
- Devoluciones a proveedor (VOUCHER TypeCode='R') → NS/SDP
- Ingresos de proveedor (VOUCHER TypeCode='P') → NI/IOP
- **Nota**: usa PySide2 (no PySide6) para compatibilidad con Windows Server 2012
- Build: `build.bat` → PyInstaller `--onedir --windowed`, excluye PySide6 explícitamente
- Carpeta `setup/` con scripts de instalación para PC destino

### [Venta](Venta/CLAUDE.md)
`F:\vcode\Venta\margen.py`
App PySide6 que genera la tabla de margen en dbferrini:
- Lee cabventa + detventa de FERRINI
- Cruza con RMS para descuentos/promociones (PROMO, RECEIPT_LINE)
- Enriquece con maestros FERRINI (modelo, color, talla, temporada, campaña, proveedor)
- Inserta en `dbferrini.dbo.margen`

### [APP CATALOGO](APP CATALOGO/)
`F:\vcode\APP CATALOGO\generar_archivos.py`
App PySide6 + pandas que genera el archivo Excel de catálogo para importar a RetailDataSHOE:
- Lee un Excel de entrada (hoja seleccionable) con datos del proveedor
- Lookup en RMS: CLASS, SUBCLASS, MASTER_SIZE, SEASON, VENDOR
- Lookup en FERRINI: `maecol` (CodColor por RTRIM(descripcion)), `maeart` (ALU por descripcion+temporada)
- Parámetros manuales: talla desde/hasta, Season, Vendor, PO Number, MasterSize
- Genera Excel de salida con columnas en orden exacto de `catalogo_res.xlsx`
- Config en `catalogo_py/config.json`: claves `db` (RMS=192.168.1.113) y `maeart` (FERRINI=192.168.1.10)

### [RMS](RMS/CLAUDE.md)
`F:\vcode\RMS\` — App web para emitir documentos electrónicos SUNAT desde RetailDataSHOE
- **Backend**: FastAPI + uvicorn (`backend/main.py`), lógica en `backend/engine.py`
- **Frontend**: SPA vanilla JS ES modules (`frontend/`); Tailwind + DaisyUI vía CDN
- Arranque: `python run.py --port 8003` → `http://127.0.0.1:8003`
- **GRE**: genera Guías de Remisión tipo 09 (devoluciones a proveedor, serie T002) vía Factus POS Manager
- **Boletas/Facturas/NC**: reenvía ventas manuales (RECEIPT ManualReceipt=1) a SUNAT vía Factus
- Factus firma documentos via `C:\Factus\inbox\` → `C:\Factus\outbox\` (file-based, ~2 s)
- **Tarea automática**: `tareas/auto_reenvio.bat` → llama a la API de RMS, genera log diario y envía correo

### [GeneradorLetras](GeneradorLetras/)
`F:\vcode\GeneradorLetras\generador_letras.py`
App PySide6 que reemplaza y mejora GeneradorLetras_v13.xlsm:
- Genera cronogramas de letras de cambio distribuyendo hacia atrás desde Fecha Límite
- Respeta presupuesto diario por mes, días bloqueados, feriados, montos mín/máx, separación mínima
- 5 pestañas: Generador, Parámetros, Histórico, Base de Datos, Datos Externos
- Persistencia en JSON (`data/`): base.json, parametros.json, historico.json, externos.json
- Diseñado para uso multi-usuario en carpeta de red (escrituras atómicas + reintentos)
- Exporta cronograma a Excel (openpyxl)
- Build: `GeneradorLetras.spec` + `build.bat` → ZIP `GeneradorLetras_red_vYYYYMMDD.zip`

### [GeneradorLetras_web](GeneradorLetras_web/CLAUDE.md)
`F:\vcode\GeneradorLetras_web\` — versión web de GeneradorLetras (misma lógica, nueva interfaz)
- **Backend**: FastAPI + uvicorn (`backend/main.py`), motor de cálculo en `backend/engine.py`
- **Frontend**: SPA vanilla JS ES modules, sin build step (`frontend/`); SheetJS vía CDN para exportar Excel
- Arranque: `python run.py --host 0.0.0.0 --port 8001` → `http://192.168.1.71:8001`
- **Persistencia**: SQL Server — `dbferrini` en 192.168.1.113 (retailuser/retail)
  - Capa: `backend/persistence_sql.py`
  - Conexión configurada en `backend/db_config.json`
  - DDL en `backend/create_tables.sql` (ejecutar desde SSMS conectado a dbferrini)
- **Pestaña OC Dyspow**: crea productos y OCs en BD_DYSPOW_BFX (empresa 2 = SHOE TRADE) jalando POs de RMS (`RetailDataSHOE` mismo servidor/credenciales). Ver `GeneradorLetras_web/CLAUDE.md` para detalle.

#### Tablas GL_ en dbferrini

| Tabla | Contenido |
|---|---|
| `GL_Config` | Singleton de configuración + credenciales Dyspow (nunca exponer password por GET) |
| `GL_PeriodoBloqueado` | Días bloqueados del calendario |
| `GL_PresupuestoMensual` | Presupuesto mensual (1 fila por mes) — `acumulado_externo` y `acumulado_saldo` se recalculan siempre desde GL_Externo |
| `GL_Empresa` | Empresas emisoras |
| `GL_Proveedor` | Proveedores (nombre, RUC, `clase`: 'letra'/'factoring'/'otro'/'') |
| `GL_Temporada` | Temporadas |
| `GL_Moneda` | Monedas |
| `GL_Operacion` | Cabecera de cada cronograma generado (`operacion_id` = `OP-NNNN`) |
| `GL_Letra` | Letras individuales de cada operación (CASCADE DELETE desde GL_Operacion) |
| `GL_OperacionFactura` | Facturas vinculadas a cada operación (CASCADE DELETE desde GL_Operacion) |
| `GL_Externo` | Compromisos externos — manuales y Dyspow. `saldo` = balance restante en moneda original (editable, default=monto_original) |

**Reglas de persistencia:**
- `acumulado_externo`: Σ `monto_pen` de GL_Externo por mes/año — recalculado en cada GET/PUT `/api/params`
- `acumulado_saldo`: Σ `saldo × tc` de GL_Externo por mes/año — recalculado junto con `acumulado_externo`, no se persiste en BD
- `next_operacion_id()`: consulta `MAX` sobre `GL_Operacion` en lugar de generar UUID
- Import Dyspow es idempotente: ID = `MD5("DY|{descripcion}|{fecha_vcto}")[:12]`; `upsert_ext_dyspow()` hace DELETE+INSERT preservando `saldo` editados manualmente (solo pisa si `saldo == monto_original`)
- JS/CSS se sirven con `Cache-Control: no-cache` via `NoCacheStaticMiddleware`
- **Export Datos Externos**: solo exporta filas cuyo proveedor tenga `clase='letra'` o `clase='factoring'`

#### Dyspow — `BD_DYSPOW_BFX` en 192.168.1.114
Credenciales en `GL_Config` — nunca exponer password por GET; PUT conserva password existente si el cliente envía vacío.

**Tablas principales:**

| Tabla | Descripción | Campos clave |
|---|---|---|
| `T_267_LETRA_PAGO` | Planilla de Letras en Cartera (PLC) | `C_VAR_NUMERO_LETRA_PAGO`, `C_INT_CODIGO_PROVEEDOR`, `C_INT_CANTIDAD_LETRA`, `C_DEC_TOTAL` |
| `T_269_CANJE_PAGO` | **Letras individuales emitidas** — fuente correcta para Datos Externos | `C_INT_CODIGO_LETRA_PAGO`, `C_VAR_NUMERO_SERIE`, `C_VAR_NUMERO_CANJE`, `C_DAT_FECHA_VENCIMIENTO`, `C_DEC_IMPORTE`, `C_INT_CODIGO_ETAPA` |
| `T_514_SALDO_INICIAL_LETRA_COMPRA` | Letras de saldo inicial (anteriores al sistema Dyspow) | `C_VAR_NUMERO_SERIE`, `C_VAR_NUMERO_DOCUMENTO`, `C_DAT_FECHA_PAGO` (vcto), `C_DEC_TOTAL` |
| `T_122_COMPRA` | Facturas/compras de proveedores | `C_INT_CODIGO_COMPRA`, `C_VAR_NUMERO_SERIE`, `C_VAR_NUMERO_DOCUMENTO` |
| `T_060_PROVEEDOR` | Proveedores | `C_INT_CODIGO_PROVEEDOR`, `C_VAR_RAZON_SOCIAL` |
| `T_067_TIPO_CAMBIO` | Tipos de cambio | `C_DEC_VENTA` ← usar este |
| `T_633_ETAPA_LETRA_PAGO` | Estados | 1=EN CARTERA, 2=EN RENOVACION, 3=ENDOSADA, 4=EN DESCUENTO, 5=EN PROTESTO |

**Import Datos Externos** (`POST /api/dyspow/importar-letras`):
- Query UNION ALL: T_269 JOIN T_267 + T_060 + T_019 + T_066 + T_067 + T_633 **UNION** T_514 + T_060 + T_019 + T_066 + T_067
- Descripción = `{PLC_REF} / {SERIE}-{NUMERO_CANJE}` (ej. "PLC-202512-000000014 / PV26-353")
- Total: ~1893 letras (1436 de T_269 + 457 de T_514)

### [Marcador](marcador/CLAUDE.md)
`F:\vcode\marcador\` — Control de Asistencia SHOE TRADE S.A.C. (reemplaza VA_WEB)
- **Backend**: FastAPI + uvicorn (`backend/main.py`), puerto 8004
- **Frontend**: SPA vanilla JS ES modules (`frontend/`), sidebar vertical con 7 módulos
- **Auth**: sesión cookie httponly `marcador_session`, valida contra `VASEGURIDAD.ADM_Usuario`
  - Password encoding: Vigenere key `"682001"` = `[54,56,50,48,48,49]`, decode como `cp1252`
- **BDs**: 192.168.1.14 — `VA_WEB` (personal), `CrossChex_Vasist` (marcaciones), `VASEGURIDAD` (usuarios)
- Arranque: `python run.py --port 8004` → `http://192.168.1.71:8004`
- 34 relojes ANVIZ registrados

### [GeneradorLetras_excel](GeneradorLetras_excel/)
`F:\vcode\GeneradorLetras_excel\GENERADOR DE LETRAS - VERSION FINAL 10.04.26.xlsm`
Archivo Excel con macros VBA (versión 15) — generador de cronogramas de letras de cambio:
- **Hoja Generador**: tabla de facturas ampliada a 70 filas (16–85); subtotales en fila 86
- **Hoja Imprimible**: también ampliada a 70 facturas (hasta fila 70)
- **Hoja Histórico**: columna 9 = `CODIGO LETRA` (formato `{temporada}{0000}`), reemplazó `N LETRAS`
- **VBA v15** (`GeneradorLetras_NUEVO.bas`): constantes actualizadas (`FACT_END=85`, `SCHED_START=89`,
  `DIST_START=129`, `DIST_END=139`); fix `LimpiarFormulario` apunta solo a filas de datos reales
- Script de modificación: `modificar_xlsm.py` (win32com) + `aplicar_vba.py`
- Backup: `...BACKUP.xlsm`

### [Traspasos](traspasos/CLAUDE.md)
`F:\vcode\traspasos\` — Gestión de traspasos de stock entre tiendas (3 pasos)
- **Paso 1** ✓: Reporte de margen con filtros → Excel (hoja Detalle + hoja Pivot agrupada por Depto/Prov/Campaña)
- **Paso 2** ✓: Sube Excel Paso 1 → detecta grupos de color en Pivot → ejecuta USP_StockHorizontal por grupo → Excel con hoja Pivot agregada + hoja Detalle raw por grupo
- **Paso 3** (pendiente): Lee stock horizontal → filas amarillas = traspasos → hojas por tienda origen con destino
- Fuente: `dbferrini.dbo.margen` (~2.2M filas); Filtros: fecha, campaña, proveedor, departamento, tienda
- Arranque: `python run.py` → `http://127.0.0.1:8006` (host 0.0.0.0 bloqueado por firewall local)

### Procedimiento almacenado: `USP_StockHorizontal` (dbferrini)
Reporte de stock pivotado por talla. Versión corregida en `F:\vcode\USP_StockHorizontal_v2.sql`:
- Usa `#BASE` y `#RESULTADOS` (tablas `#temp`) en lugar de tablas globales → elimina colisiones entre usuarios
- Fix: tránsito usa `CASE avgcost WHEN 0 THEN lastcost` igual que stock
- Un solo cursor de tallas ordenado por `maetal.Correlativo`
- Totales en un único `UPDATE … GROUP BY` sin cursores adicionales

## Stack técnico común
- **Python** + **PySide2** (QThread, Signal/Slot) para apps de escritorio en Windows Server 2012
- **PySide6** para apps de escritorio en Windows 10+ (GeneradorLetras, Venta, APP CATALOGO)
- **FastAPI + uvicorn** para apps web (RMS, GeneradorLetras_web, Marcador, Traspasos, AppBF)
- **React 19 + Vite + Tailwind 4** para SPA modernas (AppBF/frontend)
- **JWT (python-jose) + bcrypt** para auth en AppBF
- **pyodbc** con ODBC Driver 17 for SQL Server
- **python-dateutil** (relativedelta) en GeneradorLetras
- Config en `config.json`, `db_config.json` o `.env` local a cada proyecto
