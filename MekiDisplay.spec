# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

datas = [("MekiCopy.ico", ".")] if Path("MekiCopy.ico").exists() else []
common = dict(
    pathex=[], binaries=[], datas=datas,
    hiddenimports=["tkinter", "tkinter.constants"], hookspath=[],
    hooksconfig={}, runtime_hooks=[], excludes=["PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False, optimize=0,
)

overlayer = Analysis(["meki_overlayer.py"], **common)
script = Analysis(["meki_script.py"], **common)
MERGE((overlayer, "MekiOverlayer", "MekiOverlayer"), (script, "MekiScript", "MekiScript"))

overlayer_pyz = PYZ(overlayer.pure)
script_pyz = PYZ(script.pure)
overlayer_exe = EXE(
    overlayer_pyz, overlayer.scripts, [], exclude_binaries=True, name="MekiOverlayer",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True, console=False,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None, icon="MekiCopy.ico",
)
script_exe = EXE(
    script_pyz, script.scripts, [], exclude_binaries=True, name="MekiScript",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True, console=False,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None, icon="MekiCopy.ico",
)

coll = COLLECT(
    overlayer_exe, script_exe,
    overlayer.binaries, overlayer.datas, script.binaries, script.datas,
    strip=False, upx=True, upx_exclude=[], name="MekiDisplay",
)
