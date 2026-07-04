"""Control remoto en vivo del agente RedBF.

Cuando el agente recibe el comando INICIAR_CONTROL, ejecuta este módulo EN LA
SESIÓN DEL USUARIO (vía el helper de Session 0, igual que la captura). Allí:
  - abre un WebSocket al servidor (/ws/agente/{hostname})
  - en loop: captura la pantalla y la envía como frames JPEG (base64)
  - recibe eventos de mouse/teclado del operador y los inyecta con SendInput

Inyección de input: ctypes + Win32 SendInput (mouse_event/keybd_event modernos).
Las coordenadas del mouse llegan normalizadas 0..1 (el operador puede tener otra
resolución) y se mapean a la pantalla virtual real.

Uso (modo helper, lanzado por el servicio en la sesión del usuario):
    redbf-agent.exe --control <ws_url> <hostname>
"""
from __future__ import annotations
import base64
import ctypes
import io
import json
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes


# ── Inyección de input (Win32 SendInput) ───────────────────────────
user32 = ctypes.windll.user32

# Tipos de input
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Flags de mouse
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800

# Flags de teclado
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


def _send(inp: INPUT):
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _virtual_screen():
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return x, y, w, h


def mover_mouse(nx: float, ny: float):
    """nx,ny normalizados 0..1 sobre el MONITOR que se está viendo.

    El cliente normaliza sobre el canvas (= el monitor activo). Para inyectar el
    input hay que reproyectar esas coords al rectángulo real de ese monitor en la
    pantalla virtual, y de ahí a 0..65535 (que SendInput ABSOLUTE mide sobre la
    virtual completa). Si `_activo` cubre toda la virtual (modo "Todos"), el mapeo
    es la identidad."""
    nx = max(0.0, min(1.0, nx))
    ny = max(0.0, min(1.0, ny))
    vsx, vsy, vsw, vsh = _virtual_screen()
    m = _grabber.get("rect") or {"left": vsx, "top": vsy, "width": vsw, "height": vsh}
    # coord absoluta en px sobre la virtual
    px = m["left"] + nx * m["width"]
    py = m["top"] + ny * m["height"]
    # a 0..65535 sobre la virtual (evitar div/0)
    ax = int((px - vsx) / max(1, vsw) * 65535)
    ay = int((py - vsy) / max(1, vsh) * 65535)
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(max(0, min(65535, ax)), max(0, min(65535, ay)), 0,
                          MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | 0x4000,  # +VIRTUALDESK
                          0, None)
    _send(inp)


def click_mouse(boton: str, accion: str):
    flags = {
        ("left", "down"): MOUSEEVENTF_LEFTDOWN, ("left", "up"): MOUSEEVENTF_LEFTUP,
        ("right", "down"): MOUSEEVENTF_RIGHTDOWN, ("right", "up"): MOUSEEVENTF_RIGHTUP,
    }.get((boton, accion))
    if flags is None:
        return
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(0, 0, 0, flags, 0, None)
    _send(inp)


def scroll_mouse(delta: int):
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(0, 0, delta, MOUSEEVENTF_WHEEL, 0, None)
    _send(inp)


def tecla(vk: int, accion: str):
    """Envía una tecla por Virtual-Key code."""
    flags = KEYEVENTF_KEYUP if accion == "up" else 0
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, 0, flags, 0, None)
    _send(inp)


def escribir_unicode(ch: str):
    """Escribe un carácter Unicode (para texto general)."""
    code = ord(ch)
    for accion in (0, KEYEVENTF_KEYUP):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | accion, 0, None)
        _send(inp)


# Teclas extendidas: necesitan KEYEVENTF_EXTENDEDKEY para que Windows las
# distinga (flechas, Ins/Del/Home/End/PgUp/PgDn, Win izq/der, Ctrl/Alt der).
KEYEVENTF_EXTENDEDKEY = 0x0001
_VK_EXTENDIDAS = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22,   # Ins Del Home End PgUp PgDn
                  0x25, 0x26, 0x27, 0x28,                # ← ↑ → ↓
                  0x5B, 0x5C,                            # Win izq/der
                  0xA3, 0xA5, 0x2C}                      # Ctrl der, Alt der, PrintScreen


