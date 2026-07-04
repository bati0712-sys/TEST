"""RedBF Agent — corre en cada PC de la red.

- Reporta inventario al servidor central cada N minutos (heartbeat).
- Recibe y ejecuta comandos (Fase 3): ejecutar proceso, reiniciar servicio, etc.

Config en config.ini (junto al .exe). Uso:
    python agente.py
    redbf-agent.exe
"""
from __future__ import annotations
import configparser
import json
import logging
import os
import sys
import time
import socket
from pathlib import Path

import urllib.request
import urllib.error

from collectors import inventory

log = logging.getLogger("redbf_agent")
__version__ = "0.1.11"  # feat: transferencia de archivos (RECIBIR/ENVIAR_ARCHIVO/LISTAR_CARPETA, límite 10MB). Incluye portapapeles v0.1.10 + dedup helpers v0.1.9 + fix Session 0 v0.1.8


def _base_dir() -> Path:
    # Soporta correr como .exe (PyInstaller) o como script
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def cargar_config() -> dict:
    cfg = configparser.ConfigParser()
    path = _base_dir() / "config.ini"
    # utf-8-sig tolera el BOM que algunos editores/PowerShell agregan al inicio
    # (sin esto, configparser falla con MissingSectionHeaderError ante '﻿[redbf]').
    cfg.read(path, encoding="utf-8-sig")
    s = cfg["redbf"] if "redbf" in cfg else {}
    return {
        "server_url": s.get("server_url", "http://192.168.1.71:8077").rstrip("/"),
        # Prefijo de la API: "" para servidor standalone (puerto 8077),
        # "/api/redbf" para el módulo integrado en AppBF.
        "api_prefix": s.get("api_prefix", "/api").rstrip("/"),
        "token": s.get("token", "redbf-lan-2026"),
        "intervalo_min": int(s.get("intervalo_min", "10")),
        "poll_comandos_s": int(s.get("poll_comandos_s", "4")),
        "incluir_software": s.get("incluir_software", "true").lower() == "true",
        "incluir_navegacion": s.get("incluir_navegacion", "true").lower() == "true",
        # Validar el certificado TLS del control remoto (anti-MITM). Por defecto
        # SÍ; solo poner false en LAN con cert autofirmado.
        "ssl_verify": s.get("ssl_verify", "true").lower() == "true",
    }


