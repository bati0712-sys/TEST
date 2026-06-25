@echo off
REM Desinstala el servicio RedBF Agent
chcp 65001 >nul 2>&1
set SERVICE=RedBFAgent
set INSTALL_DIR=%~dp0
if "%INSTALL_DIR:~-1%"=="\" set INSTALL_DIR=%INSTALL_DIR:~0,-1%
set NSSM=%INSTALL_DIR%\nssm.exe

net session >nul 2>&1
if errorlevel 1 ( echo [ERROR] Ejecutar como Administrador & pause & exit /b 1 )

echo Deteniendo y removiendo servicio %SERVICE%...
"%NSSM%" stop %SERVICE% >nul 2>&1
"%NSSM%" remove %SERVICE% confirm >nul 2>&1

REM Quitar exclusiones de Defender
powershell -NoProfile -Command "try { Remove-MpPreference -ExclusionPath '%INSTALL_DIR%' -ErrorAction SilentlyContinue; Remove-MpPreference -ExclusionProcess 'redbf-agent.exe' -ErrorAction SilentlyContinue } catch {}"

echo Servicio removido.
pause