def _tecla_ext(vk: int, accion: str):
    """Como tecla() pero marca EXTENDEDKEY cuando corresponde (combos correctos)."""
    flags = KEYEVENTF_KEYUP if accion == "up" else 0
    if vk in _VK_EXTENDIDAS:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, 0, flags, 0, None)
    _send(inp)


def combo(vks):
    """Presiona una secuencia de Virtual-Keys COMO combinación: las presiona en
    orden y las suelta en orden inverso (p.ej. [CTRL, ALT, END] = Ctrl+Alt+End).
    Nota: Ctrl+Alt+Supr real (SAS) NO se puede inyectar por diseño de Windows;
    el dashboard usa Ctrl+Alt+End (equivalente en RDP) en su lugar."""
    vks = [int(v) for v in vks if v is not None]
    if not vks:
        return
    for v in vks:
        _tecla_ext(v, "down")
    for v in reversed(vks):
        _tecla_ext(v, "up")


# ── Captura para streaming ─────────────────────────────────────────
# Nivel 1: captura rápida (mss) + diff por tiles. Solo se envían los bloques
# que cambiaron respecto al frame anterior, como un mensaje BINARIO. En uso de
# oficina (escribir, mover el mouse) eso es ~2-5% de la pantalla por frame.
TILE = 128  # lado del tile en px (sobre la imagen ya escalada)

# _grabber["monitor"]: qué se captura.
#   0            = todos los monitores (pantalla virtual combinada)
#   1, 2, 3, …   = ese monitor individual (índice mss.monitors[N])
# _grabber["rect"]: rectángulo en px del monitor activo sobre la pantalla virtual
#   {left, top, width, height} — lo usa mover_mouse() para reproyectar el input.
_grabber = {"sct": None, "mon": None, "metodo": None, "monitor": 1, "rect": None}


def listar_monitores():
    """Devuelve [{'id':N,'nombre':str,'w':px,'h':px,'principal':bool}, …] para que
    el operador elija cuál ver. id=0 es 'Todos'. Usa mss; si falla, cae a GDI
    (que solo distingue 'principal' vs 'todos')."""
    salida = [{"id": 0, "nombre": "Todos los monitores", "w": 0, "h": 0, "principal": False}]
    try:
        import mss
        with mss.mss() as sct:
            mons = sct.monitors  # [0]=virtual, [1..]=cada monitor
            virt = mons[0]
            salida[0]["w"], salida[0]["h"] = virt["width"], virt["height"]
            for i, m in enumerate(mons[1:], start=1):
                # el monitor principal empieza en (0,0) en coords de Windows
                principal = (m["left"] == 0 and m["top"] == 0)
                salida.append({"id": i,
                               "nombre": f"Monitor {i}" + (" (principal)" if principal else ""),
                               "w": m["width"], "h": m["height"], "principal": principal})
    except Exception as e:
        _log_ctrl(f"[control] listar_monitores mss falló: {e}; fallback GDI")
        x, y, w, h = _virtual_screen()
        salida.append({"id": 1, "nombre": "Monitor 1 (principal)",
                       "w": w, "h": h, "principal": True})
    return salida


def set_monitor(mid: int):
    """Cambia el monitor activo. Fuerza reinicializar el grabber mss para tomar la
    nueva geometría."""
    _grabber["monitor"] = int(mid)
    _grabber["mon"] = None
    _grabber["sct"] = None
    _grabber["rect"] = None


def _grab_mss(escala: float):
    import numpy as np
    if _grabber["sct"] is None:
        import mss
        _grabber["sct"] = mss.mss()
        mons = _grabber["sct"].monitors  # [0]=virtual, [1..]=cada monitor
        mid = _grabber.get("monitor", 1)
        # id fuera de rango o 0 → todos; si no, el monitor pedido
        mon = mons[mid] if (0 < mid < len(mons)) else mons[0]
        _grabber["mon"] = mon
        _grabber["rect"] = {"left": mon["left"], "top": mon["top"],
                            "width": mon["width"], "height": mon["height"]}
    raw = _grabber["sct"].grab(_grabber["mon"])
    arr = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
    if escala < 0.999:
        paso = max(1, round(1.0 / escala))
        if paso > 1:
            arr = arr[::paso, ::paso, :]
    return np.ascontiguousarray(arr)