def _post(url: str, payload: dict, token: str, timeout: int = 60) -> dict:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-RedBF-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Auto-actualización remota ──────────────────────────────────────
def _auto_update() -> tuple[str, dict]:
    """Descarga el .exe nuevo del servidor y se auto-reemplaza.

    Un .exe corriendo no puede sobrescribirse a sí mismo → descarga el binario
    nuevo a un .new, y lanza un .bat INDEPENDIENTE que:
      1. espera a que el servicio pare,
      2. reemplaza el .exe,
      3. reinicia el servicio.
    Solo aplica cuando corre empaquetado (.exe frozen) como servicio.
    """
    if not getattr(sys, "frozen", False):
        return "ERROR", {"error": "auto-update solo aplica al .exe empaquetado"}

    conf = cargar_config()
    exe_actual = sys.executable                       # ...\redbf-agent.exe
    carpeta = os.path.dirname(exe_actual)
    exe_new = exe_actual + ".new"
    bat = os.path.join(carpeta, "_redbf_update.bat")

    # 1. Descargar el binario nuevo
    try:
        req = urllib.request.Request(f"{conf['server_url']}{conf['api_prefix']}/agente/binario")
        req.add_header("X-RedBF-Token", conf["token"])
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        if len(data) < 1_000_000:  # sanity: el .exe pesa decenas de MB
            return "ERROR", {"error": f"binario descargado muy chico ({len(data)} bytes)"}
        with open(exe_new, "wb") as f:
            f.write(data)
    except Exception as e:
        return "ERROR", {"error": f"descarga falló: {e}"}

    # 2. Script de reemplazo robusto. Diseño tras varias iteraciones de Stop-Pending:
    #    - El agente instala un handler de señales (ver _instalar_handlers_senal) que
    #      lo hace salir LIMPIO (código 0) cuando NSSM le manda stop. Eso evita la
    #      KeyboardInterrupt que dejaba a NSSM en Stop-Pending.
    #    - El updater corre como TAREA PROGRAMADA (SYSTEM), fuera del árbol del
    #      servicio, así sobrevive aunque NSSM mate el proceso del agente.
    #    - NSSM con AppStopMethodSkip 6 + AppKillProcessTree 1 garantiza un stop que
    #      no se cuelga aunque el handler fallara.
    #    - El move del .exe reintenta hasta que el binario se libere.
    servicio = "RedBFAgent"
    nssm = os.path.join(carpeta, "nssm.exe")
    usa_nssm = os.path.exists(nssm)

    if usa_nssm:
        cfg_stop = (f'"{nssm}" set {servicio} AppStopMethodSkip 6 >nul 2>&1\n'
                    f'"{nssm}" set {servicio} AppKillProcessTree 1 >nul 2>&1\n'
                    f'"{nssm}" set {servicio} AppStopMethodConsole 1500 >nul 2>&1\n')
        stop_cmd = f'"{nssm}" stop {servicio}'
        start_cmd = f'"{nssm}" start {servicio}'
    else:
        cfg_stop = ""
        stop_cmd = f"sc stop {servicio}"
        start_cmd = f"sc start {servicio}"

    log_upd = os.path.join(carpeta, "_redbf_update.log")
    bat_content = f"""@echo off
REM Auto-update RedBF Agent. Corre como tarea programada SYSTEM (fuera del arbol
REM del servicio). El agente sale limpio con su handler de senales.
REM Escribe resultado en _redbf_update.log para que el agente nuevo lo reporte.
echo [%date% %time%] inicio update > "{log_upd}"
{cfg_stop}REM 1) detener el servicio
{stop_cmd} >nul 2>&1
ping -n 3 127.0.0.1 >nul 2>&1
REM 2) esperar a que el proceso muera de verdad (poll real, no solo sleep).
REM    En PCs lentas el exe sigue bloqueado un rato tras el stop -> el move falla.
set /a waitp=0
:killloop
taskkill /F /IM redbf-agent.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul 2>&1
tasklist /FI "IMAGENAME eq redbf-agent.exe" 2>nul | find /I "redbf-agent.exe" >nul 2>&1
if not errorlevel 1 (
    set /a waitp+=1
    if %waitp% lss 30 goto killloop
)
REM 3) reemplazar el binario (reintenta hasta 40 veces ~80s)
set /a tries=0
:wait
move /Y "{exe_new}" "{exe_actual}" >nul 2>&1
if not errorlevel 1 goto moved
set /a tries+=1
if %tries% geq 40 goto movefail
ping -n 3 127.0.0.1 >nul 2>&1
goto wait
:movefail
REM El swap NO se logro (exe bloqueado). NO arrancar con el viejo: registrar fallo,
REM dejar el .new para el proximo intento y arrancar el servicio viejo (sigue vivo).
echo [%date% %time%] ERROR move fallo tras 40 intentos, exe sin reemplazar >> "{log_upd}"
{start_cmd} >nul 2>&1
goto fin
:moved
echo [%date% %time%] OK exe reemplazado >> "{log_upd}"
REM 4) arrancar; si queda en pending tras 5s, forzar otro ciclo
{start_cmd} >nul 2>&1
ping -n 6 127.0.0.1 >nul 2>&1
sc query {servicio} | find "RUNNING" >nul 2>&1
if errorlevel 1 (
    echo [%date% %time%] reintento start >> "{log_upd}"
    {stop_cmd} >nul 2>&1
    ping -n 3 127.0.0.1 >nul 2>&1
    {start_cmd} >nul 2>&1
)
:fin
del "{exe_new}" >nul 2>&1
schtasks /Delete /F /TN RedBF_AutoUpdate >nul 2>&1
del "%~f0" >nul 2>&1
"""
    try:
        with open(bat, "w") as f:
            f.write(bat_content)
        import subprocess
        # CLAVE: el updater NO puede ser hijo del agente. Con AppKillProcessTree=1,
        # `nssm stop` mata todo el árbol del servicio — incluido un .bat hijo, que
        # entonces muere a mitad y deja el swap sin terminar (Stop-Pending).
        # Solución: ejecutarlo como TAREA PROGRAMADA de Windows. Task Scheduler corre
        # la tarea en su propio contexto (svchost), fuera del árbol del servicio, así
        # sobrevive a que NSSM mate al agente.
        tarea = "RedBF_AutoUpdate"
        # /sc ONCE a "ahora+1min" no sirve (granularidad de minuto); mejor crear la
        # tarea y dispararla de inmediato con /run. La tarea se borra a sí misma al final.
        subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", tarea,
             "/TR", f'cmd /c "{bat}"', "/SC", "ONCE", "/ST", "00:00",
             "/RU", "SYSTEM", "/RL", "HIGHEST"],
            capture_output=True, timeout=30,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        subprocess.run(["schtasks", "/Run", "/TN", tarea],
                       capture_output=True, timeout=30,
                       creationflags=0x08000000)
    except Exception as e:
        return "ERROR", {"error": f"no pude lanzar el updater: {e}"}

    return "OK", {"mensaje": f"actualización iniciada ({len(data)} bytes), el servicio se reiniciará en segundos"}


