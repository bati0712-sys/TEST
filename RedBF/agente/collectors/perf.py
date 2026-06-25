"""Evaluación de rendimiento bajo demanda — RedBF.

Recolecta métricas EN VIVO de la PC (CPU, RAM, disco, procesos, arranque) en el
momento en que se ejecuta el comando DIAGNOSTICO, y arma un veredicto en lenguaje
claro de la causa probable de lentitud.

Mismo enfoque que inventory.py: PowerShell vía `_ps_json`, sin dependencias
externas, para funcionar de Windows Server 2012 a Windows 11.
"""
from __future__ import annotations
import json
import os
import socket
import subprocess
from datetime import datetime, timezone


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return out.stdout or ""
    except Exception:
        return ""


def _ps(script: str, timeout: int = 40) -> str:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=timeout)


def _ps_json(script: str, timeout: int = 40):
    raw = _ps(script + " | ConvertTo-Json -Compress -Depth 4", timeout=timeout).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _as_list(data):
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


# ── CPU ─────────────────────────────────────────────────────────────
def cpu() -> dict:
    """% de uso de CPU en el momento + top procesos por CPU.

    El % de procesador se mide con el contador de rendimiento (promedio sobre 2
    muestras de 1 s para que no salga 0 espurio)."""
    pct = _ps(
        "$c=(Get-Counter '\\Processor(_Total)\\% Processor Time' "
        "-SampleInterval 1 -MaxSamples 2 -ErrorAction SilentlyContinue)."
        "CounterSamples | Measure-Object CookedValue -Average; "
        "[math]::Round($c.Average,1)", timeout=20).strip()
    try:
        uso_pct = float(pct)
    except Exception:
        uso_pct = None

    # Top procesos por CPU acumulada. CPU = segundos de procesador consumidos;
    # se agrupa por nombre porque un programa puede tener varios procesos.
    top = _ps_json(
        "Get-Process | Group-Object ProcessName | "
        "Select-Object Name,"
        "@{n='CPU_s';e={[math]::Round((($_.Group | Measure-Object CPU -Sum).Sum),0)}},"
        "@{n='Procesos';e={$_.Count}} | "
        "Sort-Object CPU_s -Descending | Select-Object -First 8", timeout=25)
    return {
        "uso_pct": uso_pct,
        "top": [{"nombre": p.get("Name", ""), "cpu_segundos": p.get("CPU_s", 0),
                 "procesos": p.get("Procesos", 1)} for p in _as_list(top)],
    }


# ── RAM ─────────────────────────────────────────────────────────────
def ram() -> dict:
    os_d = _ps_json(
        "Get-CimInstance Win32_OperatingSystem | Select-Object "
        "@{n='Total_GB';e={[math]::Round($_.TotalVisibleMemorySize/1MB,2)}},"
        "@{n='Libre_GB';e={[math]::Round($_.FreePhysicalMemory/1MB,2)}}") or {}
    total = os_d.get("Total_GB", 0) or 0
    libre = os_d.get("Libre_GB", 0) or 0
    usado = round(total - libre, 2)
    usado_pct = round(usado / total * 100, 1) if total else None

    # Top procesos por memoria de trabajo (WorkingSet), agrupados por nombre.
    top = _ps_json(
        "Get-Process | Group-Object ProcessName | "
        "Select-Object Name,"
        "@{n='RAM_MB';e={[math]::Round((($_.Group | Measure-Object WorkingSet64 -Sum).Sum)/1MB,0)}},"
        "@{n='Procesos';e={$_.Count}} | "
        "Sort-Object RAM_MB -Descending | Select-Object -First 8", timeout=25)
    return {
        "total_gb": total, "libre_gb": libre, "usado_gb": usado,
        "usado_pct": usado_pct,
        "top": [{"nombre": p.get("Name", ""), "ram_mb": p.get("RAM_MB", 0),
                 "procesos": p.get("Procesos", 1)} for p in _as_list(top)],
    }


