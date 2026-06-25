@echo off
REM ============================================================
REM  Empaqueta el instalador completo de RedBF Agent.
REM  Junta en installer\: redbf-agent.exe + nssm.exe + config.ini + .bat
REM  y comprime a RedBF-Agent-Installer.zip listo para distribuir.
REM ============================================================
chcp 65001 >nul 2>&1
set HERE=%~dp0
set OUT=%HERE%installer

echo.
echo  Empaquetando instalador RedBF Agent...
echo.

if not exist "%HERE%dist\redbf-agent.exe" (
    echo [ERROR] No existe dist\redbf-agent.exe. Ejecuta build.bat primero.
    pause & exit /b 1
)

REM Copiar el .exe recien compilado al installer
copy /Y "%HERE%dist\redbf-agent.exe" "%OUT%\redbf-agent.exe" >nul

REM Copiar nssm.exe (reusado del appbf_agent si no esta local)
if not exist "%OUT%\nssm.exe" (
    if exist "..\..\AppBF\appbf_agent\installer\nssm.exe" (
        copy /Y "..\..\AppBF\appbf_agent\installer\nssm.exe" "%OUT%\nssm.exe" >nul
        echo  nssm.exe copiado desde appbf_agent
    ) else (
        echo  [WARN] Falta nssm.exe en installer\. Copialo manualmente.
    )
)

REM Asegurar config.ini (desde el example si no existe)
if not exist "%OUT%\config.ini" copy /Y "%OUT%\config.ini.example" "%OUT%\config.ini" >nul

REM Comprimir
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%\*' -DestinationPath '%HERE%RedBF-Agent-Installer.zip' -Force"

echo.
echo ============================================================
echo  Listo: RedBF-Agent-Installer.zip
echo ============================================================
echo  Contenido del instalador (carpeta installer\):
dir /b "%OUT%"
echo.
echo  Para instalar en una PC:
echo    1. Copiar y descomprimir el ZIP
echo    2. Editar config.ini (server_url = IP del servidor RedBF)
echo    3. Click derecho en instalar.bat ^> Ejecutar como administrador
echo.
pause
