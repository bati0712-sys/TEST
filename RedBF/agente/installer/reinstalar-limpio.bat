@echo off
REM ============================================================
REM  REINSTALACION LIMPIA del RedBF Agent
REM  Borra cualquier resto del agente anterior (servicio, procesos)
REM  y lo instala fresco. Ejecutar como Administrador.
REM
REM  Debe estar en la MISMA carpeta que: redbf-agent.exe, nssm.exe, config.ini
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

net session >nul 2>&1
if errorlevel 1 ( echo [ERROR] Ejecutar como Administrador ^(click derecho^) & pause & exit /b 1 )

echo.
echo ============================================================
echo  REINSTALACION LIMPIA - RedBF Agent
echo  Carpeta: %INSTALL_DIR%
echo ============================================================
echo.

REM Verificar archivos
if not exist "%EXE%" ( echo [ERROR] Falta redbf-agent.exe en esta carpeta & pause & exit /b 1 )
if not exist "%NSSM%" ( echo [ERROR] Falta nssm.exe en esta carpeta & pause & exit /b 1 )
if not exist "%CONFIG%" ( echo [ERROR] Falta config.ini en esta carpeta & pause & exit /b 1 )

echo [1/7] Cerrando programas que bloquean el servicio...
taskkill /F /IM mmc.exe >nul 2>&1
taskkill /F /IM redbf-agent.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/7] Deteniendo y eliminando servicio anterior...
"%NSSM%" stop %SERVICE% >nul 2>&1
sc stop %SERVICE% >nul 2>&1
"%NSSM%" remove %SERVICE% confirm >nul 2>&1
sc delete %SERVICE% >nul 2>&1
echo       Esperando que Windows libere el servicio (8s)...
timeout /t 8 /nobreak >nul

REM Matar de nuevo por si revivio durante la espera
taskkill /F /IM redbf-agent.exe >nul 2>&1

echo [3/7] Exclusion de Windows Defender...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Add-MpPreference -ExclusionPath '%INSTALL_DIR%' -ErrorAction Stop; Add-MpPreference -ExclusionProcess 'redbf-agent.exe' -ErrorAction Stop; Write-Host '      OK' } catch { Write-Host '      [WARN]' $_.Exception.Message }"

echo [4/7] Instalando servicio fresco...
"%NSSM%" install %SERVICE% "%EXE%" >nul
if errorlevel 1 (
    echo       [ERROR] No se pudo crear el servicio. Si dice "marcado para
    echo               eliminacion", REINICIA la PC y ejecuta este .bat de nuevo.
    pause & exit /b 1
)

echo [5/7] Configurando servicio...
"%NSSM%" set %SERVICE% AppDirectory "%INSTALL_DIR%" >nul
"%NSSM%" set %SERVICE% DisplayName "RedBF Agent (Inventario de red)" >nul
"%NSSM%" set %SERVICE% Description "Inventario, monitoreo y control remoto RedBF." >nul
"%NSSM%" set %SERVICE% Start SERVICE_AUTO_START >nul
"%NSSM%" set %SERVICE% ObjectName LocalSystem >nul
"%NSSM%" set %SERVICE% AppStdout "%LOG_FILE%" >nul
"%NSSM%" set %SERVICE% AppStderr "%LOG_FILE%" >nul
"%NSSM%" set %SERVICE% AppRotateFiles 1 >nul
"%NSSM%" set %SERVICE% AppRotateBytes 10485760 >nul
"%NSSM%" set %SERVICE% AppExit Default Restart >nul
"%NSSM%" set %SERVICE% AppRestartDelay 5000 >nul

echo [6/7] Arrancando servicio...
"%NSSM%" start %SERVICE% >nul 2>&1
timeout /t 4 /nobreak >nul

echo [7/7] Verificando...
sc query %SERVICE% | find "RUNNING" >nul && (
    echo.
    echo  ============================================================
    echo   OK - Agente instalado y RUNNING.
    echo   El equipo aparecera en el dashboard en ~1 minuto.
    echo  ============================================================
) || (
    echo.
    echo  [AVISO] El servicio no arranco. REINICIA la PC y ejecuta este
    echo          .bat otra vez (al reiniciar se limpia todo resto).
    sc query %SERVICE% | findstr /i "STATE"
)
echo.
pause
endlocal
