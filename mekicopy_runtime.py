from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from ctypes import wintypes

from PIL import Image, ImageTk
from runtime_paths import prepare_tk_environment, state_data_dir

ICON_FILENAME = "MekiCopy.ico"
APP_USER_MODEL_ID = "MekiCopy.MekiCopy"
DETACHED_REGION_FILENAME = "detached_button_region.json"
DETACHED_GEOMETRY_FILENAME = "detached_button_geometry.json"

_DLL_DIR_HANDLES = []
_RUNTIME_PATH_READY = False
_APP_USER_MODEL_ID_READY = False

# ``onnxruntime-gpu[cuda,cudnn]`` 1.30 uses CUDA 13's consolidated ``cu13``
# layout. PyInstaller preserves it, so add the directories explicitly before
# importing ONNX Runtime. A display driver alone does not provide the
# CUDA/cuDNN libraries required by the CUDA EP.
_NVIDIA_RUNTIME_DLL_SUBDIRECTORIES = (
    os.path.join("nvidia", "cu13", "bin", "x86_64"),
    os.path.join("nvidia", "cudnn", "bin"),
)


def _get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_resource_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", _get_app_dir())
    return os.path.dirname(os.path.abspath(__file__))


def _set_app_user_model_id() -> None:
    global _APP_USER_MODEL_ID_READY
    if _APP_USER_MODEL_ID_READY or os.name != "nt":
        return
    _APP_USER_MODEL_ID_READY = True
    try:
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def _get_icon_path() -> str | None:
    for directory in (_get_app_dir(), _get_resource_dir()):
        candidate = os.path.join(directory, ICON_FILENAME)
        if os.path.exists(candidate):
            return candidate
    return None


def _set_window_icon(window: tk.Misc) -> None:
    icon_path = _get_icon_path()
    if not icon_path:
        return
    try:
        window.iconbitmap(default=icon_path)
    except tk.TclError:
        try:
            window.iconbitmap(icon_path)
        except tk.TclError:
            pass
    try:
        with Image.open(icon_path) as icon_image:
            icon_image.seek(0)
            photo = ImageTk.PhotoImage(icon_image.copy())
        window.iconphoto(True, photo)
        setattr(window, "_mekicopy_icon_photo", photo)
    except Exception:
        pass


def _prepare_tk_library_paths() -> None:
    prepare_tk_environment("MekiCopyRuntime")


def _prepare_native_runtime_paths() -> None:
    global _RUNTIME_PATH_READY
    if _RUNTIME_PATH_READY:
        return

    candidate_dirs = [
        _get_resource_dir(),
        _get_app_dir(),
        os.path.join(_get_resource_dir(), "onnxruntime", "capi"),
        os.path.join(_get_app_dir(), "onnxruntime", "capi"),
        os.path.join(_get_resource_dir(), "numpy.libs"),
        os.path.join(_get_app_dir(), "numpy.libs"),
        os.path.join(_get_resource_dir(), "cv2"),
        os.path.join(_get_app_dir(), "cv2"),
    ]
    for root in (_get_resource_dir(), _get_app_dir()):
        candidate_dirs.extend(
            os.path.join(root, relative)
            for relative in _NVIDIA_RUNTIME_DLL_SUBDIRECTORIES
        )

    existing_dirs: list[str] = []
    for directory in candidate_dirs:
        if os.path.isdir(directory) and directory not in existing_dirs:
            existing_dirs.append(directory)

    path_items = os.environ.get("PATH", "").split(os.pathsep) if os.environ.get("PATH") else []
    for directory in existing_dirs:
        if directory not in path_items:
            path_items.insert(0, directory)
    os.environ["PATH"] = os.pathsep.join(path_items)

    if hasattr(os, "add_dll_directory"):
        for directory in existing_dirs:
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(directory))
            except OSError:
                continue
    _RUNTIME_PATH_READY = True


def _prepare_windowed_streams() -> None:
    from system_logging import capture_windowed_streams

    capture_windowed_streams()


_STATE_DATA_DIR = state_data_dir("MekiCopy")
BOOKMARKS_FILE = str(_STATE_DATA_DIR / "bookmarks.txt")
SETTINGS_FILE = str(_STATE_DATA_DIR / "settings.cfg")
DETACHED_REGION_FILE = str(_STATE_DATA_DIR / DETACHED_REGION_FILENAME)
DETACHED_GEOMETRY_FILE = str(_STATE_DATA_DIR / DETACHED_GEOMETRY_FILENAME)