# ── Disco ───────────────────────────────────────────────────────────
def disco() -> dict:
    """Uso por unidad + cola de disco (clave en lentitud por HDD) + SSD/HDD."""
    unidades = _ps_json(
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID,"
        "@{n='Total_GB';e={[math]::Round($_.Size/1GB,1)}},"
        "@{n='Libre_GB';e={[math]::Round($_.FreeSpace/1GB,1)}}")
    out = []
    for d in _as_list(unidades):
        total = d.get("Total_GB", 0) or 0
        libre = d.get("Libre_GB", 0) or 0
        if not total:
            continue  # unidad sin medio (lector vacío) — no aporta al diagnóstico
        out.append({"unidad": d.get("DeviceID", ""), "total_gb": total, "libre_gb": libre,
                    "usado_pct": round((total - libre) / total * 100, 1)})

    # Cola promedio de disco (PhysicalDisk _Total). >2 sostenido = disco saturado.
    cola = _ps(
        "$c=(Get-Counter '\\PhysicalDisk(_Total)\\Avg. Disk Queue Length' "
        "-SampleInterval 1 -MaxSamples 2 -ErrorAction SilentlyContinue)."
        "CounterSamples | Measure-Object CookedValue -Average; "
        "[math]::Round($c.Average,2)", timeout=20).strip()
    try:
        cola_val = float(cola)
    except Exception:
        cola_val = None

    # Tipo de medio (SSD/HDD): MediaType de Get-PhysicalDisk (Win 8+/2012+).
    medios = _ps_json(
        "Get-PhysicalDisk -ErrorAction SilentlyContinue | "
        "Select-Object MediaType,@{n='Modelo';e={$_.FriendlyName}}")
    tipos = []
    for m in _as_list(medios):
        mt = (m.get("MediaType") or "").strip()
        if mt and mt not in ("Unspecified",):
            tipos.append(mt)
    tipo_medio = ", ".join(sorted(set(tipos))) if tipos else None

    return {"unidades": out, "cola_promedio": cola_val, "tipo_medio": tipo_medio}


# ── Arranque y sistema ──────────────────────────────────────────────
def sistema() -> dict:
    arranque = _ps_json(
        "Get-CimInstance Win32_StartupCommand -ErrorAction SilentlyContinue | "
        "Select-Object Name,Command,Location")
    arranque = _as_list(arranque)

    boot = _ps_json(
        "Get-CimInstance Win32_OperatingSystem | Select-Object "
        "@{n='LastBoot';e={$_.LastBootUpTime.ToString('yyyy-MM-dd HH:mm')}},"
        "@{n='Uptime_h';e={[math]::Round(((Get-Date)-$_.LastBootUpTime).TotalHours,1)}}") or {}

    # ¿Reinicio pendiente? (claves típicas de Windows Update / CBS)
    pend = _ps(
        "$p=$false;"
        "if(Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired'){$p=$true};"
        "if(Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending'){$p=$true};"
        "$p", timeout=15).strip().lower()

    return {
        "ultimo_arranque": boot.get("LastBoot", ""),
        "uptime_horas": boot.get("Uptime_h", 0),
        "arranque_cantidad": len(arranque),
        "arranque": [{"nombre": a.get("Name", ""), "comando": (a.get("Command") or "")[:200]}
                     for a in arranque[:30]],
        "reinicio_pendiente": pend == "true",
    }


# ── Veredicto ───────────────────────────────────────────────────────
# Umbrales — ajustables según el parque de PCs de la red.
CPU_ALTO = 85          # % uso sostenido
RAM_ALTO = 88          # % usado
DISCO_LLENO = 90       # % ocupado
COLA_ALTA = 2.0        # cola de disco promedio (saturación HDD)
ARRANQUE_MUCHO = 15    # programas en el inicio
UPTIME_MUCHO_H = 24 * 14  # 14 días sin reiniciar


def _fmt_top(items: list[dict], campo: str, sufijo: str, n: int = 3) -> str:
    partes = [f"{p['nombre']} ({p.get(campo, 0)}{sufijo})" for p in items[:n] if p.get("nombre")]
    return ", ".join(partes)


