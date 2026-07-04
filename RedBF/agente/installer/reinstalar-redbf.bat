@echo off
REM ============================================================
REM  REINSTALACION LIMPIA del RedBF Agent -> v0.1.8
REM  Elimina el servicio NSSM viejo (que se cuelga) y lo instala
REM  fresco. Reusa el config.ini que YA esta en la PC (con su token),
REM  asi este paquete NO lleva el token escrito.
REM  Ejecutar como ADMINISTRADOR, en la carpeta del ZIP descomprimido.
REM ============================================================
setlocal EnableDelayedExpansion

set SERVICE=RedBFAgent
set INSTALL_DIR=%~dp0
if "%INSTALL_DIR:~-1%"=="\" set INSTALL_DIR=%INSTALL_DIR:~0,-1%
set EXE=%INSTALL_DIR%\redbf-agent.exe
set NSSM=%INSTALL_DIR%\nssm.exe
set CONFIG=%INSTALL_DIR%\config.ini
set LOG_FILE=%INSTALL_DIR%\agente.log

net session >nul 2>&1
if errorlevel 1 ( echo [ERROR] Ejecutar como Administrador ^(click derecho^) & pause & exit /b 1 )

echo.
echo ============================================================
echo  REINSTALACION LIMPIA - RedBF Agent v0.1.8
echo  Carpeta: %INSTALL_DIR%
echo ============================================================
echo.

if not exist "%EXE%"  ( echo [ERROR] Falta redbf-agent.exe en esta carpeta & pause & exit /b 1 )
if not exist "%NSSM%" ( echo [ERROR] Falta nssm.exe en esta carpeta & pause & exit /b 1 )

REM === Conseguir el config.ini con el token: si no viene en el ZIP, copiarlo
REM     desde la instalacion actual de la PC (asi no llevamos el token escrito) ===
if not exist "%CONFIG%" (
  echo [*] Buscando config.ini existente en la PC...
  set FOUND=
  for %%P in (
    "C:\RedBF-Agent\config.ini"
    "C:\Program Files\RedBF-Agent\config.ini"
    "C:\Program Files (x86)\RedBF-Agent\config.ini"
    "C:\ProgramData\RedBF-Agent\config.ini"
  ) do (
    if exist "%%~P" if not defined FOUND (
      copy /Y "%%~P" "%CONFIG%" >nul 2>&1
      set FOUND=%%~P
    )
  )
  if defined FOUND (
    echo     Reusando config de: !FOUND!
  ) else (
    echo [ERROR] No encontre config.ini en la PC. Pega tu config.ini en esta
    echo         carpeta junto a este .bat y vuelve a ejecutar.
    pause & exit /b 1
  )
)

echo [1/7] Cerrando procesos que bloquean el servicio...
taskkill /F /IM redbf-agent.exe >nul 2>&1
taskkill /F /IM mmc.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/7] Deteniendo y ELIMINANDO el servicio viejo (causa del cuelgue)...
"%NSSM%" stop %SERVICE% >nul 2>&1
sc stop %SERVICE% >nul 2>&1
"%NSSM%" remove %SERVICE% confirm >nul 2>&1
sc delete %SERVICE% >nul 2>&1
echo       Esperando que Windows libere el servicio (8s)...
timeout /t 8 /nobreak >nul
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
    echo   OK - Agente v0.1.8 instalado y RUNNING.
    echo   El equipo aparecera actualizado en el dashboard en ~1-2 min.
    echo  ============================================================
) || (
    echo.
    echo  [AVISO] El servicio no arranco. REINICIA la PC y ejecuta este
    echo          .bat otra vez ^(al reiniciar se limpia todo resto^).
    sc query %SERVICE% | findstr /i "STATE"
)
echo.
pause
endlocal