# ── Notificación toast en la PC del usuario ────────────────────────
def _notificar(titulo: str, mensaje: str) -> tuple[str, dict]:
    """Muestra un toast nativo de Windows en la sesión del usuario.
    Como el agente está en Session 0, lanza el modo --toast en la sesión
    interactiva (helper). Si corre interactivo, lo muestra directo."""
    # codificar argumentos para pasarlos por la línea de comando del helper
    import base64, tempfile, time, uuid
    t_b64 = base64.b64encode(titulo.encode("utf-8")).decode("ascii")
    m_b64 = base64.b64encode(mensaje.encode("utf-8")).decode("ascii")

    # Archivo testigo: el helper escribe "1" si el usuario vio el aviso, "0" si
    # corrió pero no había escritorio interactivo visible. Generamos la ruta
    # absoluta aquí (en Session 0 gettempdir => C:\Windows\Temp) y se la pasamos
    # al helper, que escribe en esa MISMA ruta. Es el patrón ya probado por la
    # captura de pantalla (pantalla.py _captura_via_helper).
    testigo = os.path.join(tempfile.gettempdir(), f"_redbf_notify_{uuid.uuid4().hex}.flag")
    try:
        if os.path.exists(testigo):
            os.remove(testigo)
    except OSError:
        pass

    if getattr(sys, "frozen", False):
        cmdline = f'"{sys.executable}" --toast "{t_b64}" "{m_b64}" "{testigo}"'
    else:
        agente_py = os.path.abspath(__file__)
        cmdline = f'"{sys.executable}" "{agente_py}" --toast "{t_b64}" "{m_b64}" "{testigo}"'

    def _leer_testigo(espera_s: float = 12.0) -> str | None:
        """Espera a que el helper escriba el testigo. Devuelve '1'/'0' o None si
        nunca apareció (helper murió antes de pintar)."""
        deadline = time.time() + espera_s
        while time.time() < deadline:
            try:
                with open(testigo, "r", encoding="ascii") as _f:
                    val = _f.read().strip()
                if val:
                    return val
            except OSError:
                pass
            time.sleep(0.4)
        return None

    try:
        from collectors import session_helper
        if session_helper.en_session0():
            # Usar el MISMO mecanismo que el control (que SÍ funciona): lanzar en la
            # sesión del usuario SIN esperar y sin CREATE_NO_WINDOW. El antiguo
            # ejecutar_en_sesion_usuario esperaba con WaitForSingleObject + NO_WINDOW,
            # lo que impedía que el MessageBox se viera.
            ok = _lanzar_en_sesion_async(cmdline)
            if not ok:
                return "ERROR", {"error": "no hay sesión de usuario activa para mostrar la notificación"}
            # No basta con lanzar el helper: confirmamos por el testigo que el
            # aviso se VIO de verdad (antes esto reportaba OK falso aunque el
            # usuario no viera nada — p. ej. sesión bloqueada/sin login).
            val = _leer_testigo()
            if val == "1":
                return "OK", {"mensaje": "notificación mostrada al usuario"}
            if val == "0":
                return "ERROR", {"error": "sesión sin escritorio visible (bloqueada o sin usuario logueado); el aviso no se mostró"}
            return "ERROR", {"error": "no se pudo confirmar que el aviso se mostró (helper sin respuesta)"}
        else:
            from collectors import notificar
            visto = notificar.mostrar_toast(titulo, mensaje)
            if visto:
                return "OK", {"mensaje": "notificación mostrada (interactivo)"}
            return "ERROR", {"error": "sin escritorio visible; el aviso no se mostró"}
    except Exception as e:
        return "ERROR", {"error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            if os.path.exists(testigo):
                os.remove(testigo)
        except OSError:
            pass


# ── Control remoto en vivo ─────────────────────────────────────────
def _matar_controles_previos(esperar: bool = True):
    """Mata cualquier helper de control (`redbf-agent.exe --control`) que siga vivo,
    para no acumular varios inyectando input a la vez. NO toca el servicio principal
    (que no lleva --control en su línea de comando) ni el proceso actual.

    Usa WMI vía PowerShell para filtrar por CommandLine (tasklist no muestra args).
    Si `esperar`, verifica en un bucle corto que realmente murieron antes de volver
    (evita la carrera: si el dashboard reenvía INICIAR_CONTROL rápido, varios
    helpers arrancan antes de que el Stop-Process surta efecto → pelean por el
    mouse). Best-effort: si algo falla, no aborta el inicio del control."""
    import subprocess
    mi_pid = os.getpid()
    ps_kill = (
        "$ps = Get-CimInstance Win32_Process -Filter \"Name='redbf-agent.exe'\" | "
        "Where-Object { $_.CommandLine -like '*--control*' -and "
        f"$_.ProcessId -ne {mi_pid} }}; "
        "$ps | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
        "($ps | Measure-Object).Count"
    )
    def _run(ps):
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, text=True, timeout=15,
                               creationflags=0x08000000)  # CREATE_NO_WINDOW
            return (r.stdout or "").strip()
        except Exception:
            return ""
    _run(ps_kill)
    if not esperar:
        return
    # Confirmar que ya no queda ninguno (hasta ~3s). Cada vuelta re-mata por si
    # arrancó otro en la carrera.
    ps_count = (
        "(Get-CimInstance Win32_Process -Filter \"Name='redbf-agent.exe'\" | "
        "Where-Object { $_.CommandLine -like '*--control*' -and "
        f"$_.ProcessId -ne {mi_pid} }} | Measure-Object).Count"
    )
    for _ in range(6):
        c = _run(ps_count)
        if c == "0" or c == "":
            return
        time.sleep(0.5)
        _run(ps_kill)


