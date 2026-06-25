# PyInstaller spec — empaqueta el RedBF Agent como un solo .exe portable
#
# Build:   pyinstaller redbf_agent.spec
# Output:  dist/redbf-agent.exe
#
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

block_cipher = None
AGENT_DIR = Path(SPECPATH)

a = Analysis(
    [str(AGENT_DIR / 'agente.py')],
    pathex=[str(AGENT_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # stdlib que PyInstaller a veces no autodetecta
        'sqlite3', 'urllib.request', 'urllib.error', 'configparser',
        'socket', 'platform', 'subprocess', 'json', 're', 'shutil', 'tempfile',
        # colectores (importados desde inventory.recolectar)
        'collectors.inventory',
        'collectors.perf',
        'collectors.navegacion',
        'collectors.pantalla',
        'collectors.session_helper',
        'collectors.control_remoto',
        'websockets',
        'websockets.client',
        'websockets.legacy',
        'websockets.legacy.client',
        # websockets 16 — connect() vive en el módulo asyncio nuevo
        'websockets.asyncio',
        'websockets.asyncio.client',
        'websockets.asyncio.connection',
        'ssl',
        # captura/stream usa PIL + mss (captura rápida DXGI)
        'PIL', 'PIL.Image', 'PIL.ImageGrab',
        'mss', 'mss.windows', 'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='redbf-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # consola para ver logs durante prueba; el servicio NSSM la oculta
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
