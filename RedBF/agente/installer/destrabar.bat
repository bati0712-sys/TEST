@echo off
REM ============================================================
REM  DESTRABAR servicio RedBF Agent atascado en "Stop Pending"
REM  (mensaje "El servicio se esta iniciando o deteniendo").
REM  Ejecutar como Administrador. NO requiere reiniciar la PC.
REM ============================================================
chcp 65001 >nul 2>&1
set SERVICE=RedBFAgent

net session >nul 2>&1
if errorlevel 1 ( echo [ERROR] Ejecutar como Administrador & pause & exit /b 1 )

echo.
echo ============================================================
echo  Destrabando %SERVICE%...
echo ============================================================
echo.

echo [1] Obteniendo PID del proceso colgado...
for /f "tokens=2 delims=:" %%a in ('sc queryex %SERVICE% ^| findstr /i "PID"') do set PID=%%a
set PID=%PID: =%
echo     PID = %PID%

echo [2] Matando el proceso del agente (por PID y por nombre)...
if not "%PID%"=="0" if not "%PID%"=="" taskkill /F /PID %PID% >nul 2>&1
taskkill /F /IM redbf-agent.exe >nul 2>&1
timeout /t 3 /nobreak >nul

echo [3] Verificando estado del servicio...
sc query %SERVICE% | findstr /i "STATE"

echo [4] Intentando arrancar el servicio...
sc start %SERVICE% >nul 2>&1
net start %SERVICE% >nul 2>&1
timeout /t 3 /nobreak >nul

sc query %SERVICE% | find "RUNNING" >nul && (
    echo.
    echo  ============================================
    echo   OK - Servicio RUNNING. El agente revivio.
    echo  ============================================
) || (
    echo.
    echo  [AVISO] Aun no arranca. El estado "Stop Pending" del
    echo          Administrador de Servicios a veces solo se limpia
    echo          reiniciando la PC. Si esto persiste, reinicia.
    sc query %SERVICE% | findstr /i "STATE"
)
echo.
pause
