# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import copy_metadata

datas = [('runtime_models', 'runtime_models'), ('MekiCopy.ico', '.')]
binaries = []
hiddenimports = [
    'mekicopy',
    'onnxruntime.capi.onnxruntime_pybind11_state',
    'tkinter',
    'tkinter.constants',
    'tkinter.messagebox',
    'tkinter.simpledialog',
]
datas += collect_data_files('huggingface_hub')
datas += copy_metadata('meikiocr')
# OpenCV discovers its binary extension and FFmpeg helper at runtime. Collect
# the complete package explicitly instead of relying only on PyInstaller's
# optional hook discovery.
cv2_datas, cv2_binaries, cv2_hiddenimports = collect_all('cv2')
datas += cv2_datas
binaries += cv2_binaries
hiddenimports += cv2_hiddenimports
# Install onnxruntime-gpu last before building; its import package is onnxruntime.
binaries += collect_dynamic_libs('onnxruntime')
hiddenimports += collect_submodules('meikiocr')
hiddenimports += collect_submodules('onnxruntime.capi')


a = Analysis(
    ['meki_bootstrap.py'],
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
    upx=False,
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
    upx=False,
    upx_exclude=[],
    name='MekiCopy',
)
