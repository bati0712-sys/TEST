@echo off
REM ============================================================
REM  Build del RedBF Agent -> dist\redbf-agent.exe
REM  Requiere: pip install pyinstaller
REM ============================================================
chcp 65001 >nul 2>&1
echo.
echo ============================================================
echo  Compilando RedBF Agent...
echo ============================================================
echo.

REM Limpiar builds previos
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

python -m PyInstaller redbf_agent.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo [ERROR] Fallo el build. Revisa los mensajes de arriba.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build OK -^> dist\redbf-agent.exe
echo ============================================================
echo.
echo  Siguiente: ejecuta empaquetar.bat para armar el instalador
echo  completo (exe + config + nssm + instalar.bat).
echo.
pause
