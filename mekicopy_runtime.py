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
_DLL_DIRECTORIES: set[str] = set()
_RUNTIME_PATH_READY = False
_APP_USER_MODEL_ID_READY = False

# CUDA and cuDNN directories packaged with the Full runtime.
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

    _register_native_dll_directories(candidate_dirs)
    _RUNTIME_PATH_READY = True


def _register_native_dll_directories(directories) -> None:
    """Keep DLL search handles alive for bundled or cached runtime libraries."""
    path_items = os.environ.get("PATH", "").split(os.pathsep)
    for directory in directories:
        directory = os.path.abspath(os.fspath(directory))
        key = os.path.normcase(directory)
        if key in _DLL_DIRECTORIES or not os.path.isdir(directory):
            continue
        if directory not in path_items:
            path_items.insert(0, directory)
        if hasattr(os, "add_dll_directory"):
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(directory))
            except OSError:
                continue
        _DLL_DIRECTORIES.add(key)
    os.environ["PATH"] = os.pathsep.join(path_items)


def _cuda_driver_is_usable() -> bool:
    """Require a CUDA 13 driver and an accessible device before downloading."""
    if os.name != "nt":
        return False
    try:
        driver = ctypes.WinDLL("nvcuda.dll")
        driver.cuInit.argtypes = [ctypes.c_uint]
        driver.cuInit.restype = ctypes.c_int
        driver.cuDriverGetVersion.argtypes = [ctypes.POINTER(ctypes.c_int)]
        driver.cuDriverGetVersion.restype = ctypes.c_int
        driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        driver.cuDeviceGetCount.restype = ctypes.c_int
        if driver.cuInit(0) != 0:
            return False
        version = ctypes.c_int()
        count = ctypes.c_int()
        return (driver.cuDriverGetVersion(ctypes.byref(version)) == 0
                and version.value >= 13000
                and driver.cuDeviceGetCount(ctypes.byref(count)) == 0
                and count.value > 0)
    except (OSError, AttributeError):
        return False


def _prepare_windowed_streams() -> None:
    from system_logging import capture_windowed_streams

    capture_windowed_streams()


_STATE_DATA_DIR = state_data_dir("MekiCopy")
BOOKMARKS_FILE = str(_STATE_DATA_DIR / "bookmarks.txt")
SETTINGS_FILE = str(_STATE_DATA_DIR / "settings.cfg")
DETACHED_REGION_FILE = str(_STATE_DATA_DIR / DETACHED_REGION_FILENAME)
DETACHED_GEOMETRY_FILE = str(_STATE_DATA_DIR / DETACHED_GEOMETRY_FILENAME)
