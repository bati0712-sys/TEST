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
    """nx,ny normalizados 0..1 sobre la pantalla virtual."""
    # SendInput ABSOLUTE usa 0..65535 sobre la pantalla virtual completa
    ax = int(max(0.0, min(1.0, nx)) * 65535)
    ay = int(max(0.0, min(1.0, ny)) * 65535)
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(ax, ay, 0,
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


# ── Captura para streaming ─────────────────────────────────────────
# Nivel 1: captura rápida (mss) + diff por tiles. Solo se envían los bloques
# que cambiaron respecto al frame anterior, como un mensaje BINARIO. En uso de
# oficina (escribir, mover el mouse) eso es ~2-5% de la pantalla por frame.
TILE = 128  # lado del tile en px (sobre la imagen ya escalada)

_grabber = {"sct": None, "mon": None, "metodo": None}


def _grab_mss(escala: float):
    import numpy as np
    if _grabber["sct"] is None:
        import mss
        _grabber["sct"] = mss.mss()
        _grabber["mon"] = _grabber["sct"].monitors[0]  # 0 = todos los monitores
    raw = _grabber["sct"].grab(_grabber["mon"])
    arr = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
    if escala < 0.999:
        paso = max(1, round(1.0 / escala))
        if paso > 1:
            arr = arr[::paso, ::paso, :]
    return np.ascontiguousarray(arr)


def _grab_gdi(escala: float):
    """Fallback con PIL ImageGrab (GDI). Más lento pero funciona en sesiones donde
    mss/DXGI falla (algunas configuraciones de Session 0 / escritorio remoto)."""
    import numpy as np
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True)
    if escala < 0.999:
        img = img.resize((max(1, int(img.width * escala)),
                          max(1, int(img.height * escala))))
    return np.ascontiguousarray(np.asarray(img.convert("RGB"), dtype=np.uint8))


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

            async def enviar_frames():
                prev = None
                geom = None  # (cols, rows, w, h) ya enviada al cliente
                quietos = 0  # frames consecutivos sin cambios (para FPS adaptativo)
                while not estado["stop"]:
                    t_ini = time.monotonic()
                    try:
                        img = _grab_rgb(estado["escala"])
                    except Exception as e:
                        _log_ctrl(f"[control] grab error: {e}")
                        await asyncio.sleep(0.5)
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
                    elif t == "config":
                        estado["fps"] = int(m.get("fps", estado["fps"]))
                        estado["calidad"] = int(m.get("calidad", estado["calidad"]))
                        if m.get("escala") is not None:
                            estado["escala"] = max(0.3, min(1.0, float(m["escala"])))
                            estado["keyframe"] = True  # re-render completo al cambiar escala
                    elif t == "keyframe":
                        estado["keyframe"] = True  # el cliente pide cuadro completo
                    elif t == "stop":
                        estado["stop"] = True
                        break

            await asyncio.gather(enviar_frames(), recibir_input())

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