def _iniciar_control(p: dict) -> tuple[str, dict]:
    """Lanza el módulo de control remoto EN LA SESIÓN DEL USUARIO.

    El control necesita capturar pantalla e inyectar mouse/teclado, lo que un
    servicio en Session 0 no puede hacer directamente → se lanza en la sesión
    interactiva con el helper (CreateProcessAsUser). El proceso de control corre
    desligado y mantiene su propio WebSocket al servidor."""
    import socket as _s
    import urllib.parse as _up
    conf = cargar_config()
    hostname = _s.gethostname()
    # URL COMPLETA del WS de agente: ws(s)://server{prefix}/ws/agente/{host}?token=...
    base = conf["server_url"].replace("http://", "ws://").replace("https://", "wss://")
    full_ws = (f"{base}{conf['api_prefix']}/ws/agente/{_up.quote(hostname)}"
               f"?token={_up.quote(conf['token'])}")

    # Modo de acceso + operador para el handshake de consentimiento.
    modo = (p.get("modo") or "consentimiento")
    operador = (p.get("operador") or "Sistemas")
    # sanitizar para la línea de comando (sin comillas que rompan el cmdline)
    modo = "".join(ch for ch in modo if ch.isalnum())[:20] or "consentimiento"
    operador = operador.replace('"', "").replace("'", "")[:40] or "Sistemas"
    sslv = "1" if conf.get("ssl_verify", True) else "0"

    if getattr(sys, "frozen", False):
        cmdline = f'"{sys.executable}" --control "{full_ws}" "{hostname}" "{modo}" "{operador}" "{sslv}"'
    else:
        agente_py = os.path.abspath(__file__)
        cmdline = f'"{sys.executable}" "{agente_py}" --control "{full_ws}" "{hostname}" "{modo}" "{operador}" "{sslv}"'

    # DEDUP por tiempo: dos INICIAR_CONTROL casi simultáneos (el mismo comando
    # tomado por poll+ciclo solapados, o el dashboard reenviando) lanzaban 2 helpers
    # a <1s de diferencia → matar-previos de cada uno corría ANTES de que el otro
    # fuera visible en WMI → 2 helpers peleando por el mouse. Un lock con timestamp
    # ignora cualquier segundo inicio dentro de una ventana corta.
    import tempfile as _tf
    lock = os.path.join(_tf.gettempdir(), "_redbf_control.lock")
    ahora = time.time()
    try:
        if os.path.exists(lock):
            edad = ahora - os.path.getmtime(lock)
            if edad < 8.0:
                log.info(f"[control] INICIAR_CONTROL duplicado ignorado "
                         f"(hace {edad:.1f}s se lanzó otro)")
                return "OK", {"mensaje": "control ya iniciándose (dedup)", "ws": full_ws}
    except Exception:
        pass
    try:
        with open(lock, "w") as _f:
            _f.write(str(ahora))
    except Exception:
        pass

    # Evitar helpers de control DUPLICADOS: si ya hay uno corriendo (p.ej. el
    # dashboard reenvió INICIAR_CONTROL, o quedó un zombi de una sesión previa),
    # varios helpers inyectarían SendInput a la vez y pelearían por el mouse →
    # "se ve pero no se controla". Matamos cualquier helper --control previo antes
    # de lanzar el nuevo, así siempre hay como máximo uno.
    _matar_controles_previos()

    try:
        from collectors import session_helper
        if session_helper.en_session0():
            # lanzar en la sesión del usuario, SIN esperar (corre en background)
            ok = _lanzar_en_sesion_async(cmdline)
            if not ok:
                return "ERROR", {"error": "no hay sesión de usuario activa para el control"}
            return "OK", {"mensaje": "control iniciado en sesión de usuario", "ws": full_ws}
        else:
            # corriendo interactivo (pruebas): lanzar directo desligado
            import subprocess
            subprocess.Popen(cmdline, creationflags=0x00000008)
            return "OK", {"mensaje": "control iniciado (interactivo)", "ws": full_ws}
    except Exception as e:
        return "ERROR", {"error": f"{type(e).__name__}: {e}"}


