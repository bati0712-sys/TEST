@echo off
REM ============================================================
REM  Instalador RedBF Agent - TODO EN UNO
REM
REM  1. Verifica permisos de Admin
REM  2. Verifica que config.ini exista
REM  3. Excluye carpeta y .exe de Windows Defender
REM  4. Instala el servicio RedBFAgent con NSSM
REM  5. Logs con rotacion + auto-restart
REM  6. Arranca el servicio
REM ============================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1

set SERVICE=RedBFAgent
set INSTALL_DIR=%~dp0
if "%INSTALL_DIR:~-1%"=="\" set INSTALL_DIR=%INSTALL_DIR:~0,-1%
set EXE=%INSTALL_DIR%\redbf-agent.exe
set CONFIG=%INSTALL_DIR%\config.ini
set NSSM=%INSTALL_DIR%\nssm.exe
set LOG_FILE=%INSTALL_DIR%\agente.log

echo.
echo ============================================================
echo  Instalador RedBF Agent
echo  Carpeta:  %INSTALL_DIR%
echo  Servicio: %SERVICE%
echo ============================================================
echo.

REM === 1. Admin ===
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Necesita permisos de Administrador.
    echo         Click derecho ^> "Ejecutar como administrador"
    pause & exit /b 1
)
echo [1/6] Permisos de Admin: OK

REM === 2. Archivos ===
if not exist "%EXE%" ( echo [ERROR] Falta redbf-agent.exe & pause & exit /b 1 )
if not exist "%NSSM%" ( echo [ERROR] Falta nssm.exe & pause & exit /b 1 )
if not exist "%CONFIG%" (
    echo [ERROR] Falta config.ini
    echo         Copia config.ini.example a config.ini y edita server_url.
    pause & exit /b 1
)
echo [2/6] Archivos requeridos: OK

REM === 3. Exclusion Defender ===
echo [3/6] Configurando exclusiones de Windows Defender...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Add-MpPreference -ExclusionPath '%INSTALL_DIR%' -ErrorAction Stop; Add-MpPreference -ExclusionProcess 'redbf-agent.exe' -ErrorAction Stop; Write-Host '      Exclusiones agregadas' } catch { Write-Host '      [WARN] No se pudo (Defender off/policy):' $_.Exception.Message }"

REM === 4. Quitar servicio + procesos previos ===
REM Detener servicio anterior si existe (reinstalacion limpia)
sc query %SERVICE% >nul 2>&1
if not errorlevel 1 (
    echo [4/6] Servicio existente, removiendo...
    "%NSSM%" stop %SERVICE% >nul 2>&1
    "%NSSM%" remove %SERVICE% confirm >nul 2>&1
    timeout /t 2 /nobreak >nul
) else (
    echo [4/6] Sin servicio previo
)
REM Matar cualquier redbf-agent.exe que haya quedado corriendo (version anterior
REM ejecutada manualmente, o de otra carpeta) para liberar el archivo.
taskkill /F /IM redbf-agent.exe >nul 2>&1
timeout /t 1 /nobreak >nul

REM === 5. Instalar servicio ===
echo [5/6] Instalando servicio %SERVICE%...
"%NSSM%" install %SERVICE% "%EXE%" >nul
"%NSSM%" set %SERVICE% AppDirectory "%INSTALL_DIR%" >nul
"%NSSM%" set %SERVICE% DisplayName "RedBF Agent (Inventario de red)" >nul
"%NSSM%" set %SERVICE% Description "Reporta inventario de hardware/software/red al servidor RedBF y ejecuta comandos remotos." >nul
"%NSSM%" set %SERVICE% Start SERVICE_AUTO_START >nul
"%NSSM%" set %SERVICE% ObjectName LocalSystem >nul
"%NSSM%" set %SERVICE% AppStdout "%LOG_FILE%" >nul
"%NSSM%" set %SERVICE% AppStderr "%LOG_FILE%" >nul
"%NSSM%" set %SERVICE% AppRotateFiles 1 >nul
"%NSSM%" set %SERVICE% AppRotateOnline 1 >nul
"%NSSM%" set %SERVICE% AppRotateBytes 10485760 >nul
"%NSSM%" set %SERVICE% AppExit Default Restart >nul
"%NSSM%" set %SERVICE% AppRestartDelay 5000 >nul
REM Anti Stop-Pending: que `nssm stop` mate el arbol directo sin esperar señales
REM (el agente lanza helpers de control/captura en otras sesiones que no responden
REM a WM_CLOSE). Sin esto, el stop del auto-update se cuelga en Stop-Pending.
"%NSSM%" set %SERVICE% AppStopMethodSkip 6 >nul
"%NSSM%" set %SERVICE% AppKillProcessTree 1 >nul
"%NSSM%" set %SERVICE% AppStopMethodConsole 1500 >nul

REM === 6. Arrancar ===
echo [6/6] Arrancando servicio...
"%NSSM%" start %SERVICE% >nul
timeout /t 3 /nobreak >nul
"%NSSM%" status %SERVICE%

echo.
echo ============================================================
echo  Instalacion completada.
echo  El equipo aparecera en el dashboard RedBF en ~1 minuto.
echo ============================================================
echo.
echo  Comandos:
echo    nssm status %SERVICE%   - estado
echo    nssm restart %SERVICE%  - reiniciar
echo    Get-Content "%LOG_FILE%" -Wait -Tail 50   (logs en vivo)
echo.
pause
endlocal
