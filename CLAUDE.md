# F:\vcode — Workspace Principal

## Por qué hay varios CLAUDE.md
Cada subcarpeta tiene su propio `CLAUDE.md` con el contexto específico de ese proyecto.
Claude carga automáticamente el `CLAUDE.md` de la carpeta donde está trabajando,
más este raíz. No hace falta duplicar información entre ellos.

## Bases de Datos (compartidas por todos los proyectos)

| Alias | Servidor | Base de datos | Usuario | Propósito |
|---|---|---|---|---|
| `rms` | 192.168.1.113 | `dbferrini` | retailuser | RetailDataSHOE — POS/ventas tiendas |
| `ferrini` | 192.168.1.10 | `FERRINI` | sa | ERP interno — stock, kardex, ventas |

La tabla `margen` en `dbferrini` es destino de análisis de rentabilidad.

## Proyectos

### [MIGRAR A SA](MIGRAR A SA/CLAUDE.md)
`F:\vcode\MIGRAR A SA\migrar_sa.py`
App PySide6 que migra diariamente desde RetailDataSHOE → FERRINI:
- Ventas (cabventa, detventa, kardex, cobros)
- Guías de salida NS entre tiendas
- Ingresos NI por transferencia (auto-fix y revisión)
- Devoluciones a proveedor (VOUCHER TypeCode='R') → NS/SDP
- Ingresos de proveedor (VOUCHER TypeCode='P') → NI/IOP

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

### [GeneradorLetras_web](GeneradorLetras_web/)
`F:\vcode\GeneradorLetras_web\` — versión web de GeneradorLetras (misma lógica, nueva interfaz)
- **Backend**: FastAPI + uvicorn (`backend/main.py`), motor de cálculo en `backend/engine.py`
- **Frontend**: SPA vanilla JS ES modules, sin build step (`frontend/`)
- Arranque: `python run.py --port 8001` → `http://127.0.0.1:8001`
- Datos en `backend/data/` (mismos JSON que el desktop)

#### Dyspow — `BD_DYSPOW_BFX` en 192.168.1.114
Credenciales en `backend/data/dyspow_config.json` — nunca exponer password por GET; PUT conserva password existente si el cliente envía vacío.

**Tablas principales:**

| Tabla | Descripción | Campos clave |
|---|---|---|
| `T_267_LETRA_PAGO` | Planilla de Letras en Cartera (PLC) — cabecera del documento de pago | `C_VAR_NUMERO_LETRA_PAGO` (ej. PLC-202512-000000014), `C_INT_CODIGO_PROVEEDOR`, `C_INT_CODIGO_MONEDA`, `C_INT_CODIGO_TIPO_CAMBIO`, `C_INT_CANTIDAD_LETRA`, `C_DEC_TOTAL`, `C_INT_CODIGO_ESTADO` |
| `T_268_LETRA_PAGO_DETALLE` | Facturas/compras vinculadas a cada PLC | `C_INT_CODIGO_LETRA_PAGO` (FK→T_267), `C_INT_CODIGO_COMPRA` (FK→T_122), `C_DEC_IMPORTE`, `C_DEC_TOTAL` (PEN), `C_INT_ITEM` |
| `T_269_CANJE_PAGO` | **Letras individuales emitidas** (una fila por letra de cambio) — es la tabla correcta para Datos Externos | `C_INT_CODIGO_LETRA_PAGO` (FK→T_267), `C_INT_CODIGO_CANJE` (PK), `C_VAR_NUMERO_SERIE` (ej. PV26), `C_VAR_NUMERO_CANJE` (ej. 353), `C_DAT_FECHA_EMISION`, `C_DAT_FECHA_VENCIMIENTO`, `C_DEC_IMPORTE`, `C_VAR_LUGAR_GIRO`, `C_VAR_CODIGO_UNICO`, `C_INT_CODIGO_ETAPA` |
| `T_514_SALDO_INICIAL_LETRA_COMPRA` | Letras de saldo inicial (anteriores al sistema Dyspow) | `C_VAR_NUMERO_SERIE`, `C_VAR_NUMERO_DOCUMENTO`, `C_DAT_FECHA_EMISION`, `C_DAT_FECHA_PAGO` (vcto), `C_DEC_TOTAL`, `C_INT_CODIGO_PROVEEDOR`, `C_VAR_NUMERO_UNICO` |
| `T_122_COMPRA` | Facturas/compras de proveedores | `C_INT_CODIGO_COMPRA`, `C_VAR_NUMERO_SERIE`, `C_VAR_NUMERO_DOCUMENTO`, `C_DAT_FECHA_EMISION`, `C_INT_CODIGO_PROVEEDOR` |
| `T_060_PROVEEDOR` | Proveedores | `C_INT_CODIGO_PROVEEDOR`, `C_VAR_RAZON_SOCIAL` — JOIN sin FK de empresa |
| `T_019_EMPRESA` | Empresas | `C_INT_CODIGO_EMPRESA`, `C_VAR_RAZON_SOCIAL` |
| `T_066_MONEDA` | Monedas | `C_INT_CODIGO_EMPRESA`, `C_INT_CODIGO_MONEDA`, `C_VAR_CODIGO_SUNAT` |
| `T_067_TIPO_CAMBIO` | Tipos de cambio | `C_INT_CODIGO_EMPRESA`, `C_INT_CODIGO_TIPO_CAMBIO`, `C_DEC_VENTA` ← usar este |
| `T_633_ETAPA_LETRA_PAGO` | Estados de letra individual | 1=EN CARTERA, 2=EN RENOVACION, 3=ENDOSADA A TERCEROS, 4=EN DESCUENTO, 5=EN PROTESTO |

**Relación clave:**
- 1 PLC (T_267) → N letras individuales (T_269) vía `C_INT_CODIGO_LETRA_PAGO`
- 1 PLC (T_267) → N facturas (T_268) → T_122 vía `C_INT_CODIGO_COMPRA`
- `T_267.C_INT_CANTIDAD_LETRA` = total de rows en T_269 para ese PLC

**Import Datos Externos** (`POST /api/dyspow/importar-letras`):
- Query UNION ALL: T_269 JOIN T_267 + T_060 + T_019 + T_066 + T_067 + T_633 **UNION** T_514 + T_060 + T_019 + T_066 + T_067
- Descripción = `{PLC_REF} / {SERIE}-{NUMERO_CANJE}` (ej. "PLC-202512-000000014 / PV26-353")
- Total: ~1893 letras (1436 de T_269 + 457 de T_514)

**Otros endpoints Dyspow:**
- `GET /api/dyspow/schema/{tabla}` — columnas de cualquier tabla (diagnóstico)
- `POST /api/dyspow/importar-proveedores` — importa T_060 → base.json (sin duplicar por nombre)
- `POST /api/dyspow/importar-empresas` — importa T_019 → base.json (sin duplicar)

#### Funcionalidades del frontend
- **Histórico**: búsqueda global + filtros por Proveedor/Temporada/Moneda/Estado + ordenamiento por columna + importar desde Excel (plantilla descargable)
- **Datos Externos**: búsqueda + filtros por Empresa/Proveedor/Estado/Fuente/Fecha rango + ordenamiento por columna
- Migración automática de `externos.json` viejo (formato desktop) al nuevo formato en GET /api/externos

## Stack técnico común
- **Python** + **PySide6** (QThread, Signal/Slot)
- **pyodbc** con ODBC Driver 17 for SQL Server
- **python-dateutil** (relativedelta) en GeneradorLetras
- Patrón: workers en QThread, UI en MainWindow
- Config en `config.json` local a cada proyecto
