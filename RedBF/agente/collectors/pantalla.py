"""Captura de pantalla del agente RedBF.

Dos métodos, con fallback:
  1. PIL ImageGrab (si está disponible) — rápido, multi-monitor.
  2. .NET via PowerShell (System.Drawing) — sin dependencias, siempre presente
     en Windows. Captura la pantalla virtual completa a PNG en base64.

LIMITACIÓN de servicios: un servicio en Session 0 (LocalSystem) NO ve el
escritorio del usuario. Para captura real, el componente de pantalla debe correr
en la sesión interactiva del usuario. El agente detecta si está en Session 0 y
lo reporta para que el servidor lo sepa.
"""
from __future__ import annotations
import base64
import os
import subprocess
import sys
import tempfile


def _en_session0() -> bool:
    """True si corre en Session 0 (servicio, sin escritorio de usuario)."""
    try:
        import ctypes
        pid = os.getpid()
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)):
            return sid.value == 0
    except Exception:
        pass
    return False


def _captura_pil(calidad: int = 60) -> bytes | None:
    """Captura con PIL ImageGrab → JPEG bytes. all_screens=True para multimonitor."""
    try:
        from PIL import ImageGrab
        import io
        img = ImageGrab.grab(all_screens=True)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=calidad)
        return buf.getvalue()
    except Exception:
        return None


def _captura_dotnet() -> bytes | None:
    """Captura con System.Drawing vía PowerShell → PNG bytes. Sin dependencias."""
    tmp = os.path.join(tempfile.gettempdir(), f"_redbf_cap_{os.getpid()}.png")
    script = f"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
$bmp.Save('{tmp}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if os.path.exists(tmp):
            with open(tmp, "rb") as f:
                data = f.read()
            os.remove(tmp)
            return data
    except Exception:
        pass
    return None


def _captura_via_helper(calidad: int) -> dict | None:
    """En Session 0 (servicio): lanza el propio .exe como helper en la sesión del
    usuario logueado para capturar su escritorio. Lee el archivo resultante."""
    try:
        from . import session_helper
    except Exception:
        return None
    out = os.path.join(tempfile.gettempdir(), f"_redbf_helper_cap_{os.getpid()}.img")
    # limpiar restos previos
    for ext in ("", ".fmt", ".err"):
        try:
            os.remove(out + ext)
        except OSError:
            pass
    # comando: el propio ejecutable con --captura <archivo>
    if getattr(sys, "frozen", False):
        cmdline = f'"{sys.executable}" --captura "{out}"'
    else:
        # modo script: python agente.py --captura <out>
        agente_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agente.py")
        cmdline = f'"{sys.executable}" "{agente_py}" --captura "{out}"'

    ok = session_helper.ejecutar_en_sesion_usuario(cmdline, timeout=35)
    if not ok or not os.path.exists(out):
        err = ""
        if os.path.exists(out + ".err"):
            try:
                err = open(out + ".err").read()[:200]
            except Exception:
                pass
        return {"ok": False, "session0": True,
                "error": f"Helper en sesión de usuario falló{': ' + err if err else ''}"}
    fmt = "jpeg"
    if os.path.exists(out + ".fmt"):
        try:
            fmt = open(out + ".fmt").read().strip() or "jpeg"
        except Exception:
            pass
    data = open(out, "rb").read()
    for ext in ("", ".fmt", ".err"):
        try:
            os.remove(out + ext)
        except OSError:
            pass
    return {"ok": True, "formato": fmt, "session0": True,
            "base64": base64.b64encode(data).decode("ascii"), "bytes": len(data),
            "via_helper": True}


def capturar(calidad: int = 60) -> dict:
    """Captura la pantalla. Devuelve {ok, formato, base64, session0, error}.

    Si corre como servicio (Session 0), no ve el escritorio del usuario → lanza
    un helper en la sesión interactiva del usuario (como NetSupport)."""
    session0 = _en_session0()

    if session0:
        # Captura directa NO funciona desde Session 0 → usar helper.
        r = _captura_via_helper(calidad)
        if r is not None:
            return r
        # si el helper no se pudo usar, intentar directo igual (puede fallar)

    data = _captura_pil(calidad)
    fmt = "jpeg"
    if not data:
        data = _captura_dotnet()
        fmt = "png"
    if not data:
        return {"ok": False, "session0": session0,
                "error": "No se pudo capturar (¿servicio en Session 0 sin escritorio?)"}
    return {
        "ok": True,
        "formato": fmt,
        "session0": session0,
        "base64": base64.b64encode(data).decode("ascii"),
        "bytes": len(data),
    }


if __name__ == "__main__":
    r = capturar()
    print(f"ok={r['ok']} formato={r.get('formato')} session0={r['session0']} "
          f"bytes={r.get('bytes')} error={r.get('error','')}")
    if r["ok"]:
        out = "captura_test." + r["formato"]
        with open(out, "wb") as f:
            f.write(base64.b64decode(r["base64"]))
        print(f"guardada en {out}")
