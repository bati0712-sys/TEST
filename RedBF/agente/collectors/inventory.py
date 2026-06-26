"""Colector de inventario de la PC — equivalente a NetSupport DNA.

Recolecta hardware, software, red y sistema usando WMI (vía wmic / PowerShell)
y módulos estándar. Sin dependencias externas pesadas: usa `wmic`, `reg query`
y `socket`/`platform` para funcionar en Windows Server 2012 → 11.

Devuelve un dict JSON-serializable que el agente envía al servidor.
"""
from __future__ import annotations
import os
import platform
import socket
import subprocess
import json
import re
from datetime import datetime, timezone


def _run(cmd: list[str], timeout: int = 30) -> str:
    """Ejecuta un comando y devuelve stdout (string). Silencioso ante errores."""
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
    """Ejecuta PowerShell y devuelve stdout."""
    return _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
    )


def _ps_json(script: str, timeout: int = 40):
    """Ejecuta PowerShell que emite JSON (ConvertTo-Json) y lo parsea."""
    raw = _ps(script + " | ConvertTo-Json -Compress -Depth 4", timeout=timeout)
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ── Identidad ──────────────────────────────────────────────────────
_MID_BASURA = {"", "ffffffff-ffff-ffff-ffff-ffffffffffff",
               "00000000-0000-0000-0000-000000000000",
               "03000200-0400-0500-0006-000700080009"}


def _mid_valido(v: str) -> bool:
    v = (v or "").strip().lower()
    return bool(v) and v not in _MID_BASURA and "to be filled" not in v and v.strip("0") != ""


def _uuid_smbios() -> str:
    """UUID SMBIOS por varias vías independientes (no solo PowerShell/CIM, que en
    algunas PCs de tienda falla o se cuelga). Orden: wmic → CIM → WMI clásico."""
    # 1) wmic (rápido, sin levantar PowerShell; existe en Win7→Win10, a veces no Win11)
    out = _run(["wmic", "csproduct", "get", "UUID", "/value"], timeout=15)
    for line in out.splitlines():
        if "=" in line:
            v = line.split("=", 1)[1].strip()
            if _mid_valido(v):
                return v.upper()
    # 2) CIM (PowerShell moderno)
    v = (_ps("(Get-CimInstance Win32_ComputerSystemProduct).UUID", timeout=20) or "").strip()
    if _mid_valido(v):
        return v.upper()
    # 3) WMI clásico (Get-WmiObject) — distinto subsistema, a veces responde cuando CIM no
    v = (_ps("(Get-WmiObject Win32_ComputerSystemProduct).UUID", timeout=20) or "").strip()
    if _mid_valido(v):
        return v.upper()
    return ""