def _lanzar_en_sesion_async(cmdline: str) -> bool:
    """Como session_helper.ejecutar_en_sesion_usuario pero SIN esperar (el control
    es un proceso de larga duración).

    BUG HISTÓRICO (v<=0.1.7): las funciones Win32 se llamaban SIN declarar
    argtypes/restype. En Windows 64-bit, ctypes asume c_int (32 bits) para cada
    argumento → los HANDLE (punteros de 64 bits) se TRUNCABAN → SetTokenInformation
    fallaba silenciosamente y el proceso caía en Session 0 (captura falla, mouse no
    responde). Fix: declarar argtypes/restype para que los HANDLE pasen enteros +
    verificar SetTokenInformation + loggear la sesión final del proceso.
    """
    from collectors import session_helper as sh
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    adv = ctypes.windll.advapi32
    wts = ctypes.windll.wtsapi32
    uenv = ctypes.windll.userenv

    # ── Declarar firmas (CRÍTICO en 64-bit para no truncar los HANDLE) ──
    wts.WTSQueryUserToken.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    wts.WTSQueryUserToken.restype = wintypes.BOOL
    adv.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                     ctypes.c_int, ctypes.c_int,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.DuplicateTokenEx.restype = wintypes.BOOL
    adv.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD]
    adv.SetTokenInformation.restype = wintypes.BOOL
    uenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                            wintypes.HANDLE, wintypes.BOOL]
    uenv.CreateEnvironmentBlock.restype = wintypes.BOOL
    adv.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
        wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
    adv.CreateProcessAsUserW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k32.ProcessIdToSessionId.restype = wintypes.BOOL

    sid = sh._sesion_activa()
    if sid is None:
        log.warning("[control] no hay sesión interactiva activa (nadie logueado)")
        return False
    user_token = wintypes.HANDLE()
    if not wts.WTSQueryUserToken(sid, ctypes.byref(user_token)):
        log.warning(f"[control] WTSQueryUserToken falló (sesión {sid}, err={k32.GetLastError()})")
        return False
    dup = wintypes.HANDLE()
    ok = adv.DuplicateTokenEx(
        user_token, sh.TOKEN_ALL_FOR_PROCESS, None, sh.SecurityImpersonation,
        sh.TokenPrimary, ctypes.byref(dup))
    k32.CloseHandle(user_token)
    if not ok:
        log.warning(f"[control] DuplicateTokenEx falló (err={k32.GetLastError()})")
        return False
    # CRÍTICO: forzar el SessionId del token a la sesión interactiva. Sin esto, el
    # token duplicado queda en Session 0 y CreateProcessAsUser lanza el proceso en
    # Session 0 (sin escritorio del usuario) → captura falla y NO hay input.
    TokenSessionId = 12
    sid_dword = wintypes.DWORD(sid)
    if not adv.SetTokenInformation(dup, TokenSessionId, ctypes.byref(sid_dword),
                                   ctypes.sizeof(sid_dword)):
        # ya no debería fallar con argtypes correctos; si falla, logueamos y seguimos
        log.warning(f"[control] SetTokenInformation(SessionId={sid}) falló "
                    f"(err={k32.GetLastError()}) — el proceso podría caer en Session 0")
    env = ctypes.c_void_p()
    uenv.CreateEnvironmentBlock(ctypes.byref(env), dup, False)
    si = sh.STARTUPINFO(); si.cb = ctypes.sizeof(sh.STARTUPINFO)
    si.lpDesktop = "winsta0\\default"
    pi = sh.PROCESS_INFORMATION()
    flags = sh.CREATE_UNICODE_ENVIRONMENT | sh.CREATE_NO_WINDOW | sh.NORMAL_PRIORITY_CLASS
    created = adv.CreateProcessAsUserW(
        dup, None, ctypes.c_wchar_p(cmdline), None, None, False,
        flags, env, None, ctypes.byref(si), ctypes.byref(pi))
    if created:
        # Diagnóstico: confirmar en qué sesión quedó realmente el proceso.
        try:
            psid = wintypes.DWORD()
            if k32.ProcessIdToSessionId(pi.dwProcessId, ctypes.byref(psid)):
                nivel = log.info if psid.value == sid else log.warning
                nivel(f"[control] helper lanzado PID={pi.dwProcessId} en Session "
                      f"{psid.value} (objetivo={sid})")
        except Exception:
            pass
        k32.CloseHandle(pi.hProcess)
        k32.CloseHandle(pi.hThread)
    else:
        log.warning(f"[control] CreateProcessAsUserW falló (err={k32.GetLastError()})")
    if env:
        uenv.DestroyEnvironmentBlock(env)
    k32.CloseHandle(dup)
    return bool(created)


