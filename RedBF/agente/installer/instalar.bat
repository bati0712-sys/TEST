@echo off
REM ============================================================
REM  Instalador RedBF Agent - TODO EN UNO (sirve tambien para
REM  REINSTALAR limpio sobre una version anterior).
REM
REM  1. Verifica permisos de Admin
REM  2. Verifica/ubica config.ini (lo reusa si ya esta en la PC)
REM  3. Excluye carpeta y .exe de Windows Defender
REM  4. DESTRABA y elimina el servicio anterior (mata PID colgado
REM     en Stop-Pending si hace falta)
REM  5. Crea el servicio (reintenta si Windows aun lo libera)
REM  6. Configura (anti Stop-Pending) + arranca
REM ============================================================
setlocal EnableDelayedExpansion

set "SERVICE=RedBFAgent"
set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"
set "EXE=%INSTALL_DIR%\redbf-agent.exe"
set "CONFIG=%INSTALL_DIR%\config.ini"
set "NSSM=%INSTALL_DIR%\nssm.exe"
set "LOG_FILE=%INSTALL_DIR%\agente.log"

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

REM === 2. Archivos + config ===
if not exist "%EXE%" ( echo [ERROR] Falta redbf-agent.exe & pause & exit /b 1 )
if not exist "%NSSM%" ( echo [ERROR] Falta nssm.exe & pause & exit /b 1 )
REM Si no hay config.ini en el paquete, reusar el que ya esta instalado en la PC
REM (asi el paquete no necesita llevar el token escrito al reinstalar).
if not exist "%CONFIG%" (
    echo       config.ini no esta en el paquete; buscando en la PC...
    call :buscar_config
)
if not exist "%CONFIG%" (
    echo [ERROR] Falta config.ini. Copia config.ini.example a config.ini y
    echo         edita el token/server_url, o pega tu config.ini en esta carpeta.
    pause & exit /b 1
)
echo [2/6] Archivos y config: OK

REM === 3. Exclusion Defender ===
echo [3/6] Configurando exclusiones de Windows Defender...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Add-MpPreference -ExclusionPath '%INSTALL_DIR%' -ErrorAction Stop; Add-MpPreference -ExclusionProcess 'redbf-agent.exe' -ErrorAction Stop; Write-Host '      Exclusiones agregadas' } catch { Write-Host '      [WARN] No se pudo (Defender off/policy):' $_.Exception.Message }"

REM === 4. Destrabar + quitar servicio + procesos previos ===
echo [4/6] Quitando servicio anterior (destraba si esta colgado)...
"%NSSM%" stop %SERVICE% >nul 2>&1
sc stop %SERVICE% >nul 2>&1
call :destrabar
"%NSSM%" remove %SERVICE% confirm >nul 2>&1
sc delete %SERVICE% >nul 2>&1
call :destrabar
taskkill /F /IM redbf-agent.exe >nul 2>&1
timeout /t 3 /nobreak >nul

REM === 5. Crear servicio (reintenta si Windows aun lo libera) ===
echo [5/6] Creando servicio %SERVICE%...
set /a intentos=0
:crear
"%NSSM%" install %SERVICE% "%EXE%" >nul 2>&1
sc query %SERVICE% >nul 2>&1
if not errorlevel 1 goto creado
set /a intentos+=1
if %intentos% geq 12 (
    echo       [ERROR] Windows no libero el servicio. Reinicia la PC y corre
    echo               este .bat otra vez.
    pause & exit /b 1
)
echo       Servicio aun marcado para eliminacion, reintento %intentos% de 12...
taskkill /F /IM mmc.exe >nul 2>&1
timeout /t 10 /nobreak >nul
goto crear
:creado

REM Configurar el servicio
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
"%NSSM%" start %SERVICE% >nul 2>&1
timeout /t 5 /nobreak >nul

sc query %SERVICE% | find "RUNNING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo ============================================================
    echo  OK - Agente instalado y RUNNING.
    echo  El equipo aparecera en el dashboard RedBF en ~1-2 min.
    echo ============================================================
) else (
    echo.
    echo  [AVISO] El servicio no quedo RUNNING. Reinicia la PC y corre este
    echo          .bat otra vez ^(al reiniciar se limpia todo resto^).
    sc query %SERVICE% | findstr /i "STATE"
)
echo.
pause
exit /b 0


REM ============================================================
REM  Subrutina: ubicar config.ini del agente ya instalado y
REM  copiarlo a la carpeta del paquete (reusa token + server_url).
REM ============================================================
:buscar_config
for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Services\%SERVICE%" /v ImagePath 2^>nul') do set "IMG=%%b"
if defined IMG (
  for %%I in (%IMG%) do set "SVCDIR=%%~dpI"
  if exist "!SVCDIR!config.ini" ( copy /Y "!SVCDIR!config.ini" "%CONFIG%" >nul 2>&1 & goto :eof )
)
if exist "C:\RedBF-Agent\config.ini" ( copy /Y "C:\RedBF-Agent\config.ini" "%CONFIG%" >nul 2>&1 & goto :eof )
if exist "C:\Program Files\RedBF-Agent\config.ini" ( copy /Y "C:\Program Files\RedBF-Agent\config.ini" "%CONFIG%" >nul 2>&1 & goto :eof )
if exist "C:\ProgramData\RedBF-Agent\config.ini" ( copy /Y "C:\ProgramData\RedBF-Agent\config.ini" "%CONFIG%" >nul 2>&1 & goto :eof )
echo       Buscando en el disco C:, espera...
for /f "delims=" %%F in ('dir /b /s "C:\redbf-agent.exe" 2^>nul') do (
  if exist "%%~dpFconfig.ini" ( copy /Y "%%~dpFconfig.ini" "%CONFIG%" >nul 2>&1 & goto :eof )
)
goto :eof


REM ============================================================
REM  Subrutina: destrabar el servicio si quedo colgado en
REM  Stop-Pending/Start-Pending: mata su PID hasta liberarlo.
REM ============================================================
:destrabar
set /a _d=0
:destrabar_loop
sc query %SERVICE% >nul 2>&1
if errorlevel 1 goto :eof
set "SPID="
for /f "tokens=2 delims=:" %%a in ('sc queryex %SERVICE% 2^>nul ^| findstr /i "PID"') do set "SPID=%%a"
if defined SPID set "SPID=%SPID: =%"
if not defined SPID goto :eof
if "%SPID%"=="0" goto :eof
taskkill /F /PID %SPID% >nul 2>&1
timeout /t 2 /nobreak >nul
set /a _d+=1
if %_d% lss 5 goto destrabar_loop
goto :eof