def _machine_guid() -> str:
    """MachineGuid del registro (HKLM\\SOFTWARE\\Microsoft\\Cryptography). Es un GUID
    estable por instalación de Windows — NO sobrevive a reinstalar SO, pero sí a
    renombrar la PC. Lectura por registro: rapidísima y sin WMI."""
    out = _run(["reg", "query",
                r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"], timeout=10)
    m = re.search(r"MachineGuid\s+REG_SZ\s+([0-9a-fA-F-]{36})", out)
    if m and _mid_valido(m.group(1)):
        return m.group(1).upper()
    return ""


def _mac_fisica() -> str:
    """MAC del adaptador físico (fallback). Intenta PowerShell; si falla, usa
    getmac (nativo) y por último uuid.getnode() de Python — para no quedar sin ID."""
    macs = _ps(
        "Get-CimInstance Win32_NetworkAdapter -Filter 'PhysicalAdapter=True' | "
        "Where-Object { $_.MACAddress -and "
        "$_.Name -notmatch 'Virtual|VMware|Hyper-V|Loopback|VPN|TAP|Bluetooth' } | "
        "Select-Object -ExpandProperty MACAddress",
        timeout=20) or ""
    candidatas = sorted({m.strip().upper().replace("-", ":") for m in macs.splitlines()
                         if m.strip() and m.strip().upper() not in ("00:00:00:00:00:00",)})
    if not candidatas:
        # getmac nativo (no usa WMI). Formato: "00-E0-4C-..."
        out = _run(["getmac", "/fo", "csv", "/nh"], timeout=15)
        candidatas = sorted({
            mm.upper().replace("-", ":")
            for mm in re.findall(r"([0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5})", out)
            if mm.upper().replace("-", ":") != "00:00:00:00:00:00"})
    if candidatas:
        return f"MAC:{candidatas[0]}"
    # Último recurso: MAC del nodo vía Python puro (siempre da algo si hay NIC).
    try:
        import uuid as _uuidmod
        n = _uuidmod.getnode()
        if (n >> 40) % 2 == 0:  # bit multicast en 0 => MAC real, no aleatoria
            mac = ":".join(f"{(n >> (i*8)) & 0xff:02X}" for i in reversed(range(6)))
            if mac != "00:00:00:00:00:00":
                return f"MAC:{mac}"
    except Exception:
        pass
    return ""


def _machine_id() -> str:
    """Identificador ESTABLE de la máquina, independiente del hostname.

    Permite que el servidor reconozca una PC aunque la renombren (caso típico
    en tiendas) y actualice su registro en vez de crear un duplicado fantasma.

    Prioridad (cada nivel usa varias vías por si una falla en la PC):
      1) UUID SMBIOS (wmic/CIM/WMI) — sobrevive a reinstalar Windows.
      2) Serial de BIOS.
      3) MachineGuid del registro — estable salvo reinstalar SO.
      4) MAC del adaptador físico (PowerShell/getmac/Python).
    Antes solo usaba PowerShell-CIM; si esa vía fallaba (WMI lento/roto en algunas
    PCs de tienda) el id quedaba vacío y la PC perdía la protección anti-duplicado.
    """
    uuid = _uuid_smbios()
    if uuid:
        return uuid
    serie = (_run(["wmic", "bios", "get", "SerialNumber", "/value"], timeout=15) or "")
    for line in serie.splitlines():
        if "=" in line:
            v = line.split("=", 1)[1].strip()
            if _mid_valido(v):
                return f"BIOS:{v}"
    serie = (_ps("(Get-CimInstance Win32_BIOS).SerialNumber", timeout=20) or "").strip()
    if _mid_valido(serie):
        return f"BIOS:{serie}"
    guid = _machine_guid()
    if guid:
        return f"WGUID:{guid}"
    return _mac_fisica()


def identidad() -> dict:
    return {
        "hostname": socket.gethostname(),
        "machine_id": _machine_id(),
        "usuario_actual": os.environ.get("USERNAME", ""),
        "dominio": os.environ.get("USERDOMAIN", ""),
        "fecha_reporte": datetime.now(timezone.utc).isoformat(),
    }


# ── Sistema Operativo ──────────────────────────────────────────────
def sistema_operativo() -> dict:
    d = _ps_json(
        "Get-CimInstance Win32_OperatingSystem | "
        "Select-Object Caption,Version,BuildNumber,OSArchitecture,"
        "@{n='InstallDate';e={$_.InstallDate.ToString('yyyy-MM-dd')}},"
        "@{n='LastBoot';e={$_.LastBootUpTime.ToString('yyyy-MM-dd HH:mm')}},"
        "@{n='RAM_GB';e={[math]::Round($_.TotalVisibleMemorySize/1MB,1)}},"
        "@{n='RAM_Libre_GB';e={[math]::Round($_.FreePhysicalMemory/1MB,1)}}"
    ) or {}
    return {
        "nombre": d.get("Caption", platform.system()),
        "version": d.get("Version", platform.version()),
        "build": str(d.get("BuildNumber", "")),
        "arquitectura": d.get("OSArchitecture", platform.machine()),
        "fecha_instalacion": d.get("InstallDate", ""),
        "ultimo_arranque": d.get("LastBoot", ""),
        "ram_total_gb": d.get("RAM_GB", 0),
        "ram_libre_gb": d.get("RAM_Libre_GB", 0),
    }


# ── Hardware ───────────────────────────────────────────────────────
def hardware() -> dict:
    cs = _ps_json(
        "Get-CimInstance Win32_ComputerSystem | "
        "Select-Object Manufacturer,Model,@{n='RAM_GB';e={[math]::Round($_.TotalPhysicalMemory/1GB,1)}},NumberOfProcessors,NumberOfLogicalProcessors"
    ) or {}
    cpu = _ps_json(
        "Get-CimInstance Win32_Processor | Select-Object -First 1 Name,NumberOfCores,MaxClockSpeed"
    ) or {}
    bios = _ps_json(
        "Get-CimInstance Win32_BIOS | Select-Object SerialNumber,Manufacturer,"
        "@{n='Fecha';e={$_.ReleaseDate.ToString('yyyy-MM-dd')}}"
    ) or {}
    return {
        "fabricante": cs.get("Manufacturer", ""),
        "modelo": cs.get("Model", ""),
        "serie": (bios.get("SerialNumber") or "").strip(),
        "cpu": (cpu.get("Name") or "").strip(),
        "cpu_cores": cpu.get("NumberOfCores", 0),
        "cpu_logicos": cs.get("NumberOfLogicalProcessors", 0),
        "cpu_mhz": cpu.get("MaxClockSpeed", 0),
        "ram_gb": cs.get("RAM_GB", 0),
    }


# ── Discos ─────────────────────────────────────────────────────────
def discos() -> list[dict]:
    data = _ps_json(
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
        "Select-Object DeviceID,"
        "@{n='Total_GB';e={[math]::Round($_.Size/1GB,1)}},"
        "@{n='Libre_GB';e={[math]::Round($_.FreeSpace/1GB,1)}}"
    )
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        total = d.get("Total_GB", 0) or 0
        libre = d.get("Libre_GB", 0) or 0
        out.append({
            "unidad": d.get("DeviceID", ""),
            "total_gb": total,
            "libre_gb": libre,
            "usado_pct": round((total - libre) / total * 100, 1) if total else 0,
        })
    return out


# ── Red ────────────────────────────────────────────────────────────
def red() -> dict:
    def _local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return ""

    adapters = _ps_json(
        "Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=True' | "
        "Select-Object Description,MACAddress,DHCPEnabled,"
        "@{n='IP';e={($_.IPAddress | Where-Object {$_ -notmatch ':'}) -join ','}},"
        "@{n='Gateway';e={$_.DefaultIPGateway -join ','}},"
        "@{n='DNS';e={$_.DNSServerSearchOrder -join ','}},"
        "DHCPServer"
    )
    if isinstance(adapters, dict):
        adapters = [adapters]
    return {
        "ip_principal": _local_ip(),
        "adaptadores": [
            {
                "descripcion": a.get("Description", ""),
                "mac": a.get("MACAddress", ""),
                "ip": a.get("IP", ""),
                "gateway": a.get("Gateway", ""),
                "dns": a.get("DNS", ""),
                # DHCPEnabled=True → DHCP; False → IP estática
                "dhcp": bool(a.get("DHCPEnabled")),
                "tipo_ip": "DHCP" if a.get("DHCPEnabled") else "Estática",
                "dhcp_server": a.get("DHCPServer", ""),
            }
            for a in (adapters or [])
        ],
    }


# ── Software instalado ─────────────────────────────────────────────
def software() -> list[dict]:
    """Lee software instalado del registro (64 y 32 bits)."""
    script = (
        "$paths=@("
        "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*');"
        "Get-ItemProperty $paths -ErrorAction SilentlyContinue | "
        "Where-Object {$_.DisplayName} | "
        "Select-Object DisplayName,DisplayVersion,Publisher,InstallDate | "
        "Sort-Object DisplayName -Unique"
    )
    data = _ps_json(script, timeout=60)
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for s in data:
        name = (s.get("DisplayName") or "").strip()
        if not name:
            continue
        out.append({
            "nombre": name,
            "version": (s.get("DisplayVersion") or "").strip(),
            "fabricante": (s.get("Publisher") or "").strip(),
            "fecha": (s.get("InstallDate") or "").strip(),
        })
    return out


# ── Seguridad (relevante post-incidente malware) ───────────────────
def _decode_product_state(state: int) -> tuple[bool, bool]:
    """Decodifica el productState de SecurityCenter2 AntiVirusProduct.

    El valor (24 bits) se lee por bytes: [proveedor][estado][firmas].
      - byte de estado (0x__XX__): 0x10/0x11 = AV activo (RT protection on),
        0x00/0x01 = inactivo (ej. Defender pasivo cuando otro AV es el principal).
      - byte de firmas (0x____XX): bit 0x10 set = desactualizado.
    Devuelve (activo, firmas_al_dia).
    Ejemplos reales: Cyber Protect 266240=0x041000 → [04 10 00] activo, al día;
    Defender pasivo 393472=0x060100 → [06 01 00] inactivo, al día.
    """
    try:
        h = int(state) & 0xFFFFFF
    except Exception:
        return (None, None)
    estado_byte = (h >> 8) & 0xFF
    firmas_byte = h & 0xFF
    activo = estado_byte in (0x10, 0x11)
    al_dia = (firmas_byte & 0x10) == 0
    return (activo, al_dia)


def seguridad() -> dict:
    """Estado de antivirus (CUALQUIERA registrado: Defender, Acronis Cyber
    Protect, ESET, Kaspersky...) vía Windows Security Center, + firewall.

    Antes solo miraba Defender → falso positivo "AV apagado" en PCs que usan
    otro AV (ej. Acronis Cyber Protect). Ahora lee root\\SecurityCenter2 que
    centraliza TODOS los productos AV registrados.
    """
    productos = _ps_json(
        "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct "
        "-ErrorAction SilentlyContinue | Select-Object displayName, productState",
        timeout=30,
    )
    if isinstance(productos, dict):
        productos = [productos]
    productos = productos or []

    lista = []
    algun_activo = False
    nombres_vistos = set()
    for p in productos:
        nombre = (p.get("displayName") or "").strip()
        if not nombre or nombre.lower() in nombres_vistos:
            continue
        nombres_vistos.add(nombre.lower())
        activo, al_dia = _decode_product_state(p.get("productState", 0))
        if activo:
            algun_activo = True
        lista.append({"nombre": nombre, "activo": activo, "firmas_al_dia": al_dia})

    # Fallback: si Security Center no devolvió nada (Windows Server no lo trae),
    # consultar Defender directamente.
    if not lista:
        d = _ps_json(
            "Get-MpComputerStatus -ErrorAction SilentlyContinue | "
            "Select-Object AntivirusEnabled,RealTimeProtectionEnabled", timeout=20) or {}
        if d:
            activo = bool(d.get("AntivirusEnabled"))
            algun_activo = activo
            lista.append({"nombre": "Windows Defender", "activo": activo,
                          "firmas_al_dia": None})

    fw = _ps(
        "(Get-NetFirewallProfile -ErrorAction SilentlyContinue | "
        "Where-Object {$_.Enabled -eq $true}).Count", timeout=20
    ).strip()

    return {
        # antivirus_activo = True si AL MENOS un producto AV está activo
        "antivirus_activo": algun_activo if lista else None,
        "antivirus_productos": lista,          # detalle de cada AV registrado
        "antivirus_nombre": ", ".join(p["nombre"] for p in lista if p["activo"]) or
                            (", ".join(p["nombre"] for p in lista) if lista else ""),
        "firewall_perfiles_activos": int(fw) if fw.isdigit() else None,
    }


# ── Recolección completa ───────────────────────────────────────────
def recolectar(incluir_software: bool = True, incluir_navegacion: bool = True) -> dict:
    """Inventario completo de la máquina."""
    inv = {
        **identidad(),
        "so": sistema_operativo(),
        "hardware": hardware(),
        "discos": discos(),
        "red": red(),
        "seguridad": seguridad(),
    }
    if incluir_software:
        inv["software"] = software()
    if incluir_navegacion:
        try:
            from . import navegacion as _nav
            inv["navegacion"] = _nav.navegacion(desde_dias=30, top=40)
        except Exception as e:
            inv["navegacion"] = {"error": str(e)[:200]}
    return inv


if __name__ == "__main__":
    print(json.dumps(recolectar(), indent=2, ensure_ascii=False, default=str))