# ── Transferencia de archivos ──────────────────────────────────────
_MAX_ARCHIVO = 10 * 1024 * 1024  # 10 MB (límite acorde a la cola de comandos)


def _recibir_archivo(p: dict) -> tuple[str, dict]:
    """Escribe un archivo enviado desde el dashboard. p = {ruta, contenido_b64}.
    ruta puede ser una carpeta destino o un path completo."""
    import base64
    ruta = p.get("ruta", "")
    b64 = p.get("contenido_b64", "")
    nombre = p.get("nombre", "")
    if not ruta:
        return "ERROR", {"error": "falta la ruta destino"}
    try:
        data = base64.b64decode(b64)
    except Exception as e:
        return "ERROR", {"error": f"base64 inválido: {e}"}
    if len(data) > _MAX_ARCHIVO:
        return "ERROR", {"error": f"archivo muy grande ({len(data)} bytes, máx {_MAX_ARCHIVO})"}
    # si ruta es una carpeta existente y hay nombre, componer el path
    if nombre and (os.path.isdir(ruta) or ruta.endswith(("\\", "/"))):
        destino = os.path.join(ruta, nombre)
    else:
        destino = ruta
    try:
        os.makedirs(os.path.dirname(destino) or ".", exist_ok=True)
        with open(destino, "wb") as f:
            f.write(data)
        return "OK", {"destino": destino, "bytes": len(data)}
    except Exception as e:
        return "ERROR", {"error": f"no pude escribir {destino}: {e}"}


def _enviar_archivo(p: dict) -> tuple[str, dict]:
    """Lee un archivo de la PC y lo devuelve en base64. p = {ruta}."""
    import base64
    ruta = p.get("ruta", "")
    if not ruta or not os.path.isfile(ruta):
        return "ERROR", {"error": f"no existe el archivo: {ruta}"}
    try:
        tam = os.path.getsize(ruta)
        if tam > _MAX_ARCHIVO:
            return "ERROR", {"error": f"archivo muy grande ({tam} bytes, máx {_MAX_ARCHIVO}). "
                                      f"Usá control remoto o comprimilo."}
        with open(ruta, "rb") as f:
            data = f.read()
        return "OK", {"nombre": os.path.basename(ruta), "bytes": tam,
                      "contenido_b64": base64.b64encode(data).decode("ascii")}
    except Exception as e:
        return "ERROR", {"error": f"no pude leer {ruta}: {e}"}


def _listar_carpeta(p: dict) -> tuple[str, dict]:
    """Lista archivos/carpetas de un directorio. p = {ruta} (default: unidades)."""
    ruta = p.get("ruta", "")
    try:
        if not ruta:
            # listar unidades disponibles
            import string
            drives = [f"{d}:\\" for d in string.ascii_uppercase
                      if os.path.exists(f"{d}:\\")]
            return "OK", {"ruta": "", "items": [{"nombre": d, "tipo": "dir"} for d in drives]}
        if not os.path.isdir(ruta):
            return "ERROR", {"error": f"no es una carpeta: {ruta}"}
        items = []
        for n in sorted(os.listdir(ruta)):
            full = os.path.join(ruta, n)
            try:
                es_dir = os.path.isdir(full)
                items.append({"nombre": n, "tipo": "dir" if es_dir else "file",
                              "bytes": 0 if es_dir else os.path.getsize(full)})
            except Exception:
                pass
        # padre para navegar hacia arriba
        padre = os.path.dirname(ruta.rstrip("\\/")) if ruta.rstrip("\\/") else ""
        return "OK", {"ruta": ruta, "padre": padre, "items": items[:500]}
    except Exception as e:
        return "ERROR", {"error": f"{type(e).__name__}: {e}"}


