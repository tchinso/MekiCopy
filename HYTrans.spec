# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from build_runtime_data import ssl_runtime_data
from release_packaging import browser_runtime_data

from PyInstaller.utils.hooks import collect_submodules

datas = ssl_runtime_data()
datas += browser_runtime_data(Path("assets"))
if Path("MekiCopy.ico").exists():
    datas.append(("MekiCopy.ico", "."))

hiddenimports = ["hytrans_main", "hytrans.hdtrans_backend", "hytrans.hdtrans_download"]
# hdTrans native payloads are deliberately staged beside HYTrans.exe by the
# Full packaging step. Never collect a warmed developer download into Lite.
hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("starlette")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("websockets")

a = Analysis(
    ["meki_bootstrap.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HYTrans",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="MekiCopy.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="HYTrans",
)
