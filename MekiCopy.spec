# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import copy_metadata

# Full carries the OCR files locally. Lite intentionally has no model files;
# MeikiOCR then uses its ordinary first-run Hugging Face download/cache path.
datas = [('MekiCopy.ico', '.')]
if os.environ.get('MEKICOPY_PACKAGE_FLAVOR', 'lite').casefold() == 'full':
    datas.insert(0, ('runtime_models', 'runtime_models'))
# Video subtitle generation uses the existing ReazonSubtitle FFmpeg payload,
# but only model caches are shared at runtime.  Keep this media utility in the
# MekiCopy bundle so a release does not depend on a developer-side PATH.
spec_root = Path(SPECPATH).resolve()
subtitle_ffmpeg = spec_root.parent / 'ReazonSubtitle' / 'assets' / 'ffmpeg'
if subtitle_ffmpeg.is_dir():
    datas.append((str(subtitle_ffmpeg), 'assets/ffmpeg'))
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
# The CUDA provider DLL is not self-contained. ONNX Runtime 1.30's CUDA 13
# extras install the CUDA libraries under nvidia/cu13/bin/x86_64 and cuDNN
# under nvidia/cudnn/bin. Preserve these package-relative paths because
# onnxruntime.preload_dlls() searches that layout in a frozen bundle.
for _gpu_runtime_package in ('nvidia.cu13', 'nvidia.cudnn'):
    binaries += collect_dynamic_libs(_gpu_runtime_package)
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
