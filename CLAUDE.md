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

## Stack técnico común
- **Python** + **PySide6** (QThread, Signal/Slot)
- **pyodbc** con ODBC Driver 17 for SQL Server
- **python-dateutil** (relativedelta) en GeneradorLetras
- Patrón: workers en QThread, UI en MainWindow
- Config en `config.json` local a cada proyecto
