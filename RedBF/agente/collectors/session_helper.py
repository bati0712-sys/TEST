"""Lanzar procesos en la SESIÓN INTERACTIVA del usuario desde un servicio (Session 0).

Problema: un servicio Windows (LocalSystem, Session 0) no ve el escritorio del
usuario, así que no puede capturar pantalla directamente. NetSupport y otras
suites resuelven esto lanzando un proceso "helper" EN la sesión del usuario
logueado, vía la API de Windows WTS + CreateProcessAsUser.

Este módulo expone `ejecutar_en_sesion_usuario(cmdline)` que:
  1. Encuentra la sesión interactiva activa (WTSGetActiveConsoleSessionId /
     enumera sesiones con un usuario logueado).
  2. Obtiene el token de ese usuario (WTSQueryUserToken).
  3. Lo duplica como token primario y lanza el proceso en su escritorio.

Si el agente NO está en Session 0 (corre interactivo, p.ej. en pruebas), no hace
falta nada: se ejecuta normal.

Solo Windows. Usa ctypes (sin dependencias externas).
"""
from __future__ import annotations
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes


# ── Constantes Win32 ───────────────────────────────────────────────
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100
TOKEN_ALL_FOR_PROCESS = (TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY |
                         TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID)

SecurityImpersonation = 2
TokenPrimary = 1

CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
NORMAL_PRIORITY_CLASS = 0x00000020

WTS_CURRENT_SERVER_HANDLE = 0
WTSActive = 0


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


# ── Firmas Win32 (CRÍTICO en 64-bit) ───────────────────────────────
# Sin argtypes, ctypes asume c_int (32 bits) por argumento y TRUNCA los HANDLE
# (punteros de 64 bits) → SetTokenInformation falla y el proceso cae en Session 0
# (sin escritorio: captura falla, no hay input, notificaciones invisibles).
def _declarar_firmas():
    try:
        adv = ctypes.windll.advapi32
        wts = ctypes.windll.wtsapi32
        uenv = ctypes.windll.userenv
        k32 = ctypes.windll.kernel32
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
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
    except Exception:
        pass


_declarar_firmas()


def en_session0() -> bool:
    try:
        sid = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return sid.value == 0
    except Exception:
        pass
    return False


def _sesion_activa() -> int | None:
    """Devuelve el ID de la sesión de consola activa con un usuario logueado."""
    sid = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    if sid != 0xFFFFFFFF and sid != 0:
        return sid
    # Fallback: enumerar sesiones y tomar la primera Activa (no Session 0)
    try:
        wtsapi = ctypes.windll.wtsapi32
        class WTS_SESSION_INFO(ctypes.Structure):
            _fields_ = [("SessionId", wintypes.DWORD),
                        ("pWinStationName", wintypes.LPWSTR),
                        ("State", ctypes.c_int)]
        pp = ctypes.POINTER(WTS_SESSION_INFO)()
        count = wintypes.DWORD()
        if wtsapi.WTSEnumerateSessionsW(WTS_CURRENT_SERVER_HANDLE, 0, 1,
                                        ctypes.byref(pp), ctypes.byref(count)):
            arr = ctypes.cast(pp, ctypes.POINTER(WTS_SESSION_INFO * count.value)).contents
            for s in arr:
                if s.State == WTSActive and s.SessionId != 0:
                    sel = s.SessionId
                    wtsapi.WTSFreeMemory(pp)
                    return sel
            wtsapi.WTSFreeMemory(pp)
    except Exception:
        pass
    return None


def ejecutar_en_sesion_usuario(cmdline: str, timeout: int = 35) -> bool:
    """Lanza `cmdline` en el escritorio del usuario logueado. Espera a que termine.
    Devuelve True si lanzó OK. Requiere privilegios de servicio (LocalSystem)."""
    sid = _sesion_activa()
    if sid is None:
        return False

    user_token = wintypes.HANDLE()
    if not ctypes.windll.wtsapi32.WTSQueryUserToken(sid, ctypes.byref(user_token)):
        return False

    dup_token = wintypes.HANDLE()
    ok = ctypes.windll.advapi32.DuplicateTokenEx(
        user_token, TOKEN_ALL_FOR_PROCESS, None, SecurityImpersonation,
        TokenPrimary, ctypes.byref(dup_token))
    ctypes.windll.kernel32.CloseHandle(user_token)
    if not ok:
        return False

    # CRÍTICO: forzar el SessionId del token a la sesión interactiva. Sin esto, el
    # proceso puede quedar en Session 0 (sin escritorio del usuario) → la
    # notificación se "muestra" pero el usuario no la ve. Con TokenSessionId el
    # helper corre en la sesión del usuario donde sí hay escritorio.
    TokenSessionId = 12
    sid_dword = wintypes.DWORD(sid)
    ctypes.windll.advapi32.SetTokenInformation(
        dup_token, TokenSessionId, ctypes.byref(sid_dword), ctypes.sizeof(sid_dword))

    # Bloque de entorno del usuario (para que el proceso tenga su perfil)
    env = ctypes.c_void_p()
    ctypes.windll.userenv.CreateEnvironmentBlock(ctypes.byref(env), dup_token, False)

    si = STARTUPINFO()
    si.cb = ctypes.sizeof(STARTUPINFO)
    si.lpDesktop = "winsta0\\default"   # escritorio interactivo del usuario
    pi = PROCESS_INFORMATION()

    flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | NORMAL_PRIORITY_CLASS
    created = ctypes.windll.advapi32.CreateProcessAsUserW(
        dup_token, None, ctypes.c_wchar_p(cmdline), None, None, False,
        flags, env, None, ctypes.byref(si), ctypes.byref(pi))

    if created:
        # esperar a que el helper termine (genera el archivo de captura)
        ctypes.windll.kernel32.WaitForSingleObject(pi.hProcess, timeout * 1000)
        ctypes.windll.kernel32.CloseHandle(pi.hProcess)
        ctypes.windll.kernel32.CloseHandle(pi.hThread)

    if env:
        ctypes.windll.userenv.DestroyEnvironmentBlock(env)
    ctypes.windll.kernel32.CloseHandle(dup_token)
    return bool(created)