# ── Ejecución de comandos (Fase 3) ─────────────────────────────────
def ejecutar_comando(cmd: dict) -> tuple[str, dict]:
    """Ejecuta un comando recibido del servidor. Retorna (estado, resultado)."""
    import subprocess
    tipo = cmd.get("tipo")
    p = cmd.get("parametros") or {}
    try:
        if tipo == "EXEC":
            # ejecutar comando shell
            out = subprocess.run(
                p.get("comando", ""), shell=True, capture_output=True, text=True,
                timeout=p.get("timeout", 120), encoding="utf-8", errors="replace",
            )
            return "OK", {"stdout": out.stdout[-4000:], "stderr": out.stderr[-2000:], "rc": out.returncode}
        elif tipo == "REINICIAR_SERVICIO":
            svc = p.get("servicio", "")
            subprocess.run(["net", "stop", svc], capture_output=True, timeout=60)
            r = subprocess.run(["net", "start", svc], capture_output=True, text=True, timeout=60)
            return "OK", {"servicio": svc, "salida": r.stdout}
        elif tipo == "PING":
            return "OK", {"pong": True, "hostname": socket.gethostname()}
        elif tipo == "DIAGNOSTICO":
            # Evaluación de rendimiento bajo demanda (CPU/RAM/disco/procesos + veredicto)
            from collectors import perf
            return "OK", perf.evaluar()
        elif tipo == "CAPTURA_PANTALLA":
            from collectors import pantalla
            r = pantalla.capturar(calidad=p.get("calidad", 55))
            if not r.get("ok"):
                return "ERROR", {"error": r.get("error", "captura falló"), "session0": r.get("session0")}
            return "OK", {"formato": r["formato"], "base64": r["base64"],
                          "bytes": r["bytes"], "session0": r["session0"]}
        elif tipo == "AUTO_UPDATE":
            return _auto_update()
        elif tipo == "INICIAR_CONTROL":
            return _iniciar_control(p)
        elif tipo == "NOTIFICAR":
            return _notificar(p.get("titulo", "RedBF"), p.get("mensaje", ""))
        elif tipo == "RECIBIR_ARCHIVO":
            return _recibir_archivo(p)
        elif tipo == "ENVIAR_ARCHIVO":
            return _enviar_archivo(p)
        elif tipo == "LISTAR_CARPETA":
            return _listar_carpeta(p)
        else:
            return "ERROR", {"error": f"tipo desconocido: {tipo}"}
    except Exception as e:
        return "ERROR", {"error": f"{type(e).__name__}: {e}"}


def ciclo(conf: dict):
    """Un ciclo: recolecta inventario, reporta, ejecuta comandos pendientes."""
    inv = inventory.recolectar(
        incluir_software=conf["incluir_software"],
        incluir_navegacion=conf["incluir_navegacion"],
    )
    inv["agente_version"] = __version__
    resp = _post(f"{conf['server_url']}{conf['api_prefix']}/agente/reportar",
                 {"inventario": inv}, conf["token"])
    _ejecutar_comandos(conf, resp.get("comandos", []))


def _ejecutar_comandos(conf: dict, comandos: list):
    if comandos:
        log.info(f"{len(comandos)} comando(s) pendientes")
    for cmd in comandos:
        estado, resultado = ejecutar_comando(cmd)
        log.info(f"Comando #{cmd['id']} {cmd['tipo']} -> {estado}")
        try:
            _post(f"{conf['server_url']}{conf['api_prefix']}/agente/resultado",
                  {"comando_id": cmd["id"], "estado": estado, "resultado": resultado},
                  conf["token"])
        except Exception as e:
            log.warning(f"no pude reportar resultado de #{cmd['id']}: {e}")


def poll_comandos(conf: dict):
    """Trae y ejecuta comandos pendientes SIN reportar inventario (rápido).
    Permite responder a 'ver pantalla' en segundos, no cada N minutos."""
    import socket as _s
    resp = _post(f"{conf['server_url']}{conf['api_prefix']}/agente/comandos",
                 {"hostname": _s.gethostname()}, conf["token"], timeout=30)
    _ejecutar_comandos(conf, resp.get("comandos", []))


_parar = {"flag": False}