def _grab_gdi(escala: float):
    """Fallback con PIL ImageGrab (GDI). Más lento pero funciona en sesiones donde
    mss/DXGI falla (algunas configuraciones de Session 0 / escritorio remoto).
    Respeta el monitor activo: 0=todos (all_screens), N=recorta ese monitor."""
    import numpy as np
    from PIL import ImageGrab
    mid = _grabber.get("monitor", 1)
    img = ImageGrab.grab(all_screens=True)
    vsx, vsy, vsw, vsh = _virtual_screen()
    if mid != 0:
        # recortar el monitor pedido dentro de la imagen virtual (que arranca en vsx,vsy)
        try:
            from ctypes import wintypes
            monitores = _enum_monitores_win()
            if 0 < mid <= len(monitores):
                r = monitores[mid - 1]
                bx, by = r["left"] - vsx, r["top"] - vsy
                img = img.crop((bx, by, bx + r["width"], by + r["height"]))
                _grabber["rect"] = r
        except Exception as e:
            _log_ctrl(f"[control] GDI recorte monitor falló: {e}; enviando todo")
            _grabber["rect"] = {"left": vsx, "top": vsy, "width": vsw, "height": vsh}
    else:
        _grabber["rect"] = {"left": vsx, "top": vsy, "width": vsw, "height": vsh}
    if escala < 0.999:
        img = img.resize((max(1, int(img.width * escala)),
                          max(1, int(img.height * escala))))
    return np.ascontiguousarray(np.asarray(img.convert("RGB"), dtype=np.uint8))


def _enum_monitores_win():
    """Enumera monitores vía EnumDisplayMonitors (Win32). Devuelve lista de dicts
    {left,top,width,height} en coords de la pantalla virtual, orden del sistema."""
    monitores = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_int, wintypes.HANDLE, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _cb(hmon, hdc, lprc, lparam):
        r = lprc.contents
        monitores.append({"left": r.left, "top": r.top,
                          "width": r.right - r.left, "height": r.bottom - r.top})
        return 1
    user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(_cb), 0)
    # monitor principal primero (empieza en 0,0), para alinear con mss
    monitores.sort(key=lambda m: (m["left"] != 0 or m["top"] != 0, m["left"], m["top"]))
    return monitores


def _grab_rgb(escala: float):
    """Captura la pantalla como ndarray RGB (H, W, 3) uint8. Intenta mss (rápido,
    DXGI) y si falla cae a GDI/ImageGrab. Recuerda el método que funcionó."""
    if _grabber["metodo"] == "gdi":
        return _grab_gdi(escala)
    try:
        arr = _grab_mss(escala)
        _grabber["metodo"] = "mss"
        return arr
    except Exception as e:
        # mss falló: resetear su estado y caer a GDI (y recordarlo)
        _log_ctrl(f"[control] mss falló ({type(e).__name__}: {e}); usando GDI")
        _grabber["sct"] = None
        _grabber["metodo"] = "gdi"
        return _grab_gdi(escala)


def _grab_jpeg(calidad: int) -> bytes | None:
    """Fallback: pantalla completa JPEG (compatibilidad)."""
    try:
        from PIL import Image
        arr = _grab_rgb(1.0)
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="JPEG", quality=calidad)
        return buf.getvalue()
    except Exception:
        return None


