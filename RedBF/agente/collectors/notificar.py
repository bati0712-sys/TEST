"""Notificaciones toast de Windows — RedBF.

Muestra una notificación nativa de Windows (la del centro de notificaciones,
esquina inferior derecha) en la PC del usuario. Usa la API
Windows.UI.Notifications vía PowerShell — sin dependencias externas.

Como el agente corre en Session 0 (servicio), la notificación debe lanzarse EN
LA SESIÓN DEL USUARIO (con el helper), igual que la captura. Aquí va la función
que GENERA el toast; el lanzamiento en sesión de usuario lo hace el agente.
"""
from __future__ import annotations
import os
import subprocess
import tempfile


def _ps_toast_script(titulo: str, mensaje: str, app_id: str = "RedBF") -> str:
    """Script PowerShell que muestra un toast nativo de Windows."""
    # Escapar comillas para XML/PowerShell
    t = (titulo or "RedBF").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    m = (mensaje or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null

$xml = @"
<toast scenario="reminder">
  <visual>
    <binding template="ToastGeneric">
      <text>{t}</text>
      <text>{m}</text>
    </binding>
  </visual>
  <audio src="ms-winsoundevent:Notification.Default"/>
</toast>
"@

$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
# AppId: usar PowerShell para que Windows lo muestre sin registrar app propia
$appId = "{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe"
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
Start-Sleep -Milliseconds 500
"""


def _messagebox(titulo: str, mensaje: str) -> bool:
    """MessageBox nativo (user32) — SIEMPRE visible en la sesión del usuario.
    Es el método más confiable: el toast de Windows.UI.Notifications a veces se
    'muestra' silenciosamente (Foco Asistido, notificaciones de PS desactivadas)
    y el usuario no lo ve. El MessageBox no tiene ese problema.

    Devuelve True solo si el cuadro se mostró REALMENTE. MessageBoxW retorna 0
    cuando falla (p. ej. la sesión no tiene un escritorio interactivo visible:
    pantalla de bloqueo, sin usuario logueado, o escritorio Winlogon). Ese 0 es
    la señal que distingue 'mostrado' de 'lanzado pero invisible'."""
    try:
        import ctypes
        MB_OK = 0x0
        MB_ICONINFORMATION = 0x40
        MB_TOPMOST = 0x40000
        MB_SETFOREGROUND = 0x10000
        ret = ctypes.windll.user32.MessageBoxW(
            0, str(mensaje), str(titulo or "RedBF"),
            MB_OK | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND)
        # ret == 0 => MessageBox no pudo crearse (sin escritorio interactivo).
        return ret != 0
    except Exception:
        return False


def mostrar_toast(titulo: str, mensaje: str) -> bool:
    """Muestra la notificación en la sesión del usuario. (El agente la invoca vía
    helper en la sesión interactiva.)

    Estrategia: intenta el toast nativo (bonito, esquina) Y ADEMÁS un MessageBox
    garantizado-visible. El toast puede no verse según la config de Windows; el
    MessageBox siempre se ve. Así el mensaje SIEMPRE llega al usuario.

    Devuelve True solo si el MessageBox se mostró realmente (hay escritorio
    interactivo visible). False indica que el proceso corrió pero el usuario NO
    vio nada (sesión bloqueada / sin login) → el agente debe reportarlo como tal
    en vez de un OK falso."""
    # 1) intentar el toast (no bloqueante, decorativo)
    script = _ps_toast_script(titulo, mensaje)
    tmp = os.path.join(tempfile.gettempdir(), f"_redbf_toast_{os.getpid()}.ps1")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(script)
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp],
            capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # 2) MessageBox garantizado-visible (lo que el usuario sí ve). Su retorno es
    #    la prueba de que hubo un escritorio interactivo donde pintarlo.
    return _messagebox(titulo, mensaje)


def _fallback_msgbox(titulo: str, mensaje: str):
    """Fallback: msg.exe (popup clásico) si el toast no está disponible."""
    try:
        subprocess.run(["msg", "*", "/TIME:60", f"{titulo}: {mensaje}"],
                       capture_output=True, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    titulo = sys.argv[1] if len(sys.argv) > 1 else "RedBF — Prueba"
    mensaje = sys.argv[2] if len(sys.argv) > 2 else "Notificación de prueba de RedBF"
    mostrar_toast(titulo, mensaje)