def _instalar_handlers_senal():
    """Maneja las señales de stop de Windows para salir LIMPIO y rápido.

    NSSM detiene el servicio mandando Ctrl-C (CTRL_C_EVENT) o, si no, matando el
    proceso. Sin un handler, Python lanza KeyboardInterrupt en medio del loop y
    PyInstaller lo reporta como 'unhandled exception' → NSSM lo ve como muerte
    anómala y puede quedar en Stop-Pending. Con el handler salimos con código 0
    de inmediato, así NSSM completa el stop sin colgarse. Esto es lo que destraba
    el auto-update."""
    import signal

    def _handler(signum, frame):
        _parar["flag"] = True
        try:
            log.info(f"señal {signum} recibida — saliendo limpio")
        except Exception:
            pass
        os._exit(0)  # salida inmediata, sin levantar excepciones

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _handler)
        except Exception:
            pass
    # En Windows, capturar también CTRL_BREAK/CTRL_CLOSE vía SetConsoleCtrlHandler
    try:
        import ctypes
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)
        def _console_handler(ctrl_type):
            _parar["flag"] = True
            os._exit(0)
            return True
        # guardar ref para que no la recoja el GC
        _parar["_chref"] = _console_handler
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
    except Exception:
        pass


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    _instalar_handlers_senal()
    conf = cargar_config()
    log.info(f"=== RedBF Agent v{__version__} ===")
    log.info(f"Servidor: {conf['server_url']} | inventario: {conf['intervalo_min']} min, "
             f"comandos: {conf['poll_comandos_s']}s")

    ultimo_inv = 0.0
    inv_interval = conf["intervalo_min"] * 60
    while not _parar["flag"]:
        ahora = time.time()
        try:
            # Reporte de inventario completo cada N minutos
            if ahora - ultimo_inv >= inv_interval:
                ciclo(conf)
                log.info("Inventario reportado OK")
                ultimo_inv = ahora
            else:
                # Entre reportes: solo poll rápido de comandos (pantalla, etc.)
                poll_comandos(conf)
        except urllib.error.URLError as e:
            log.warning(f"servidor no alcanzable: {e}")
        except Exception as e:
            log.exception(f"error en loop: {e}")
        time.sleep(conf["poll_comandos_s"])


def _modo_helper_captura(ruta_salida: str) -> int:
    """Modo helper: captura la pantalla y la escribe a `ruta_salida`. Sale.
    El servicio (Session 0) lanza el .exe con este modo EN la sesión del usuario,
    donde sí hay escritorio visible."""
    try:
        from collectors import pantalla
        r = pantalla.capturar(calidad=55)
        if r.get("ok"):
            import base64
            with open(ruta_salida, "wb") as f:
                f.write(base64.b64decode(r["base64"]))
            # escribir también el formato en un sidecar
            with open(ruta_salida + ".fmt", "w") as f:
                f.write(r["formato"])
            return 0
    except Exception as e:
        try:
            with open(ruta_salida + ".err", "w") as f:
                f.write(str(e))
        except Exception:
            pass
    return 1


if __name__ == "__main__":
    # Modo helper de captura (invocado por el servicio en la sesión del usuario)
    if len(sys.argv) >= 3 and sys.argv[1] == "--captura":
        sys.exit(_modo_helper_captura(sys.argv[2]))
    # Modo control remoto (invocado en la sesión del usuario): --control <ws_url> <hostname>
    if len(sys.argv) >= 4 and sys.argv[1] == "--control":
        from collectors import control_remoto
        _modo = sys.argv[4] if len(sys.argv) >= 5 else "consentimiento"
        _operador = sys.argv[5] if len(sys.argv) >= 6 else "Sistemas"
        _sslv = (sys.argv[6] != "0") if len(sys.argv) >= 7 else True
        control_remoto.ejecutar_control(sys.argv[2], sys.argv[3],
                                        modo=_modo, operador=_operador, ssl_verify=_sslv)
        sys.exit(0)
    # Modo toast (invocado en la sesión del usuario):
    #   --toast <titulo_b64> <mensaje_b64> [<ruta_testigo>]
    # El testigo (si se pasa) es un archivo que el agente en Session 0 vigila para
    # confirmar que el toast se MOSTRÓ de verdad. Escribimos "1" si el usuario lo
    # vio (MessageBox creado) o "0" si corrió pero no había escritorio visible.
    if len(sys.argv) >= 3 and sys.argv[1] == "--toast":
        import base64
        from collectors import notificar
        titulo = base64.b64decode(sys.argv[2]).decode("utf-8")
        mensaje = base64.b64decode(sys.argv[3]).decode("utf-8") if len(sys.argv) > 3 else ""
        visto = False
        try:
            visto = notificar.mostrar_toast(titulo, mensaje)
        finally:
            testigo = sys.argv[4] if len(sys.argv) > 4 else ""
            if testigo:
                try:
                    with open(testigo, "w", encoding="ascii") as _f:
                        _f.write("1" if visto else "0")
                except OSError:
                    pass
        sys.exit(0 if visto else 2)
    main()