def _tiles_changed(arr, prev, tile: int, calidad: int):
    """Compara arr (ndarray HxWx3) con el frame previo por bloques, vectorizado.
    Devuelve (cambios, keyframe, cols, rows, w, h). cambios = [(idx, jpeg_bytes)].
    Un tile cambia si algún píxel difiere > UMBRAL (ignora ruido subpixel)."""
    import numpy as np
    from PIL import Image
    h, w = arr.shape[:2]
    cols = (w + tile - 1) // tile
    rows = (h + tile - 1) // tile
    keyframe = prev is None or prev.shape != arr.shape
    UMBRAL = 8  # diferencia mínima por canal para considerar el bloque "cambiado"
    cambios = []
    for ry in range(rows):
        y0, y1 = ry * tile, min((ry + 1) * tile, h)
        for rx in range(cols):
            x0, x1 = rx * tile, min((rx + 1) * tile, w)
            idx = ry * cols + rx
            blk = arr[y0:y1, x0:x1]
            if not keyframe:
                pblk = prev[y0:y1, x0:x1]
                # diff absoluto vectorizado; si el máximo no supera el umbral, sin cambio
                if int(np.abs(blk.astype(np.int16) - pblk.astype(np.int16)).max()) <= UMBRAL:
                    continue
            buf = io.BytesIO()
            Image.fromarray(blk).save(buf, format="JPEG", quality=calidad)
            cambios.append((idx, buf.getvalue()))
    return cambios, keyframe, cols, rows, w, h


def _empaquetar_init(w, h, tile, cols, rows) -> bytes:
    import struct
    return b"\x01" + struct.pack("<HHHHH", w, h, tile, cols, rows)


def _empaquetar_tiles(cambios) -> bytes:
    import struct
    out = bytearray(b"\x02")
    out += struct.pack("<H", len(cambios))
    for idx, jpg in cambios:
        out += struct.pack("<HI", idx, len(jpg))
        out += jpg
    return bytes(out)


