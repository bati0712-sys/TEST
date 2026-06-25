"""Colector de navegación web — RESUMEN POR DOMINIO (no URLs individuales).

Enfoque de gestión, no vigilancia granular: agrega el historial de los
navegadores por dominio → {dominio, visitas, ultima_vez}. Útil para ver uso
("se navega mucho YouTube en horario laboral") sin guardar cada URL.

Soporta navegadores Chromium (Chrome, Brave, Edge — mismo esquema SQLite) y
Firefox (places.sqlite). Copia la BD a un temporal antes de leer (los
navegadores la bloquean mientras están abiertos).

Privacidad: requiere política de uso aceptable informada a los empleados.
"""
from __future__ import annotations
import os
import sqlite3
import shutil
import tempfile
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from collections import defaultdict

# Epoch de Chrome: microsegundos desde 1601-01-01
_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _dominio(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _copiar_y_abrir(db_path: Path) -> sqlite3.Connection | None:
    """Copia la BD a un temporal (los navegadores la bloquean) y la abre."""
    try:
        tmp = Path(tempfile.gettempdir()) / f"_redbf_{os.getpid()}_{db_path.parent.name}_{db_path.name}.db"
        shutil.copy2(db_path, tmp)
        return sqlite3.connect(f"file:{tmp}?mode=ro", uri=True, timeout=10)
    except Exception:
        return None


def _perfiles_chromium(user_home: Path) -> list[tuple[str, Path]]:
    """Devuelve (navegador, ruta History) de todos los perfiles Chromium."""
    bases = {
        "Chrome": user_home / "AppData/Local/Google/Chrome/User Data",
        "Brave":  user_home / "AppData/Local/BraveSoftware/Brave-Browser/User Data",
        "Edge":   user_home / "AppData/Local/Microsoft/Edge/User Data",
    }
    out = []
    for nav, base in bases.items():
        if not base.exists():
            continue
        for prof in base.iterdir():
            if prof.is_dir() and prof.name not in ("System Profile", "Guest Profile"):
                h = prof / "History"
                if h.exists():
                    out.append((nav, h))
    return out


def _leer_chromium(db_path: Path, desde_dias: int) -> dict[str, dict]:
    conn = _copiar_y_abrir(db_path)
    if not conn:
        return {}
    agregado: dict[str, dict] = {}
    try:
        # last_visit_time en microsegundos desde 1601
        limite_us = int((datetime.now(timezone.utc) - timedelta(days=desde_dias) - _CHROME_EPOCH).total_seconds() * 1_000_000)
        cur = conn.execute(
            "SELECT url, visit_count, last_visit_time FROM urls "
            "WHERE last_visit_time > ? ", (limite_us,)
        )
        for url, vc, lvt in cur.fetchall():
            d = _dominio(url)
            if not d:
                continue
            try:
                last = (_CHROME_EPOCH + timedelta(microseconds=lvt)).date().isoformat()
            except Exception:
                last = ""
            a = agregado.setdefault(d, {"visitas": 0, "ultima": ""})
            a["visitas"] += int(vc or 1)
            if last > a["ultima"]:
                a["ultima"] = last
    except Exception:
        pass
    finally:
        conn.close()
    return agregado


def _leer_firefox(user_home: Path, desde_dias: int) -> dict[str, dict]:
    base = user_home / "AppData/Roaming/Mozilla/Firefox/Profiles"
    if not base.exists():
        return {}
    agregado: dict[str, dict] = {}
    for prof in base.iterdir():
        places = prof / "places.sqlite"
        if not places.exists():
            continue
        conn = _copiar_y_abrir(places)
        if not conn:
            continue
        try:
            # Firefox: last_visit_date en microsegundos desde 1970
            limite_us = int((datetime.now(timezone.utc) - timedelta(days=desde_dias)).timestamp() * 1_000_000)
            cur = conn.execute(
                "SELECT url, visit_count, last_visit_date FROM moz_places "
                "WHERE last_visit_date > ?", (limite_us,)
            )
            for url, vc, lvd in cur.fetchall():
                d = _dominio(url)
                if not d:
                    continue
                try:
                    last = datetime.fromtimestamp(lvd / 1_000_000, timezone.utc).date().isoformat() if lvd else ""
                except Exception:
                    last = ""
                a = agregado.setdefault(d, {"visitas": 0, "ultima": ""})
                a["visitas"] += int(vc or 1)
                if last > a["ultima"]:
                    a["ultima"] = last
        except Exception:
            pass
        finally:
            conn.close()
    return agregado


def navegacion(desde_dias: int = 30, top: int = 40) -> dict:
    """Resumen de navegación por dominio de los últimos N días.

    Recorre TODOS los perfiles de usuario de la máquina (C:\\Users\\*), no solo
    el actual — útil en PCs compartidas de tienda.
    """
    users_dir = Path(os.environ.get("SystemDrive", "C:") + "\\Users")
    total: dict[str, dict] = defaultdict(lambda: {"visitas": 0, "ultima": ""})

    homes = []
    if users_dir.exists():
        for u in users_dir.iterdir():
            if u.is_dir() and u.name not in ("Public", "Default", "Default User", "All Users"):
                homes.append(u)
    if not homes:
        homes = [Path(os.path.expanduser("~"))]

    for home in homes:
        fuentes = _perfiles_chromium(home)
        parciales = [_leer_chromium(h, desde_dias) for _, h in fuentes]
        parciales.append(_leer_firefox(home, desde_dias))
        for ag in parciales:
            for dom, info in ag.items():
                t = total[dom]
                t["visitas"] += info["visitas"]
                if info["ultima"] > t["ultima"]:
                    t["ultima"] = info["ultima"]

    ranking = sorted(
        ({"dominio": d, "visitas": v["visitas"], "ultima": v["ultima"]} for d, v in total.items()),
        key=lambda x: x["visitas"], reverse=True,
    )
    return {
        "dias": desde_dias,
        "dominios_unicos": len(ranking),
        "total_visitas": sum(r["visitas"] for r in ranking),
        "top_dominios": ranking[:top],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(navegacion(desde_dias=30, top=20), indent=2, ensure_ascii=False))
