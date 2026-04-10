@echo off
chcp 65001 >nul
echo.
echo ══════════════════════════════════════════════════
echo   Compilando GeneradorLetras — Letras de Cambio
echo ══════════════════════════════════════════════════
echo.

:: ── Limpiar builds anteriores ────────────────────────────────────────────────
if exist dist\GeneradorLetras rmdir /s /q dist\GeneradorLetras
if exist build                rmdir /s /q build
if exist GeneradorLetras_red_*.zip del /q GeneradorLetras_red_*.zip

:: ── Compilar ─────────────────────────────────────────────────────────────────
echo [1/3] Compilando con PyInstaller...
pyinstaller --noconfirm GeneradorLetras.spec

if errorlevel 1 (
    echo.
    echo [ERROR] Falló la compilación.
    pause
    exit /b 1
)

:: ── Crear carpeta data compartida ─────────────────────────────────────────────
echo [2/3] Preparando estructura para red...
mkdir dist\GeneradorLetras\data 2>nul

:: LEEME para el administrador de red
(
echo GENERADOR DE LETRAS DE CAMBIO — INSTALACION EN RED
echo =====================================================
echo.
echo ESTRUCTURA EN LA CARPETA DE RED:
echo   GeneradorLetras.exe   ^<-- ejecutar desde aqui
echo   data\                 ^<-- datos COMPARTIDOS entre todos los usuarios
echo     base.json           ^<-- proveedores, empresas, temporadas
echo     parametros.json     ^<-- configuracion general
echo     historico.json      ^<-- historial de letras generadas
echo     externos.json       ^<-- letras emitidas fuera del sistema
echo.
echo IMPORTANTE:
echo   - Todos los usuarios deben tener acceso de LECTURA Y ESCRITURA
echo     a la carpeta de red y subcarpeta data\
echo   - NO mover GeneradorLetras.exe fuera de esta carpeta
echo   - NO borrar la carpeta data\
echo   - Los datos se comparten automaticamente entre usuarios
echo   - Las escrituras son seguras ^(mecanismo anti-conflicto incluido^)
echo.
echo REQUISITO: Windows 7 o superior, 64 bits
echo NO requiere instalacion ni .NET ni otros programas.
) > dist\GeneradorLetras\LEEME.txt

:: ── Crear ZIP ─────────────────────────────────────────────────────────────────
echo [3/3] Creando paquete ZIP...
python -c "
import shutil, datetime
fecha = datetime.date.today().strftime('%%Y%%m%%d')
nombre = f'GeneradorLetras_red_v{fecha}'
shutil.make_archive(nombre, 'zip', 'dist', 'GeneradorLetras')
import os
size = os.path.getsize(f'{nombre}.zip') / (1024*1024)
print(f'  Creado: {nombre}.zip  ({size:.1f} MB)')
"

echo.
echo ══════════════════════════════════════════════════
echo   LISTO
echo.
for %%f in (GeneradorLetras_red_*.zip) do echo   ZIP: %%f
echo.
echo   INSTRUCCIONES:
echo   1. Copiar el ZIP a la carpeta de red y descomprimir
echo   2. Dar permisos de Lectura+Escritura a todos los usuarios
echo   3. Cada usuario ejecuta GeneradorLetras.exe directamente
echo      desde la carpeta de red (o con un acceso directo)
echo ══════════════════════════════════════════════════
echo.
pause