# ── Loop de control (cliente WebSocket) ────────────────────────────
def _log_ctrl(msg: str):
    """Log de diagnóstico del control (el helper no tiene consola)."""
    try:
        with open(os.path.join(tempfile.gettempdir(), "_redbf_control.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def pedir_consentimiento(operador: str, timeout_s: int = 30) -> bool:
    """Muestra un cuadro de diálogo al usuario pidiendo permiso para el control.
    Devuelve True si ACEPTA, False si rechaza o no responde en timeout_s.

    Usa MessageBoxTimeoutW (user32, no documentada pero estable desde XP): igual
    que MessageBoxW pero se cierra solo tras el timeout devolviendo un código
    especial (32000). Como el control ya corre en la sesión del usuario, el cuadro
    es visible en su escritorio."""
    MB_YESNO = 0x04
    MB_ICONQUESTION = 0x20
    MB_TOPMOST = 0x40000
    MB_SETFOREGROUND = 0x10000
    IDYES = 6
    titulo = "RedBF — Solicitud de control remoto"
    quien = operador or "Sistemas"
    texto = (f"{quien} (Sistemas) quiere ver y controlar tu equipo.\n\n"
             f"¿Permites el control remoto?\n\n"
             f"(Si no respondes en {timeout_s} segundos, se cancela.)")
    try:
        # firma: MessageBoxTimeoutW(hWnd, lpText, lpCaption, uType, wLangId, dwMs)
        fn = ctypes.windll.user32.MessageBoxTimeoutW
        fn.restype = ctypes.c_int
        ret = fn(0, ctypes.c_wchar_p(texto), ctypes.c_wchar_p(titulo),
                 MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND,
                 0, int(timeout_s * 1000))
        return ret == IDYES
    except Exception as e:
        _log_ctrl(f"[control] error en consentimiento: {e}")
        # ante un fallo del diálogo, denegar por seguridad
        return False


def ejecutar_control(ws_url: str, hostname: str, duracion_max_s: int = 1800,
                     modo: str = "consentimiento", operador: str = "Sistemas",
                     ssl_verify: bool = True):
    """Conecta al WS del servidor, streamea pantalla y aplica input recibido.

    Antes de streamear hace el HANDSHAKE de autorización según `modo`:
      - 'directo': arranca de inmediato (servidores/cajas sin usuario).
      - 'pin'    : el servidor ya validó el PIN; arranca igual que directo.
      - 'consentimiento': pregunta al usuario; si no acepta en 30s, se cancela.
    duracion_max_s: corta la sesión por seguridad tras X tiempo."""
    import websockets
    import asyncio
    import ssl as _ssl

    estado = {"fps": 12, "calidad": 55, "stop": False, "escala": 1.0,
              "keyframe": False}
    _log_ctrl(f"[control] iniciando, url={ws_url[:80]} modo={modo}")

    async def run():
        # ws_url ya viene completa (incluye /ws/agente/{host}?token=...) desde el
        # agente. Compat: si no la incluye (uso antiguo), la armamos.
        url = ws_url if "/ws/agente/" in ws_url else f"{ws_url}/ws/agente/{hostname}"
        # SSL: por defecto VALIDAMOS el certificado (anti-MITM en internet). En
        # producción el servidor (app.brunoferrini.pe) tiene cert Let's Encrypt
        # válido, así que valida sin problema. Solo se desactiva si el config tiene
        # ssl_verify=false explícito (entornos con cert propio/autofirmado en LAN).
        connect_kwargs = {"max_size": None, "ping_interval": 20}
        if url.startswith("wss://"):
            if ssl_verify:
                ctx = _ssl.create_default_context()  # CERT_REQUIRED + check_hostname
            else:
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                _log_ctrl("[control] SSL sin validar (ssl_verify=false en config)")
            connect_kwargs["ssl"] = ctx
        _log_ctrl(f"[control] conectando a {url[:80]} (ssl_verify={ssl_verify})")
        async with websockets.connect(url, **connect_kwargs) as ws:
            _log_ctrl("[control] WS conectado")

            # ── HANDSHAKE de autorización (consentimiento estilo AnyDesk) ──
            # 'directo' y 'pin' (ya validado por el servidor) arrancan directo.
            # 'consentimiento' pregunta al usuario con un cuadro de diálogo.
            if modo == "consentimiento":
                try:
                    await ws.send(json.dumps({"t": "prompt",
                                              "msg": "Esperando que el usuario acepte…"}))
                except Exception:
                    return
                _log_ctrl("[control] pidiendo consentimiento al usuario…")
                # el MessageBox es bloqueante: correrlo en un hilo para no congelar el WS
                acepto = await asyncio.get_event_loop().run_in_executor(
                    None, pedir_consentimiento, operador, 30)
                if not acepto:
                    _log_ctrl("[control] usuario NO autorizó (rechazo o timeout)")
                    try:
                        await ws.send(json.dumps({"t": "denied", "reason": "rejected"}))
                    except Exception:
                        pass
                    return
                _log_ctrl("[control] usuario autorizó el control")

            try:
                await ws.send(json.dumps({"t": "granted"}))
            except Exception:
                return
            _log_ctrl("[control] autorizado, enviando frames")

            # informar al operador qué monitores hay para que pueda elegir.
            # default = monitor principal (id 1); si solo hay uno, es transparente.
            try:
                mons = listar_monitores()
                set_monitor(1 if len(mons) > 1 else 0)
                await ws.send(json.dumps({"t": "monitors", "lista": mons,
                                          "activo": _grabber["monitor"]}))
            except Exception as e:
                _log_ctrl(f"[control] no se pudo listar monitores: {e}")

            # Executor DEDICADO de 1 thread para la captura: mss NO es thread-safe
            # entre threads distintos, así que el objeto sct siempre se usa desde el
            # mismo hilo. Aísla el BitBlt/mss del event loop sin romper mss.
            import concurrent.futures
            _grab_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                               thread_name_prefix="rbf-grab")

            async def enviar_frames():
                prev = None
                geom = None  # (cols, rows, w, h) ya enviada al cliente
                quietos = 0  # frames consecutivos sin cambios (para FPS adaptativo)
                fallos = 0   # capturas fallidas consecutivas (para degradar)
                loop = asyncio.get_event_loop()
                while not estado["stop"]:
                    t_ini = time.monotonic()
                    try:
                        # CRÍTICO: la captura (BitBlt/mss) es SÍNCRONA y puede colgarse
                        # (BitBlt timeout). Si se llama directo bloquea el event loop y
                        # AHOGA recibir_input() → el mouse deja de responder. Corriéndola
                        # en un thread (executor), el loop sigue procesando el input.
                        img = await loop.run_in_executor(_grab_pool, _grab_rgb, estado["escala"])
                        fallos = 0
                    except Exception as e:
                        fallos += 1
                        _log_ctrl(f"[control] grab error #{fallos}: {e}")
                        # Degradar: si la captura falla repetido, esperar más para no
                        # saturar el sistema gráfico (deja respirar a otros procesos y
                        # al input). Backoff hasta 2s.
                        await asyncio.sleep(min(2.0, 0.3 * fallos))
                        continue

                    forzar_key = estado.pop("keyframe", False)
                    cambios, keyframe, cols, rows, w, h = _tiles_changed(
                        img, None if forzar_key else prev, TILE, estado["calidad"])
                    prev = img

                    # si cambió la geometría (resolución), reenviar init
                    nueva_geom = (cols, rows, w, h)
                    if nueva_geom != geom:
                        try:
                            await ws.send(_empaquetar_init(w, h, TILE, cols, rows))
                        except Exception:
                            break
                        geom = nueva_geom

                    if cambios:
                        try:
                            await ws.send(_empaquetar_tiles(cambios))
                        except Exception:
                            break
                        quietos = 0
                    else:
                        quietos += 1

                    # FPS adaptativo: alto cuando hay actividad, bajo cuando quieto
                    fps = estado["fps"] if quietos < 8 else max(2, estado["fps"] // 4)
                    dt = time.monotonic() - t_ini
                    await asyncio.sleep(max(0.0, (1.0 / max(1, fps)) - dt))

            async def recibir_input():
                vsx, vsy, vsw, vsh = _virtual_screen()
                while not estado["stop"]:
                    try:
                        raw = await ws.recv()
                    except Exception:
                        break
                    try:
                        m = json.loads(raw)
                    except Exception:
                        continue
                    t = m.get("t")
                    if t == "mouse":
                        if "x" in m and "y" in m:
                            mover_mouse(m["x"], m["y"])
                        act = m.get("action")
                        if act == "scroll":
                            scroll_mouse(int(m.get("delta", 0)))
                        elif act in ("down", "up"):
                            click_mouse(m.get("button", "left"), act)
                    elif t == "key":
                        if m.get("char"):
                            escribir_unicode(m["char"])
                        elif m.get("vk") is not None:
                            tecla(int(m["vk"]), m.get("action", "down"))
                    elif t == "combo":
                        # combinación de teclas especial (Ctrl+Alt+End, Win+L, etc.)
                        vks = m.get("vks") or []
                        try:
                            combo(vks)
                        except Exception as e:
                            _log_ctrl(f"[control] combo error: {e}")
                    elif t == "config":
                        estado["fps"] = int(m.get("fps", estado["fps"]))
                        estado["calidad"] = int(m.get("calidad", estado["calidad"]))
                        if m.get("escala") is not None:
                            estado["escala"] = max(0.3, min(1.0, float(m["escala"])))
                            estado["keyframe"] = True  # re-render completo al cambiar escala
                        if m.get("monitor") is not None:
                            set_monitor(int(m["monitor"]))
                            estado["keyframe"] = True  # nueva geometría → cuadro completo
                    elif t == "keyframe":
                        estado["keyframe"] = True  # el cliente pide cuadro completo
                    elif t == "stop":
                        estado["stop"] = True
                        break

            try:
                await asyncio.gather(enviar_frames(), recibir_input())
            finally:
                estado["stop"] = True
                _grab_pool.shutdown(wait=False)

    try:
        asyncio.run(run())
    except Exception as e:
        # log mínimo a archivo (el helper no tiene consola)
        try:
            import os, tempfile
            with open(os.path.join(tempfile.gettempdir(), "_redbf_control.err"), "w") as f:
                f.write(f"{type(e).__name__}: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    # modo helper: redbf-agent.exe --control <ws_url> <hostname> [modo] [operador] [ssl_verify]
    if len(sys.argv) >= 3:
        _modo = sys.argv[3] if len(sys.argv) >= 4 else "consentimiento"
        _operador = sys.argv[4] if len(sys.argv) >= 5 else "Sistemas"
        _sslv = (sys.argv[5] != "0") if len(sys.argv) >= 6 else True
        ejecutar_control(sys.argv[1], sys.argv[2], modo=_modo, operador=_operador,
                         ssl_verify=_sslv)
