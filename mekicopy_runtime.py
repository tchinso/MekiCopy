from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from ctypes import wintypes

from PIL import Image, ImageTk
from runtime_paths import is_ascii_path, path_for_tcl, sync_tk_runtime, tk_runtime_roots

ICON_FILENAME = "MekiCopy.ico"
APP_USER_MODEL_ID = "MekiCopy.MekiCopy"
DETACHED_REGION_FILENAME = "detached_button_region.json"
DETACHED_GEOMETRY_FILENAME = "detached_button_geometry.json"

_DLL_DIR_HANDLES = []
_RUNTIME_PATH_READY = False
_WINDOW_STREAM = None
_APP_USER_MODEL_ID_READY = False

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
    for directory in (_get_resource_dir(), _get_app_dir()):
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
    if os.name != "nt":
        return

    resource_dir = _get_resource_dir()
    app_dir = _get_app_dir()
    if getattr(sys, "frozen", False):
        tcl_candidates = [
            os.path.join(resource_dir, "_tcl_data"),
            os.path.join(resource_dir, "tcl", "tcl8.6"),
            os.path.join(resource_dir, "MekiCopyRuntime", "tcl8.6"),
            os.path.join(app_dir, "MekiCopyRuntime", "tcl8.6"),
            os.path.join(sys.base_prefix, "tcl", "tcl8.6"),
        ]
        tk_candidates = [
            os.path.join(resource_dir, "_tk_data"),
            os.path.join(resource_dir, "tcl", "tk8.6"),
            os.path.join(resource_dir, "MekiCopyRuntime", "tk8.6"),
            os.path.join(app_dir, "MekiCopyRuntime", "tk8.6"),
            os.path.join(sys.base_prefix, "tcl", "tk8.6"),
        ]
    else:
        tcl_candidates = [
            os.path.join(sys.base_prefix, "tcl", "tcl8.6"),
            os.path.join(app_dir, "MekiCopyRuntime", "tcl8.6"),
        ]
        tk_candidates = [
            os.path.join(sys.base_prefix, "tcl", "tk8.6"),
            os.path.join(app_dir, "MekiCopyRuntime", "tk8.6"),
        ]
    source_tcl = next(
        (
            path
            for path in tcl_candidates
            if os.path.exists(os.path.join(path, "init.tcl"))
        ),
        None,
    )
    source_tk = next(
        (
            path
            for path in tk_candidates
            if os.path.exists(os.path.join(path, "tk.tcl"))
        ),
        None,
    )
    if not source_tcl or not source_tk:
        return

    def use_tk_paths(tcl_path: str, tk_path: str) -> bool:
        tcl_env = path_for_tcl(tcl_path)
        tk_env = path_for_tcl(tk_path)
        if not is_ascii_path(tcl_env) or not is_ascii_path(tk_env):
            return False
        safe_init = os.path.join(tcl_env, "init.tcl")
        safe_tk_script = os.path.join(tk_env, "tk.tcl")
        if os.path.exists(safe_init) and os.path.exists(safe_tk_script):
            os.environ["TCL_LIBRARY"] = tcl_env.replace("\\", "/")
            os.environ["TK_LIBRARY"] = tk_env.replace("\\", "/")
            return True
        return False

    if use_tk_paths(source_tcl, source_tk):
        return

    for safe_root_path in tk_runtime_roots("MekiCopyRuntime"):
        safe_root = path_for_tcl(safe_root_path)
        if not is_ascii_path(safe_root):
            continue
        try:
            safe_tcl, safe_tk = sync_tk_runtime(source_tcl, source_tk, safe_root)
            if use_tk_paths(str(safe_tcl), str(safe_tk)):
                return
        except OSError:
            pass

    use_tk_paths(source_tcl, source_tk)


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
    global _WINDOW_STREAM
    if sys.stderr is None:
        _WINDOW_STREAM = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = _WINDOW_STREAM
    if sys.stdout is None:
        sys.stdout = sys.stderr


BOOKMARKS_FILE = os.path.join(_get_app_dir(), "bookmarks.txt")
SETTINGS_FILE = os.path.join(_get_app_dir(), "settings.cfg")
DETACHED_REGION_FILE = os.path.join(_get_app_dir(), DETACHED_REGION_FILENAME)
DETACHED_GEOMETRY_FILE = os.path.join(_get_app_dir(), DETACHED_GEOMETRY_FILENAME)