def veredicto(cpu_d: dict, ram_d: dict, disco_d: dict, sis_d: dict) -> dict:
    """Arma un diagnóstico en lenguaje claro. severidad: ok | aviso | critico."""
    hallazgos = []  # cada uno: {severidad, titulo}

    # RAM
    rp = ram_d.get("usado_pct")
    if rp is not None and rp >= RAM_ALTO:
        culpa = _fmt_top(ram_d.get("top", []), "ram_mb", " MB")
        hallazgos.append({"severidad": "critico",
                          "titulo": f"Memoria RAM saturada al {rp}%."
                          + (f" Mayor consumo: {culpa}." if culpa else "")})

    # CPU
    cp = cpu_d.get("uso_pct")
    if cp is not None and cp >= CPU_ALTO:
        culpa = _fmt_top(cpu_d.get("top", []), "cpu_segundos", "s")
        hallazgos.append({"severidad": "critico",
                          "titulo": f"CPU al {cp}% en este momento."
                          + (f" Procesos que más consumen: {culpa}." if culpa else "")})

    # Disco lleno
    for u in disco_d.get("unidades", []):
        if u.get("usado_pct", 0) >= DISCO_LLENO:
            sev = "critico" if u["usado_pct"] >= 95 else "aviso"
            hallazgos.append({"severidad": sev,
                              "titulo": f"Disco {u['unidad']} {u['usado_pct']}% lleno "
                              f"(quedan {u.get('libre_gb', 0)} GB)."})

    # Cola de disco (saturación de E/S — típico en HDD lentos)
    cola = disco_d.get("cola_promedio")
    if cola is not None and cola >= COLA_ALTA:
        es_hdd = (disco_d.get("tipo_medio") or "").upper().find("HDD") >= 0
        extra = " El disco es HDD mecánico; considerar SSD." if es_hdd else ""
        hallazgos.append({"severidad": "aviso",
                          "titulo": f"Disco saturado de lectura/escritura "
                          f"(cola {cola}).{extra}"})

    # Arranque cargado
    ac = sis_d.get("arranque_cantidad", 0)
    if ac >= ARRANQUE_MUCHO:
        hallazgos.append({"severidad": "aviso",
                          "titulo": f"{ac} programas en el arranque — el inicio de "
                          f"sesión y el rendimiento general se ven afectados."})

    # Mucho tiempo sin reiniciar
    up = sis_d.get("uptime_horas", 0) or 0
    if up >= UPTIME_MUCHO_H:
        dias = round(up / 24, 1)
        hallazgos.append({"severidad": "aviso",
                          "titulo": f"{dias} días sin reiniciar — reiniciar suele "
                          f"liberar memoria y procesos colgados."})

    if sis_d.get("reinicio_pendiente"):
        hallazgos.append({"severidad": "aviso",
                          "titulo": "Hay un reinicio pendiente de Windows Update."})

    if not hallazgos:
        return {"severidad": "ok",
                "resumen": "Sin causas evidentes de lentitud. CPU, RAM y disco en rangos normales.",
                "hallazgos": []}

    severidad = "critico" if any(h["severidad"] == "critico" for h in hallazgos) else "aviso"
    resumen = hallazgos[0]["titulo"]  # el primero (RAM>CPU>disco) suele ser la causa principal
    return {"severidad": severidad, "resumen": resumen, "hallazgos": hallazgos}


# ── Evaluación completa ─────────────────────────────────────────────
def evaluar() -> dict:
    """Recolecta todas las métricas en vivo y devuelve el reporte con veredicto."""
    cpu_d = cpu()
    ram_d = ram()
    disco_d = disco()
    sis_d = sistema()
    return {
        "hostname": socket.gethostname(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "cpu": cpu_d,
        "ram": ram_d,
        "disco": disco_d,
        "sistema": sis_d,
        "veredicto": veredicto(cpu_d, ram_d, disco_d, sis_d),
    }


if __name__ == "__main__":
    print(json.dumps(evaluar(), indent=2, ensure_ascii=False, default=str))
