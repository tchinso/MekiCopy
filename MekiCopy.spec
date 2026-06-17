# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import tcl_tk

datas = [('runtime_models', 'runtime_models'), ('MekiCopy.ico', '.')]
binaries = []
hiddenimports = [
    'onnxruntime.capi.onnxruntime_pybind11_state',
    'tkinter',
    'tkinter.constants',
    'tkinter.messagebox',
    'tkinter.simpledialog',
]
python_root = Path(sys.base_prefix)
tcl_tk.tcltk_info.available = True
tcl_tk.tcltk_info.data_files = []
tcl_dir = python_root / 'tcl'
if tcl_dir.exists():
    datas.append((str(tcl_dir), 'tcl'))
    tcl_library_dir = tcl_dir / 'tcl8.6'
    tk_library_dir = tcl_dir / 'tk8.6'
    if tcl_library_dir.exists():
        datas.append((str(tcl_library_dir), '_tcl_data'))
    if tk_library_dir.exists():
        datas.append((str(tk_library_dir), '_tk_data'))
for dll_name in ('tcl86t.dll', 'tk86t.dll', '_tkinter.pyd'):
    dll_path = python_root / 'DLLs' / dll_name
    if dll_path.exists():
        binaries.append((str(dll_path), '.'))
datas += collect_data_files('huggingface_hub')
# Install onnxruntime-gpu last before building; its import package is onnxruntime.
binaries += collect_dynamic_libs('onnxruntime')
hiddenimports += collect_submodules('meikiocr')
hiddenimports += collect_submodules('onnxruntime.capi')


a = Analysis(
    ['mekicopy.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'pyperclip'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MekiCopy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='MekiCopy.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MekiCopy',
)
