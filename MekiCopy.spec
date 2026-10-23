# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import copy_metadata
from build_runtime_data import ssl_runtime_data

# Optional native libraries and models are attached only to Full. Both
# distributions retain the same application executable and Python modules.
datas = [('MekiCopy.ico', '.'), ('assets/gpu_runtime_manifest.json', 'assets')]
datas += ssl_runtime_data()
binaries = []
hiddenimports = [
    'mekicopy',
    'audio_capture_core',
    'meki_subtitle_paths',
    'meki_subtitle_pipeline',
    'meki_subtitle_window',
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
# MekiSubtitle invokes the same native offline recognizers as
# MekiAudioCapture, including NeMo CTC for the default Parakeet model.
binaries += collect_dynamic_libs('sherpa_onnx')
hiddenimports += collect_submodules('sherpa_onnx')


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
# Hooks and native dependency scanning may discover installed CUDA libraries.
# These are supplied by the verified optional runtime installer or Full payload.
a.binaries = [entry for entry in a.binaries
              if not entry[0].replace('\\', '/').lower().startswith('nvidia/')
              and '/nvidia/' not in entry[1].replace('\\', '/').lower()]
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
