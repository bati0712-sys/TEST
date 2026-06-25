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
__version__ = "0.1.0"


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

    bat_content = f"""@echo off
REM Auto-update RedBF Agent. Corre como tarea programada SYSTEM (fuera del arbol
REM del servicio). El agente sale limpio con su handler de senales.
{cfg_stop}REM 1) detener el servicio
{stop_cmd} >nul 2>&1
ping -n 3 127.0.0.1 >nul 2>&1
REM 2) asegurar que no quede ningun proceso del agente bloqueando el .exe
taskkill /F /IM redbf-agent.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul 2>&1
REM 3) reemplazar el binario (reintenta hasta que se libere)
set /a tries=0
:wait
move /Y "{exe_new}" "{exe_actual}" >nul 2>&1
if errorlevel 1 (
    set /a tries+=1
    if %tries% geq 20 goto restart
    ping -n 2 127.0.0.1 >nul 2>&1
    goto wait
)
:restart
REM 4) arrancar; si queda en pending tras 4s, forzar otro ciclo
{start_cmd} >nul 2>&1
ping -n 5 127.0.0.1 >nul 2>&1
sc query {servicio} | find "RUNNING" >nul 2>&1
if errorlevel 1 (
    {stop_cmd} >nul 2>&1
    ping -n 2 127.0.0.1 >nul 2>&1
    {start_cmd} >nul 2>&1
)
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
    import base64
    t_b64 = base64.b64encode(titulo.encode("utf-8")).decode("ascii")
    m_b64 = base64.b64encode(mensaje.encode("utf-8")).decode("ascii")

    if getattr(sys, "frozen", False):
        cmdline = f'"{sys.executable}" --toast "{t_b64}" "{m_b64}"'
    else:
        agente_py = os.path.abspath(__file__)
        cmdline = f'"{sys.executable}" "{agente_py}" --toast "{t_b64}" "{m_b64}"'

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
            return "OK", {"mensaje": "notificación mostrada al usuario"}
        else:
            from collectors import notificar
            notificar.mostrar_toast(titulo, mensaje)
            return "OK", {"mensaje": "notificación mostrada (interactivo)"}
    except Exception as e:
        return "ERROR", {"error": f"{type(e).__name__}: {e}"}


# ── Control remoto en vivo ─────────────────────────────────────────
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
    es un proceso de larga duración)."""
    from collectors import session_helper as sh
    import ctypes
    from ctypes import wintypes
    sid = sh._sesion_activa()
    if sid is None:
        return False
    user_token = wintypes.HANDLE()
    if not ctypes.windll.wtsapi32.WTSQueryUserToken(sid, ctypes.byref(user_token)):
        return False
    dup = wintypes.HANDLE()
    ok = ctypes.windll.advapi32.DuplicateTokenEx(
        user_token, sh.TOKEN_ALL_FOR_PROCESS, None, sh.SecurityImpersonation,
        sh.TokenPrimary, ctypes.byref(dup))
    ctypes.windll.kernel32.CloseHandle(user_token)
    if not ok:
        return False
    # CRÍTICO: forzar el SessionId del token a la sesión interactiva. Sin esto, el
    # token duplicado a veces queda en Session 0 y CreateProcessAsUser lanza el
    # proceso en Session 0 (sin escritorio del usuario) → captura sí, pero NO input
    # ni notificaciones. Con TokenSessionId el proceso va a la sesión del usuario.
    TokenSessionId = 12
    sid_dword = wintypes.DWORD(sid)
    ctypes.windll.advapi32.SetTokenInformation(
        dup, TokenSessionId, ctypes.byref(sid_dword), ctypes.sizeof(sid_dword))
    env = ctypes.c_void_p()
    ctypes.windll.userenv.CreateEnvironmentBlock(ctypes.byref(env), dup, False)
    si = sh.STARTUPINFO(); si.cb = ctypes.sizeof(sh.STARTUPINFO)
    si.lpDesktop = "winsta0\\default"
    pi = sh.PROCESS_INFORMATION()
    flags = sh.CREATE_UNICODE_ENVIRONMENT | sh.CREATE_NO_WINDOW | sh.NORMAL_PRIORITY_CLASS
    created = ctypes.windll.advapi32.CreateProcessAsUserW(
        dup, None, ctypes.c_wchar_p(cmdline), None, None, False,
        flags, env, None, ctypes.byref(si), ctypes.byref(pi))
    if created:
        ctypes.windll.kernel32.CloseHandle(pi.hProcess)
        ctypes.windll.kernel32.CloseHandle(pi.hThread)
    if env:
        ctypes.windll.userenv.DestroyEnvironmentBlock(env)
    ctypes.windll.kernel32.CloseHandle(dup)
    return bool(created)


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
    # Modo toast (invocado en la sesión del usuario): --toast <titulo_b64> <mensaje_b64>
    if len(sys.argv) >= 3 and sys.argv[1] == "--toast":
        import base64
        from collectors import notificar
        titulo = base64.b64decode(sys.argv[2]).decode("utf-8")
        mensaje = base64.b64decode(sys.argv[3]).decode("utf-8") if len(sys.argv) > 3 else ""
        notificar.mostrar_toast(titulo, mensaje)
        sys.exit(0)
    main()
