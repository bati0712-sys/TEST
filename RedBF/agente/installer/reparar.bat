@echo off
REM ============================================================
REM  REPARAR instalacion RedBF Agent
REM  Soluciona el error "servicio marcado para eliminacion".
REM  Ejecutar como Administrador en la carpeta del agente.
REM ============================================================
setlocal
chcp 65001 >nul 2>&1
set SERVICE=RedBFAgent
set INSTALL_DIR=%~dp0
if "%INSTALL_DIR:~-1%"=="\" set INSTALL_DIR=%INSTALL_DIR:~0,-1%
set EXE=%INSTALL_DIR%\redbf-agent.exe
set NSSM=%INSTALL_DIR%\nssm.exe
set LOG_FILE=%INSTALL_DIR%\agente.log

net session >nul 2>&1
if errorlevel 1 ( echo [ERROR] Ejecutar como Administrador & pause & exit /b 1 )

echo.
echo ============================================================
echo  Reparando RedBF Agent...
echo ============================================================
echo.

echo [1/5] Cerrando procesos y visor de servicios que bloquean...
REM El visor de servicios (services.msc / mmc) mantiene el servicio "abierto"
REM y por eso queda "marcado para eliminacion". Lo cerramos.
taskkill /F /IM mmc.exe >nul 2>&1
taskkill /F /IM redbf-agent.exe >nul 2>&1
timeout /t 1 /nobreak >nul

echo [2/5] Forzando eliminacion del servicio zombie...
sc stop %SERVICE% >nul 2>&1
"%NSSM%" remove %SERVICE% confirm >nul 2>&1
sc delete %SERVICE% >nul 2>&1
REM esperar a que Windows libere el nombre del servicio
echo       Esperando que Windows libere el servicio (10s)...
timeout /t 10 /nobreak >nul

echo [3/5] Verificando que el servicio ya no exista...
sc query %SERVICE% >nul 2>&1
if not errorlevel 1 (
    echo       [AVISO] El servicio aun figura. Si esto persiste, REINICIA la PC
    echo       y vuelve a ejecutar instalar.bat. Continuo de todas formas...
    timeout /t 5 /nobreak >nul
)

echo [4/5] Reinstalando servicio...
"%NSSM%" install %SERVICE% "%EXE%" >nul
"%NSSM%" set %SERVICE% AppDirectory "%INSTALL_DIR%" >nul
"%NSSM%" set %SERVICE% DisplayName "RedBF Agent (Inventario de red)" >nul
"%NSSM%" set %SERVICE% Start SERVICE_AUTO_START >nul
"%NSSM%" set %SERVICE% ObjectName LocalSystem >nul
"%NSSM%" set %SERVICE% AppStdout "%LOG_FILE%" >nul
"%NSSM%" set %SERVICE% AppStderr "%LOG_FILE%" >nul
"%NSSM%" set %SERVICE% AppRotateFiles 1 >nul
"%NSSM%" set %SERVICE% AppRotateBytes 10485760 >nul
"%NSSM%" set %SERVICE% AppExit Default Restart >nul

echo [5/5] Arrancando servicio...
sc config %SERVICE% start= auto >nul 2>&1
"%NSSM%" start %SERVICE% >nul 2>&1
sc start %SERVICE% >nul 2>&1
timeout /t 3 /nobreak >nul
sc query %SERVICE% | find "RUNNING" >nul && (
    echo.
    echo  OK - Servicio RUNNING. El equipo aparecera en el dashboard en ~1 min.
) || (
    echo.
    echo  [AVISO] El servicio no quedo RUNNING. Si persiste, REINICIA la PC
    echo          y ejecuta instalar.bat de nuevo.
)
echo.
pause
endlocal
