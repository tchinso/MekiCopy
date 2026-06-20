import argparse
import configparser
import ctypes
import datetime as _dt
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import time
import zipfile

from runtime_paths import (
    is_ascii_path,
    path_for_tcl,
    prepare_tk_environment,
    sync_tk_runtime,
    tk_runtime_roots,
)

prepare_tk_environment("MekiCopyRuntime")

import tkinter as tk
from dataclasses import dataclass
from tkinter import colorchooser, font as tkfont, messagebox, simpledialog
from ctypes import wintypes
from typing import Callable
import traceback

import mss
from PIL import Image, ImageTk
from mekicopy_capture import (
    MIN_SIZE_PX,
    CaptureStatus,
    MonitorInfo,
    Region,
    RegionRatio,
    capture_problem_message as _capture_problem_message,
    capture_region_result,
    clamp_region_to_monitor as _clamp_region_to_monitor,
    configure_capture_runtime,
    enable_dpi_awareness as _enable_dpi_awareness,
    find_monitor_for_region as _find_monitor_for_region,
    get_capture_manager,
    match_monitor_by_previous_rect as _match_monitor_by_previous_rect,
    monitor_signature as _monitor_signature,
    ratio_for_region as _ratio_for_region,
    region_from_ratio as _region_from_ratio,
    regions_equal as _regions_equal,
    run_capture_diagnostics,
)
EDGE_GRAB_PX = 8
OCR_BUTTON_HEIGHT_PX = 300
SELECTION_INSTRUCTION_FONT_SIZE = 36
DETACHED_DEFAULT_GEOMETRY = "260x160+120+120"
ICON_FILENAME = "MekiCopy.ico"
APP_USER_MODEL_ID = "MekiCopy.MekiCopy"
DETACHED_WINDOW_TITLE = "MekiCopy - 분리 버튼"
DETACHED_REGION_FILENAME = "detached_button_region.json"
DETACHED_GEOMETRY_FILENAME = "detached_button_geometry.json"
DETACHED_MUTEX_NAME = "Local\\MekiCopy.DetachedOcrButton"
KOREAN_FONT_TEST_CHARACTER = "쿈"
HYTRANS_DEFAULT_PORT = 6550
OVERLAYER_DEFAULT_PORT = 6551
MAGPIE_RELEASE_API_URL = "https://api.github.com/repos/Blinue/Magpie/releases/latest"
MAGPIE_LAUNCH_NOTICE = (
    "데스크톱 캡쳐 모드는 Graphics Capture(1순위 권장) 또는 "
    "GDI(2순위 권장)을 사용해주세요"
)
_OCR_ENGINE = None
_DLL_DIR_HANDLES = []
_RUNTIME_PATH_READY = False
_WINDOW_STREAM = None
_ORT_PRELOAD_READY = False
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
        except OSError as exc:
            _log_runtime_error("prepare_tk_library_paths", exc)

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


@dataclass
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def normalized(self) -> "Rect":
        left = min(self.left, self.right)
        right = max(self.left, self.right)
        top = min(self.top, self.bottom)
        bottom = max(self.top, self.bottom)
        return Rect(left, top, right, bottom)


@dataclass
class Bookmark:
    name: str
    left: int
    top: int
    width: int
    height: int


@dataclass
class AppSettings:
    minimize_to_tray: bool = True
    main_always_on_top: bool = False
    detached_always_on_top: bool = True
    detached_hide_titlebar: bool = False
    detached_fixed_size: bool = False
    simple_copy_complete: bool = True
    detached_geometry: str = DETACHED_DEFAULT_GEOMETRY
    detached_fixed_width: int = 260
    detached_fixed_height: int = 160
    overlay_translation_mode: bool = True
    hytrans_port: int = HYTRANS_DEFAULT_PORT
    overlayer_port: int = OVERLAYER_DEFAULT_PORT
    overlayer_always_on_top: bool = True
    overlayer_hide_titlebar: bool = False
    overlayer_fixed_size: bool = False
    overlayer_exclude_from_capture: bool = True
    overlayer_bg_color: str = "#111111"
    overlayer_bg_opacity: float = 0.78
    overlayer_text_color: str = "#ffffff"
    overlayer_text_size: int = 28
    overlayer_text_font: str = "Malgun Gothic"
    suppress_magpie_launch_notice: bool = False
    debug_logging: bool = False


def _normalize_port(port: int, fallback: int) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError):
        value = fallback
    return max(1, min(65535, value))


def _alternate_port(blocked_port: int) -> int:
    for candidate in (OVERLAYER_DEFAULT_PORT, HYTRANS_DEFAULT_PORT, 6552, 6553):
        if candidate != blocked_port:
            return candidate
    return 65535 if blocked_port != 65535 else 65534


def _normalize_font_name(font_name: str) -> str:
    normalized = str(font_name).strip().lstrip("@").strip()
    return normalized or "Malgun Gothic"


def _font_has_character(font_name: str, character: str = KOREAN_FONT_TEST_CHARACTER) -> bool:
    """Return whether a Windows font contains the requested character glyph."""
    if os.name != "nt" or not font_name or not character:
        return False

    gdi32 = ctypes.windll.gdi32
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateFontW.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
    ]
    gdi32.CreateFontW.restype = wintypes.HANDLE
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi32.SelectObject.restype = wintypes.HANDLE
    gdi32.GetGlyphIndicesW.argtypes = [
        wintypes.HDC,
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.POINTER(wintypes.WORD),
        wintypes.DWORD,
    ]
    gdi32.GetGlyphIndicesW.restype = wintypes.DWORD
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    hdc = gdi32.CreateCompatibleDC(None)
    if not hdc:
        return False

    font_handle = None
    old_font = None
    try:
        font_handle = gdi32.CreateFontW(
            -16,
            0,
            0,
            0,
            400,
            0,
            0,
            0,
            1,  # DEFAULT_CHARSET
            0,
            0,
            0,
            0,
            _normalize_font_name(font_name),
        )
        if not font_handle:
            return False
        old_font = gdi32.SelectObject(hdc, font_handle)
        glyph_indices = (wintypes.WORD * len(character))()
        result = gdi32.GetGlyphIndicesW(
            hdc,
            character,
            len(character),
            glyph_indices,
            0x0001,  # GGI_MARK_NONEXISTING
        )
        return result != 0xFFFFFFFF and all(index != 0xFFFF for index in glyph_indices)
    finally:
        if old_font:
            gdi32.SelectObject(hdc, old_font)
        if font_handle:
            gdi32.DeleteObject(font_handle)
        gdi32.DeleteDC(hdc)


def _korean_font_families(root: tk.Misc) -> list[str]:
    font_names = sorted({_normalize_font_name(name) for name in tkfont.families(root) if name})
    return [name for name in font_names if _font_has_character(name)]


def load_settings() -> AppSettings:
    settings = AppSettings()
    parser = configparser.ConfigParser()
    try:
        parser.read(SETTINGS_FILE, encoding="utf-8")
    except configparser.Error:
        return settings

    section = "settings"
    if not parser.has_section(section):
        return settings

    settings.minimize_to_tray = parser.getboolean(
        section, "minimize_to_tray", fallback=settings.minimize_to_tray
    )
    settings.main_always_on_top = parser.getboolean(
        section, "main_always_on_top", fallback=settings.main_always_on_top
    )
    settings.detached_always_on_top = parser.getboolean(
        section, "detached_always_on_top", fallback=settings.detached_always_on_top
    )
    settings.detached_hide_titlebar = parser.getboolean(
        section, "detached_hide_titlebar", fallback=settings.detached_hide_titlebar
    )
    settings.detached_fixed_size = parser.getboolean(
        section, "detached_fixed_size", fallback=settings.detached_fixed_size
    )
    settings.simple_copy_complete = parser.getboolean(
        section, "simple_copy_complete", fallback=settings.simple_copy_complete
    )
    settings.detached_geometry = parser.get(
        section, "detached_geometry", fallback=settings.detached_geometry
    )
    settings.detached_fixed_width = parser.getint(
        section, "detached_fixed_width", fallback=settings.detached_fixed_width
    )
    settings.detached_fixed_height = parser.getint(
        section, "detached_fixed_height", fallback=settings.detached_fixed_height
    )
    settings.overlay_translation_mode = parser.getboolean(
        section,
        "overlay_translation_mode",
        fallback=settings.overlay_translation_mode,
    )
    settings.hytrans_port = parser.getint(
        section, "hytrans_port", fallback=settings.hytrans_port
    )
    settings.overlayer_port = parser.getint(
        section, "overlayer_port", fallback=settings.overlayer_port
    )
    settings.overlayer_always_on_top = parser.getboolean(
        section,
        "overlayer_always_on_top",
        fallback=settings.overlayer_always_on_top,
    )
    settings.overlayer_hide_titlebar = parser.getboolean(
        section,
        "overlayer_hide_titlebar",
        fallback=settings.overlayer_hide_titlebar,
    )
    settings.overlayer_fixed_size = parser.getboolean(
        section,
        "overlayer_fixed_size",
        fallback=settings.overlayer_fixed_size,
    )
    settings.overlayer_exclude_from_capture = parser.getboolean(
        section,
        "overlayer_exclude_from_capture",
        fallback=settings.overlayer_exclude_from_capture,
    )
    settings.overlayer_bg_color = parser.get(
        section, "overlayer_bg_color", fallback=settings.overlayer_bg_color
    )
    settings.overlayer_bg_opacity = parser.getfloat(
        section,
        "overlayer_bg_opacity",
        fallback=settings.overlayer_bg_opacity,
    )
    settings.overlayer_text_color = parser.get(
        section, "overlayer_text_color", fallback=settings.overlayer_text_color
    )
    settings.overlayer_text_size = parser.getint(
        section, "overlayer_text_size", fallback=settings.overlayer_text_size
    )
    settings.overlayer_text_font = parser.get(
        section, "overlayer_text_font", fallback=settings.overlayer_text_font
    )
    settings.suppress_magpie_launch_notice = parser.getboolean(
        section,
        "suppress_magpie_launch_notice",
        fallback=settings.suppress_magpie_launch_notice,
    )
    settings.debug_logging = parser.getboolean(
        section, "debug_logging", fallback=settings.debug_logging
    )
    settings.hytrans_port = _normalize_port(settings.hytrans_port, HYTRANS_DEFAULT_PORT)
    settings.overlayer_port = _normalize_port(
        settings.overlayer_port, OVERLAYER_DEFAULT_PORT
    )
    if settings.hytrans_port == settings.overlayer_port:
        settings.overlayer_port = _alternate_port(settings.hytrans_port)
    settings.overlayer_bg_opacity = max(0.1, min(1.0, settings.overlayer_bg_opacity))
    settings.overlayer_text_size = max(8, min(96, settings.overlayer_text_size))
    settings.overlayer_text_font = _normalize_font_name(settings.overlayer_text_font)
    return settings


def save_settings(settings: AppSettings) -> None:
    parser = configparser.ConfigParser()
    parser["settings"] = {
        "minimize_to_tray": str(settings.minimize_to_tray).lower(),
        "main_always_on_top": str(settings.main_always_on_top).lower(),
        "detached_always_on_top": str(settings.detached_always_on_top).lower(),
        "detached_hide_titlebar": str(settings.detached_hide_titlebar).lower(),
        "detached_fixed_size": str(settings.detached_fixed_size).lower(),
        "simple_copy_complete": str(settings.simple_copy_complete).lower(),
        "detached_geometry": settings.detached_geometry,
        "detached_fixed_width": str(settings.detached_fixed_width),
        "detached_fixed_height": str(settings.detached_fixed_height),
        "overlay_translation_mode": str(settings.overlay_translation_mode).lower(),
        "hytrans_port": str(settings.hytrans_port),
        "overlayer_port": str(settings.overlayer_port),
        "overlayer_always_on_top": str(settings.overlayer_always_on_top).lower(),
        "overlayer_hide_titlebar": str(settings.overlayer_hide_titlebar).lower(),
        "overlayer_fixed_size": str(settings.overlayer_fixed_size).lower(),
        "overlayer_exclude_from_capture": str(
            settings.overlayer_exclude_from_capture
        ).lower(),
        "overlayer_bg_color": settings.overlayer_bg_color,
        "overlayer_bg_opacity": str(settings.overlayer_bg_opacity),
        "overlayer_text_color": settings.overlayer_text_color,
        "overlayer_text_size": str(settings.overlayer_text_size),
        "overlayer_text_font": _normalize_font_name(settings.overlayer_text_font),
        "suppress_magpie_launch_notice": str(
            settings.suppress_magpie_launch_notice
        ).lower(),
        "debug_logging": str(settings.debug_logging).lower(),
    }
    temp_path = f"{SETTINGS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            parser.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, SETTINGS_FILE)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _write_json_atomic(path: str, payload: dict) -> None:
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def save_detached_region(region: Region) -> None:
    _write_json_atomic(
        DETACHED_REGION_FILE,
        {
            "version": 1,
            "left": region.left,
            "top": region.top,
            "width": region.width,
            "height": region.height,
        },
    )


def load_detached_region() -> Region | None:
    try:
        with open(DETACHED_REGION_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        region = Region(
            left=int(payload["left"]),
            top=int(payload["top"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
        )
        if region.width < MIN_SIZE_PX or region.height < MIN_SIZE_PX:
            return None
        return region
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def save_detached_geometry(geometry: str) -> None:
    _write_json_atomic(
        DETACHED_GEOMETRY_FILE,
        {"version": 1, "geometry": str(geometry)},
    )


def load_detached_geometry(fallback: str = DETACHED_DEFAULT_GEOMETRY) -> str:
    try:
        with open(DETACHED_GEOMETRY_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        geometry = str(payload["geometry"]).strip()
        if geometry:
            return geometry
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return fallback


def _geometry_size(geometry: str) -> tuple[int, int] | None:
    try:
        size = geometry.split("+", 1)[0]
        width_text, height_text = size.split("x", 1)
        return int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError):
        return None


def load_bookmarks() -> dict[str, Bookmark]:
    bookmarks: dict[str, Bookmark] = {}
    if not os.path.exists(BOOKMARKS_FILE):
        return bookmarks
    with open(BOOKMARKS_FILE, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 5:
                continue
            name, left, top, width, height = parts
            try:
                bookmarks[name] = Bookmark(
                    name=name,
                    left=int(left),
                    top=int(top),
                    width=int(width),
                    height=int(height),
                )
            except ValueError:
                continue
    return bookmarks


def save_bookmarks(bookmarks: dict[str, Bookmark]) -> None:
    with open(BOOKMARKS_FILE, "w", encoding="utf-8") as handle:
        for name in sorted(bookmarks):
            bookmark = bookmarks[name]
            handle.write(
                f"{bookmark.name}\t{bookmark.left}\t{bookmark.top}\t{bookmark.width}\t{bookmark.height}\n"
            )


def postprocess_text(text: str) -> str:
    return " ".join(text.split())


def _log_timestamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_file_path(kind: str, filename: str) -> str:
    directory = os.path.join(_get_app_dir(), kind)
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, filename)


def _is_debug_logging_enabled() -> bool:
    parser = configparser.ConfigParser()
    try:
        parser.read(SETTINGS_FILE, encoding="utf-8")
        return parser.getboolean("settings", "debug_logging", fallback=False)
    except configparser.Error:
        return False


def _log_runtime_error(stage: str, exc: Exception) -> None:
    log_path = _log_file_path("error_log", "mekicopy_error.log")
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("\n=== ERROR ===\n")
            handle.write(f"time: {_log_timestamp()}\n")
            handle.write(f"stage: {stage}\n")
            handle.write(f"type: {type(exc).__name__}\n")
            handle.write(f"message: {exc}\n")
            handle.write(traceback.format_exc())
    except OSError:
        pass


def _log_runtime_message(stage: str, message: str) -> None:
    if not _is_debug_logging_enabled():
        return
    log_path = _log_file_path("debug_log", "mekicopy_debug.log")
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("\n=== DEBUG ===\n")
            handle.write(f"time: {_log_timestamp()}\n")
            handle.write(f"stage: {stage}\n")
            handle.write(message.rstrip() + "\n")
    except OSError:
        pass


configure_capture_runtime(
    app_dir_provider=_get_app_dir,
    error_logger=_log_runtime_error,
    message_logger=_log_runtime_message,
)


def _patch_onnxruntime_compat() -> None:
    try:
        import onnxruntime as ort
    except Exception:
        return

    if not hasattr(ort, "set_default_logger_severity"):

        def _noop_set_default_logger_severity(_level: int) -> None:
            return None

        ort.set_default_logger_severity = _noop_set_default_logger_severity


def _preload_onnxruntime_gpu_dlls() -> None:
    global _ORT_PRELOAD_READY
    if _ORT_PRELOAD_READY:
        return

    _ORT_PRELOAD_READY = True
    try:
        import onnxruntime as ort
    except Exception as exc:
        _log_runtime_error("import_onnxruntime", exc)
        return

    preload_dlls = getattr(ort, "preload_dlls", None)
    if not callable(preload_dlls):
        return

    try:
        preload_dlls(cuda=True, cudnn=True, msvc=True)
    except Exception as exc:
        _log_runtime_error("preload_onnxruntime_gpu_dlls", exc)


def _find_bundled_model(filename: str) -> str | None:
    candidate_dirs = [
        os.path.join(_get_resource_dir(), "runtime_models"),
        os.path.join(_get_resource_dir(), "runtime_models", "meikiocr"),
        os.path.join(_get_app_dir(), "runtime_models"),
        os.path.join(_get_app_dir(), "runtime_models", "meikiocr"),
    ]
    for directory in candidate_dirs:
        candidate = os.path.join(directory, filename)
        if os.path.exists(candidate):
            return candidate
    return None


def _patch_meikiocr_model_loader(meikiocr_ocr) -> None:
    if getattr(meikiocr_ocr, "_mekicopy_patched", False):
        return

    original_get_model_path = meikiocr_ocr._get_model_path

    def _local_first_model_path(repo_id: str, filename: str) -> str:
        local_path = _find_bundled_model(filename)
        if local_path:
            return local_path
        return original_get_model_path(repo_id, filename)

    meikiocr_ocr._get_model_path = _local_first_model_path
    meikiocr_ocr._mekicopy_patched = True


def _get_available_ort_providers() -> list[str]:
    try:
        import onnxruntime as ort
    except Exception as exc:
        _log_runtime_error("get_available_ort_providers", exc)
        return []
    try:
        return list(ort.get_available_providers())
    except Exception as exc:
        _log_runtime_error("get_available_ort_providers", exc)
        return []


def _create_meikiocr_engine(meikiocr_ocr, provider: str):
    engine = meikiocr_ocr.MeikiOCR(provider=provider)
    active_provider = getattr(engine, "active_provider", provider)
    _log_runtime_message(
        "create_meikiocr_engine",
        f"requested_provider: {provider}\nactive_provider: {active_provider}",
    )
    return engine


def _create_best_meikiocr_engine(meikiocr_ocr):
    available_providers = _get_available_ort_providers()
    _log_runtime_message(
        "onnxruntime_providers",
        "available_providers: " + ", ".join(available_providers),
    )

    if "CUDAExecutionProvider" in available_providers:
        try:
            return _create_meikiocr_engine(meikiocr_ocr, "CUDAExecutionProvider")
        except Exception as exc:
            _log_runtime_error("create_cuda_meikiocr_engine", exc)

    return _create_meikiocr_engine(meikiocr_ocr, "CPUExecutionProvider")


def _get_ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE

    _prepare_windowed_streams()
    _prepare_native_runtime_paths()
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    _patch_onnxruntime_compat()
    _preload_onnxruntime_gpu_dlls()
    import meikiocr.ocr as meikiocr_ocr

    _patch_meikiocr_model_loader(meikiocr_ocr)
    _OCR_ENGINE = _create_best_meikiocr_engine(meikiocr_ocr)
    return _OCR_ENGINE


def run_meikiocr(image_path: str) -> str:
    try:
        _prepare_native_runtime_paths()
        import cv2

        ocr = _get_ocr_engine()
        image = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if image is None:
            return ""
        results = ocr.run_ocr(image)
    except Exception as exc:
        _log_runtime_error("run_meikiocr", exc)
        return postprocess_text(str(exc))

    output_lines = []
    for line in results:
        text = line.get("text", "")
        if text:
            output_lines.append(text)
    return postprocess_text("\n".join(output_lines))


def capture_region(left: int, top: int, width: int, height: int) -> Image.Image:
    result = capture_region_result(left, top, width, height)
    if result.image is None:
        raise RuntimeError(_capture_problem_message(result))
    return result.image


def copy_text_to_clipboard(
    text: str,
    notify: bool = True,
    parent: tk.Misc | None = None,
) -> None:
    _prepare_tk_library_paths()
    clip_owner = parent
    created_clip_owner = False
    if clip_owner is None:
        clip_owner = tk.Tk()
        clip_owner.withdraw()
        created_clip_owner = True

    clip_owner.clipboard_clear()
    clip_owner.clipboard_append(text)
    clip_owner.update()
    if created_clip_owner:
        clip_owner.destroy()
    if notify:
        messagebox.showinfo("MekiCopy", "복사되었습니다!", parent=parent)


def ocr_region(
    left: int,
    top: int,
    width: int,
    height: int,
    parent: tk.Misc | None = None,
) -> str | None:
    if width < MIN_SIZE_PX or height < MIN_SIZE_PX:
        messagebox.showerror("MekiCopy", "캡처 영역이 너무 작습니다.", parent=parent)
        return None
    result = capture_region_result(left, top, width, height)
    if result.status in (
        CaptureStatus.REGION_OUT_OF_BOUNDS,
        CaptureStatus.BLACK_FRAME_SUSPECTED,
        CaptureStatus.CAPTURE_EXCEPTION,
        CaptureStatus.SIZE_MISMATCH,
    ):
        messagebox.showwarning("MekiCopy", _capture_problem_message(result), parent=parent)
        return None
    if result.image is None:
        messagebox.showwarning("MekiCopy", _capture_problem_message(result), parent=parent)
        return None
    image = result.image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        temp_path = temp_file.name
        image.save(temp_path)
    try:
        text = run_meikiocr(temp_path)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return text


def ocr_and_copy(
    left: int,
    top: int,
    width: int,
    height: int,
    notify: bool = True,
    parent: tk.Misc | None = None,
    on_copy_complete: Callable[[], None] | None = None,
) -> None:
    text = ocr_region(left, top, width, height, parent=parent)
    if text is None:
        return
    copy_text_to_clipboard(text, notify=notify, parent=parent)
    if on_copy_complete:
        on_copy_complete()


class SelectionUI:
    def __init__(
        self,
        root: tk.Tk,
        initial_rect: Rect | None = None,
        on_confirm: Callable[[Region], None] | None = None,
        capture_on_enter: bool = False,
    ):
        self.root = root
        self.canvas = None
        self.rect_id = None
        self.handle_ids: dict[str, int] = {}
        self.start_point: tuple[int, int] | None = None
        self.selection: Rect | None = None
        self.drag_mode: str | None = None
        self.initial_rect = initial_rect
        self.on_confirm = on_confirm
        self.capture_on_enter = capture_on_enter

        with mss.mss() as sct:
            monitor = sct.monitors[0]
            self.monitors = list(sct.monitors[1:])
        self.virtual_left = monitor["left"]
        self.virtual_top = monitor["top"]
        self.virtual_width = monitor["width"]
        self.virtual_height = monitor["height"]
        if not self.monitors:
            self.monitors = [monitor]

        self._setup_root()
        self._setup_canvas()
        self._bind_events()
        self._draw_instructions()
        if self.initial_rect:
            self._set_selection(self.initial_rect)

    def _setup_root(self) -> None:
        self.root.attributes("-topmost", True)
        self.root.attributes("-fullscreen", False)
        self.root.overrideredirect(True)
        geometry = (
            f"{self.virtual_width}x{self.virtual_height}"
            f"+{self.virtual_left}+{self.virtual_top}"
        )
        self.root.geometry(geometry)
        self.root.configure(bg="black")
        self.root.attributes("-alpha", 0.25)
        self.root.focus_force()

    def _setup_canvas(self) -> None:
        self.canvas = tk.Canvas(
            self.root,
            bg="black",
            highlightthickness=0,
            width=self.virtual_width,
            height=self.virtual_height,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def _bind_events(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._on_mouse_down)
        self.canvas.bind("<B1-Motion>", self._on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_mouse_up)
        self.root.bind("<Return>", self._on_capture)
        self.root.bind("<Escape>", self._on_cancel)

    def _draw_instructions(self) -> None:
        if self.capture_on_enter:
            text = "드래그로 미세 조정, <Enter> 캡처"
        else:
            text = "드래그로 미세 조정, <Enter> 설정"
        for monitor in self.monitors:
            x = monitor["left"] - self.virtual_left + 24
            y = monitor["top"] - self.virtual_top + 24
            self.canvas.create_text(
                x + 4,
                y + 4,
                anchor="nw",
                text=text,
                fill="black",
                font=("Segoe UI", SELECTION_INSTRUCTION_FONT_SIZE, "bold"),
            )
            self.canvas.create_text(
                x,
                y,
                anchor="nw",
                text=text,
                fill="white",
                font=("Segoe UI", SELECTION_INSTRUCTION_FONT_SIZE, "bold"),
            )

    def _canvas_coords(self, x: int, y: int) -> tuple[int, int]:
        return x - self.virtual_left, y - self.virtual_top

    def _screen_coords(self, x: int, y: int) -> tuple[int, int]:
        return x + self.virtual_left, y + self.virtual_top

    def _set_selection(self, rect: Rect) -> None:
        rect = rect.normalized()
        self.selection = rect
        self._draw_selection()

    def _draw_selection(self) -> None:
        if not self.selection:
            return
        rect = self.selection.normalized()
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        for handle_id in self.handle_ids.values():
            self.canvas.delete(handle_id)
        self.rect_id = self.canvas.create_rectangle(
            rect.left,
            rect.top,
            rect.right,
            rect.bottom,
            outline="yellow",
            width=2,
        )
        self.handle_ids = {}
        self._draw_handles(rect)

    def _draw_handles(self, rect: Rect) -> None:
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        self.handle_ids["left"] = self._draw_handle(rect.left, cy)
        self.handle_ids["right"] = self._draw_handle(rect.right, cy)
        self.handle_ids["top"] = self._draw_handle(cx, rect.top)
        self.handle_ids["bottom"] = self._draw_handle(cx, rect.bottom)

    def _draw_handle(self, x: int, y: int) -> int:
        size = 6
        return self.canvas.create_rectangle(
            x - size,
            y - size,
            x + size,
            y + size,
            outline="yellow",
            fill="black",
        )

    def _edge_hit_test(self, x: int, y: int) -> str | None:
        if not self.selection:
            return None
        rect = self.selection.normalized()
        if abs(x - rect.left) <= EDGE_GRAB_PX and rect.top <= y <= rect.bottom:
            return "left"
        if abs(x - rect.right) <= EDGE_GRAB_PX and rect.top <= y <= rect.bottom:
            return "right"
        if abs(y - rect.top) <= EDGE_GRAB_PX and rect.left <= x <= rect.right:
            return "top"
        if abs(y - rect.bottom) <= EDGE_GRAB_PX and rect.left <= x <= rect.right:
            return "bottom"
        if rect.left <= x <= rect.right and rect.top <= y <= rect.bottom:
            return "move"
        return None

    def _on_mouse_down(self, event: tk.Event) -> None:
        x, y = event.x, event.y
        if self.selection:
            hit = self._edge_hit_test(x, y)
            if hit:
                self.drag_mode = hit
                self.start_point = (x, y)
                return
        self.drag_mode = "new"
        self.start_point = (x, y)
        self.selection = Rect(x, y, x, y)
        self._draw_selection()

    def _on_mouse_drag(self, event: tk.Event) -> None:
        if not self.start_point or not self.selection:
            return
        x, y = event.x, event.y
        rect = self.selection
        if self.drag_mode == "new":
            rect.right = x
            rect.bottom = y
        elif self.drag_mode == "move":
            dx = x - self.start_point[0]
            dy = y - self.start_point[1]
            rect.left += dx
            rect.right += dx
            rect.top += dy
            rect.bottom += dy
            self.start_point = (x, y)
        elif self.drag_mode == "left":
            rect.left = x
        elif self.drag_mode == "right":
            rect.right = x
        elif self.drag_mode == "top":
            rect.top = y
        elif self.drag_mode == "bottom":
            rect.bottom = y
        self.selection = rect
        self._draw_selection()

    def _on_mouse_up(self, event: tk.Event) -> None:
        if not self.selection:
            return
        rect = self.selection.normalized()
        if rect.width < MIN_SIZE_PX or rect.height < MIN_SIZE_PX:
            self.selection = None
            if self.rect_id:
                self.canvas.delete(self.rect_id)
            return
        self.selection = rect
        self.drag_mode = None
        self.start_point = None
        self._draw_selection()

    def _on_capture(self, event: tk.Event | None = None) -> None:
        if not self.selection:
            return
        rect = self.selection.normalized()
        left, top = self._screen_coords(rect.left, rect.top)
        width = rect.width
        height = rect.height
        if self.capture_on_enter:
            self.root.withdraw()
            self.root.update_idletasks()
            ocr_and_copy(left, top, width, height)
        elif self.on_confirm:
            self.on_confirm(Region(left=left, top=top, width=width, height=height))
        self.root.destroy()

    def _on_cancel(self, event: tk.Event | None = None) -> None:
        self.root.destroy()


class RegionViewUI:
    def __init__(
        self,
        root: tk.Toplevel | tk.Tk,
        draft_region: Region | None,
        active_region: Region | None,
    ):
        self.root = root
        self.canvas = None
        self.draft_region = draft_region
        self.active_region = active_region

        with mss.mss() as sct:
            monitor = sct.monitors[0]
            self.monitors = list(sct.monitors[1:])
        self.virtual_left = monitor["left"]
        self.virtual_top = monitor["top"]
        self.virtual_width = monitor["width"]
        self.virtual_height = monitor["height"]
        if not self.monitors:
            self.monitors = [monitor]

        self._setup_root()
        self._setup_canvas()
        self._bind_events()
        self._draw_regions()

    def _setup_root(self) -> None:
        self.root.attributes("-topmost", True)
        self.root.attributes("-fullscreen", False)
        self.root.overrideredirect(True)
        geometry = (
            f"{self.virtual_width}x{self.virtual_height}"
            f"+{self.virtual_left}+{self.virtual_top}"
        )
        self.root.geometry(geometry)
        self.root.configure(bg="black")
        self.root.attributes("-alpha", 0.30)
        self.root.focus_force()

    def _setup_canvas(self) -> None:
        self.canvas = tk.Canvas(
            self.root,
            bg="black",
            highlightthickness=0,
            width=self.virtual_width,
            height=self.virtual_height,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def _bind_events(self) -> None:
        self.root.bind("<Escape>", self._on_cancel)

    def _to_canvas_rect(self, region: Region) -> Rect:
        return Rect(
            region.left - self.virtual_left,
            region.top - self.virtual_top,
            region.left + region.width - self.virtual_left,
            region.top + region.height - self.virtual_top,
        )

    def _draw_regions(self) -> None:
        self.canvas.create_text(
            20,
            20,
            anchor="nw",
            text="임시 영역: 파란색 / 확정 영역: 빨간색 / Esc 닫기",
            fill="white",
            font=("Segoe UI", 12, "bold"),
        )
        if self.draft_region:
            self._draw_region(self.draft_region, outline="#00d7ff", width=3)
        if self.active_region:
            self._draw_region(self.active_region, outline="#ff405c", width=5)

    def _draw_region(self, region: Region, outline: str, width: int) -> None:
        rect = self._to_canvas_rect(region).normalized()
        self.canvas.create_rectangle(
            rect.left,
            rect.top,
            rect.right,
            rect.bottom,
            outline=outline,
            width=width,
        )

    def _on_cancel(self, event: tk.Event | None = None) -> None:
        self.root.destroy()


_HICON = getattr(wintypes, "HICON", wintypes.HANDLE)
_LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WndClassW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", _HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NotifyIconDataW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", _HICON),
        ("szTip", wintypes.WCHAR * 128),
    ]


class WindowsTrayIcon:
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    WM_USER = 0x0400
    WM_TRAY_CALLBACK = WM_USER + 20
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040
    IDI_APPLICATION = 32512
    RESTORE_POLL_MS = 25

    def __init__(
        self,
        root: tk.Tk,
        tooltip: str,
        on_restore: Callable[[], None],
    ) -> None:
        self.root = root
        self.tooltip = tooltip[:127]
        self.on_restore = on_restore
        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._hinstance: int | None = None
        self._window_class_name: str | None = None
        self._window_class_atom: int | None = None
        self._wndproc = None
        self._active = False
        self._restore_requested = threading.Event()
        self._closed = False
        self._poll_after_id = self.root.after(
            self.RESTORE_POLL_MS,
            self._poll_restore_requests,
        )

    def show(self) -> bool:
        if os.name != "nt" or self._active:
            return False
        try:
            if not self._hwnd:
                self._create_message_window()
            if not self._hicon:
                self._hicon = self._load_icon()
            data = self._build_notify_data(self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP)
            shell32 = ctypes.windll.shell32
            if not shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(data)):
                return False
            self._active = True
            return True
        except Exception as exc:
            _log_runtime_error("tray_show", exc)
            self._destroy_message_window()
            return False

    def hide(self) -> None:
        if os.name != "nt":
            return
        if self._active and self._hwnd:
            try:
                data = self._build_notify_data(0)
                ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(data))
            except Exception:
                pass
        self._active = False
        self._restore_requested.clear()

    def close(self) -> None:
        """Remove the icon and native message window during application shutdown."""
        if self._closed:
            return
        self._closed = True
        self.hide()
        if self._poll_after_id:
            try:
                self.root.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        self._destroy_message_window()

    def _destroy_message_window(self) -> None:
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        if self._hwnd:
            try:
                user32.DestroyWindow.argtypes = [wintypes.HWND]
                user32.DestroyWindow.restype = wintypes.BOOL
                user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None
        if self._window_class_name and self._hinstance:
            try:
                user32.UnregisterClassW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.HINSTANCE,
                ]
                user32.UnregisterClassW.restype = wintypes.BOOL
                user32.UnregisterClassW(self._window_class_name, self._hinstance)
            except Exception:
                pass
        self._window_class_name = None
        self._window_class_atom = None
        self._hinstance = None
        self._wndproc = None

    def _poll_restore_requests(self) -> None:
        self._poll_after_id = None
        if self._closed:
            return
        if self._restore_requested.is_set():
            self._restore_requested.clear()
            if self._active:
                try:
                    self.on_restore()
                except Exception as exc:
                    _log_runtime_error("tray_restore", exc)
        try:
            self._poll_after_id = self.root.after(
                self.RESTORE_POLL_MS,
                self._poll_restore_requests,
            )
        except tk.TclError:
            self._closed = True

    def _build_notify_data(self, flags: int) -> _NotifyIconDataW:
        data = _NotifyIconDataW()
        data.cbSize = ctypes.sizeof(_NotifyIconDataW)
        data.hWnd = self._hwnd or 0
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = self.WM_TRAY_CALLBACK
        data.hIcon = self._hicon or 0
        data.szTip = self.tooltip
        return data

    def _load_icon(self) -> int:
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = _HICON
        icon_path = _get_icon_path()
        if icon_path:
            handle = user32.LoadImageW(
                None,
                icon_path,
                self.IMAGE_ICON,
                0,
                0,
                self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE,
            )
            if handle:
                return int(handle)
        user32.LoadIconW.restype = _HICON
        return int(user32.LoadIconW(None, self.IDI_APPLICATION))

    def _create_message_window(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.DefWindowProcW.restype = _LRESULT
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]

        def _window_proc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY_CALLBACK and lparam in (
                self.WM_LBUTTONDBLCLK,
                self.WM_RBUTTONUP,
            ):
                # A ctypes WndProc must remain tiny. Calling into Tk from here can
                # re-enter Tcl while Windows is dispatching the native message and
                # has caused hard process crashes. The Tk thread polls this event.
                self._restore_requested.set()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = _WNDPROC(_window_proc)
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._hinstance = kernel32.GetModuleHandleW(None)
        self._window_class_name = (
            f"MekiCopy.TrayMessageWindow.{os.getpid()}.{id(self):x}"
        )
        window_class = _WndClassW()
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = self._hinstance
        window_class.lpszClassName = self._window_class_name
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.RegisterClassW.argtypes = [ctypes.POINTER(_WndClassW)]
        self._window_class_atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not self._window_class_atom:
            raise ctypes.WinError(ctypes.get_last_error())

        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        self._hwnd = user32.CreateWindowExW(
            0,
            self._window_class_name,
            self.tooltip,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            self._hinstance,
            None,
        )
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())


class BookmarkPicker(tk.Toplevel):
    def __init__(self, owner: tk.Misc, bookmarks: dict[str, Bookmark]):
        super().__init__(owner)
        self.title("MekiCopy 북마크 선택")
        _set_window_icon(self)
        self.bookmarks = bookmarks
        self.selected: Bookmark | None = None
        self._build_ui()
        self.transient(owner)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build_ui(self) -> None:
        self.geometry("320x240")
        self.indicator = tk.Label(self, text="북마크를 선택하세요")
        self.indicator.pack(pady=10)
        self.listbox = tk.Listbox(self)
        for name in sorted(self.bookmarks):
            self.listbox.insert(tk.END, name)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=10)
        if self.listbox.size():
            self.listbox.selection_set(0)
            self.listbox.activate(0)
            self.listbox.focus_set()
        button = tk.Button(self, text="선택", command=self._on_select)
        button.pack(pady=10)
        self.bind("<Return>", lambda _event: self._on_select())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.listbox.bind("<Return>", lambda _event: self._on_select())
        self.listbox.bind("<Double-Button-1>", lambda _event: self._on_select())

    def _on_select(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        name = self.listbox.get(selection[0])
        self.selected = self.bookmarks[name]
        self.destroy()


def _show_bookmark_picker(
    bookmarks: dict[str, Bookmark],
    parent: tk.Misc | None = None,
) -> Bookmark | None:
    _prepare_tk_library_paths()
    temporary_root: tk.Tk | None = None
    owner = parent
    if owner is None:
        temporary_root = tk.Tk()
        temporary_root.withdraw()
        owner = temporary_root

    picker = BookmarkPicker(owner, bookmarks)
    try:
        picker.grab_set()
    except tk.TclError:
        pass
    picker.focus_force()
    owner.wait_window(picker)
    selected = picker.selected
    if temporary_root and temporary_root.winfo_exists():
        temporary_root.destroy()
    return selected


def run_picker_and_capture(parent: tk.Misc | None = None) -> None:
    bookmarks = load_bookmarks()
    if not bookmarks:
        messagebox.showerror("MekiCopy", "저장된 북마크가 없습니다.", parent=parent)
        return
    bookmark = _show_bookmark_picker(bookmarks, parent=parent)
    if bookmark:
        ocr_and_copy(bookmark.left, bookmark.top, bookmark.width, bookmark.height)


def pick_bookmark(parent: tk.Misc | None = None) -> Bookmark | None:
    bookmarks = load_bookmarks()
    if not bookmarks:
        messagebox.showerror("MekiCopy", "저장된 북마크가 없습니다.", parent=parent)
        return None
    return _show_bookmark_picker(bookmarks, parent=parent)


def build_initial_rect(region: Region | Bookmark | None) -> Rect | None:
    _enable_dpi_awareness()
    if not region:
        return None
    rect_left = region.left
    rect_top = region.top
    rect_right = rect_left + region.width
    rect_bottom = rect_top + region.height
    with mss.mss() as sct:
        monitor = sct.monitors[0]
    left_offset = monitor["left"]
    top_offset = monitor["top"]
    return Rect(
        rect_left - left_offset,
        rect_top - top_offset,
        rect_right - left_offset,
        rect_bottom - top_offset,
    )


def run_selection(
    initial_region: Region | Bookmark | None = None,
    capture_on_enter: bool = True,
    parent: tk.Tk | None = None,
) -> Region | None:
    _enable_dpi_awareness()
    _prepare_tk_library_paths()
    selection: Region | None = None

    def store_selection(region: Region) -> None:
        nonlocal selection
        selection = region

    initial_rect = build_initial_rect(initial_region)
    if parent:
        root = tk.Toplevel(parent)
    else:
        root = tk.Tk()
    SelectionUI(
        root,
        initial_rect=initial_rect,
        on_confirm=store_selection,
        capture_on_enter=capture_on_enter,
    )
    if parent:
        parent.wait_window(root)
    else:
        root.mainloop()
    return selection


def run_region_view(
    draft_region: Region | None,
    active_region: Region | None,
    parent: tk.Tk | None = None,
) -> None:
    _prepare_tk_library_paths()
    if parent:
        root = tk.Toplevel(parent)
    else:
        root = tk.Tk()
    RegionViewUI(root, draft_region=draft_region, active_region=active_region)
    if parent:
        parent.wait_window(root)
    else:
        root.mainloop()


def _json_request(
    url: str,
    payload: dict | None = None,
    timeout: float = 5.0,
    method: str | None = None,
) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)


def _is_process_alive(process: subprocess.Popen | None) -> bool:
    return bool(process and process.poll() is None)


def _mekicopy_process_command(*arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    return [sys.executable, os.path.abspath(__file__), *arguments]


def _detached_process_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )


def _launch_detached_button_process() -> subprocess.Popen:
    return subprocess.Popen(
        _mekicopy_process_command("--detached-button"),
        cwd=_get_app_dir(),
        close_fds=True,
        creationflags=_detached_process_creation_flags(),
    )


class _WindowsNamedMutex:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str) -> None:
        self.handle: int | None = None
        self.already_exists = False
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.SetLastError(0)
        self.handle = kernel32.CreateMutexW(None, False, name)
        if not self.handle:
            raise ctypes.WinError(kernel32.GetLastError())
        self.already_exists = kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle and os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(self.handle)
            self.handle = None


def _find_detached_window() -> int | None:
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    hwnd = user32.FindWindowW(None, DETACHED_WINDOW_TITLE)
    return int(hwnd) if hwnd else None


def _activate_detached_window() -> bool:
    hwnd = _find_detached_window()
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def _close_detached_window() -> bool:
    hwnd = _find_detached_window()
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    return bool(user32.PostMessageW(hwnd, 0x0010, 0, 0))  # WM_CLOSE


def _find_companion_executable(app_name: str, script_name: str) -> list[str] | None:
    exe_name = f"{app_name}.exe"
    app_dir = _get_app_dir()
    candidates = [
        os.path.join(app_dir, exe_name),
        os.path.join(os.path.dirname(app_dir), app_name, exe_name),
        os.path.join(os.path.dirname(os.path.dirname(app_dir)), app_name, exe_name),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return [candidate]

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)
    if os.path.exists(script_path):
        return [sys.executable, script_path]
    return None


def _startupinfo_for_background() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return startupinfo


def _magpie_install_dir() -> str:
    return os.path.join(os.path.dirname(_get_app_dir()), "MagPie")


def _find_magpie_executable(directory: str | None = None) -> str | None:
    install_dir = directory or _magpie_install_dir()
    direct_path = os.path.join(install_dir, "MagPie.exe")
    if os.path.isfile(direct_path):
        return direct_path
    if not os.path.isdir(install_dir):
        return None
    for root, _dirs, files in os.walk(install_dir):
        for filename in files:
            if filename.casefold() == "magpie.exe":
                return os.path.join(root, filename)
    return None


def _select_magpie_release_asset(release: dict) -> dict:
    assets = [
        asset
        for asset in release.get("assets", [])
        if str(asset.get("name", "")).lower().endswith(".zip")
        and asset.get("browser_download_url")
    ]
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    for asset in assets:
        if architecture in str(asset.get("name", "")).lower():
            return asset
    raise RuntimeError(f"{architecture}용 MagPie 최신 릴리스 ZIP 파일이 없습니다.")


def _safe_extract_zip(archive_path: str, destination: str) -> None:
    destination_real = os.path.realpath(destination)
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            member_path = os.path.realpath(os.path.join(destination, info.filename))
            if os.path.commonpath([destination_real, member_path]) != destination_real:
                raise RuntimeError("MagPie 압축 파일에 안전하지 않은 경로가 포함되어 있습니다.")
        archive.extractall(destination)


def _install_latest_magpie() -> str:
    request = urllib.request.Request(
        MAGPIE_RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MekiCopy-MagPie-Installer",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))
    asset = _select_magpie_release_asset(release)
    install_dir = _magpie_install_dir()
    suite_root = os.path.dirname(install_dir)
    os.makedirs(suite_root, exist_ok=True)
    staging_dir = tempfile.mkdtemp(prefix=".magpie-install-", dir=suite_root)
    try:
        archive_path = os.path.join(staging_dir, "MagPie.zip")
        download_request = urllib.request.Request(
            str(asset["browser_download_url"]),
            headers={"User-Agent": "MekiCopy-MagPie-Installer"},
        )
        with urllib.request.urlopen(download_request, timeout=120) as response:
            with open(archive_path, "wb") as archive_file:
                shutil.copyfileobj(response, archive_file)

        extracted_dir = os.path.join(staging_dir, "extracted")
        os.makedirs(extracted_dir, exist_ok=True)
        _safe_extract_zip(archive_path, extracted_dir)
        staged_executable = _find_magpie_executable(extracted_dir)
        if not staged_executable:
            raise RuntimeError("다운로드한 MagPie 압축 파일에서 MagPie.exe를 찾지 못했습니다.")

        source_root = os.path.dirname(staged_executable)
        os.makedirs(install_dir, exist_ok=True)
        for entry in os.scandir(source_root):
            destination = os.path.join(install_dir, entry.name)
            if entry.is_dir():
                shutil.copytree(entry.path, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(entry.path, destination)

        installed_executable = _find_magpie_executable(install_dir)
        if not installed_executable:
            raise RuntimeError("MagPie 설치 후 실행 파일을 찾지 못했습니다.")
        return installed_executable
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


class SettingsWindow(tk.Toplevel):
    def __init__(self, owner) -> None:
        super().__init__(owner)
        self.owner = owner
        self.title("MekiCopy 설정")
        self.resizable(False, True)
        _set_window_icon(self)

        settings = owner.settings
        self.minimize_to_tray_var = tk.BooleanVar(value=settings.minimize_to_tray)
        self.main_topmost_var = tk.BooleanVar(value=settings.main_always_on_top)
        self.detached_topmost_var = tk.BooleanVar(value=settings.detached_always_on_top)
        self.detached_hide_titlebar_var = tk.BooleanVar(
            value=settings.detached_hide_titlebar
        )
        self.detached_fixed_size_var = tk.BooleanVar(value=settings.detached_fixed_size)
        self.simple_copy_complete_var = tk.BooleanVar(
            value=settings.simple_copy_complete
        )
        self.overlay_mode_var = tk.BooleanVar(value=settings.overlay_translation_mode)
        self.hytrans_port_var = tk.IntVar(value=settings.hytrans_port)
        self.overlayer_port_var = tk.IntVar(value=settings.overlayer_port)
        self.overlayer_topmost_var = tk.BooleanVar(value=settings.overlayer_always_on_top)
        self.overlayer_hide_titlebar_var = tk.BooleanVar(
            value=settings.overlayer_hide_titlebar
        )
        self.overlayer_fixed_size_var = tk.BooleanVar(value=settings.overlayer_fixed_size)
        self.overlayer_exclude_capture_var = tk.BooleanVar(
            value=settings.overlayer_exclude_from_capture
        )
        self.overlayer_bg_color_var = tk.StringVar(value=settings.overlayer_bg_color)
        self.overlayer_opacity_var = tk.IntVar(
            value=max(10, min(100, int(settings.overlayer_bg_opacity * 100)))
        )
        self.overlayer_text_color_var = tk.StringVar(value=settings.overlayer_text_color)
        self.overlayer_text_size_var = tk.IntVar(value=settings.overlayer_text_size)
        self.overlayer_text_font_var = tk.StringVar(value=settings.overlayer_text_font)
        self.suppress_magpie_notice_var = tk.BooleanVar(
            value=settings.suppress_magpie_launch_notice
        )
        self.debug_logging_var = tk.BooleanVar(value=settings.debug_logging)
        self.overlay_only_widgets: list[tk.Widget] = []
        self.detached_label_controls: list[tuple[tk.Widget, str]] = []
        self._color_buttons: list[tuple[tk.Button, tk.StringVar]] = []

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.transient(owner)
        self.attributes("-topmost", settings.main_always_on_top)
        self.overlay_mode_var.trace_add("write", lambda *_: self._on_overlay_mode_changed())
        self._update_mode_labels()
        self._update_overlay_controls()

    def _build_ui(self) -> None:
        body = tk.Frame(self, padx=14, pady=14)
        body.pack(fill=tk.BOTH, expand=True)

        options = [
            (
                "MekiCopy가 최소화되면 시스템 트레이로 이동",
                self.minimize_to_tray_var,
            ),
            ("MekiCopy를 항상 위로", self.main_topmost_var),
            ("복사 완료를 간단하게 표시하기", self.simple_copy_complete_var),
        ]
        for text, variable in options:
            checkbox = tk.Checkbutton(body, text=text, variable=variable, anchor="w")
            checkbox.pack(fill=tk.X, pady=4)

        detached_options = [
            ("버튼을 항상 위로", self.detached_topmost_var),
            ("버튼의 제목표시줄 숨김", self.detached_hide_titlebar_var),
            ("버튼의 크기를 고정", self.detached_fixed_size_var),
        ]
        for suffix, variable in detached_options:
            checkbox = tk.Checkbutton(body, variable=variable, anchor="w")
            checkbox.pack(fill=tk.X, pady=4)
            self.detached_label_controls.append((checkbox, suffix))

        suppress_magpie_notice_checkbox = tk.Checkbutton(
            body,
            text="MagPie 실행 시 안내 띄우지 않기",
            variable=self.suppress_magpie_notice_var,
            anchor="w",
        )
        suppress_magpie_notice_checkbox.pack(fill=tk.X, pady=4)

        debug_checkbox = tk.Checkbutton(
            body,
            text="오류 분석을 위한 디버그 로그 켜기",
            variable=self.debug_logging_var,
            anchor="w",
        )
        debug_checkbox.pack(fill=tk.X, pady=(4, 8))

        overlay_frame = tk.LabelFrame(body, text="번역 오버레이 모드", padx=10, pady=8)
        overlay_frame.pack(fill=tk.X, pady=(6, 0))

        overlay_checkbox = tk.Checkbutton(
            overlay_frame,
            text="오버레이어 번역 모드 사용",
            variable=self.overlay_mode_var,
            anchor="w",
        )
        overlay_checkbox.pack(fill=tk.X, pady=3)

        port_row = tk.Frame(overlay_frame)
        port_row.pack(fill=tk.X, pady=3)
        port_label = tk.Label(port_row, text="HYTrans 포트")
        port_label.pack(side=tk.LEFT)
        port_spin = tk.Spinbox(
            port_row,
            from_=1,
            to=65535,
            width=8,
            textvariable=self.hytrans_port_var,
        )
        port_spin.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([port_label, port_spin])

        overlayer_port_row = tk.Frame(overlay_frame)
        overlayer_port_row.pack(fill=tk.X, pady=3)
        overlayer_port_label = tk.Label(overlayer_port_row, text="MekiOverlayer 포트")
        overlayer_port_label.pack(side=tk.LEFT)
        overlayer_port_spin = tk.Spinbox(
            overlayer_port_row,
            from_=1,
            to=65535,
            width=8,
            textvariable=self.overlayer_port_var,
        )
        overlayer_port_spin.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([overlayer_port_label, overlayer_port_spin])

        overlayer_options = [
            ("MekiOverlayer를 항상 위로", self.overlayer_topmost_var),
            ("MekiOverlayer의 제목표시줄 숨김", self.overlayer_hide_titlebar_var),
            ("MekiOverlayer 크기 고정", self.overlayer_fixed_size_var),
            (
                "MekiOverlayer가 캡쳐되지 않도록 방지",
                self.overlayer_exclude_capture_var,
            ),
        ]
        for text, variable in overlayer_options:
            checkbox = tk.Checkbutton(
                overlay_frame,
                text=text,
                variable=variable,
                anchor="w",
            )
            checkbox.pack(fill=tk.X, pady=3)
            self.overlay_only_widgets.append(checkbox)

        overlayer_style = tk.LabelFrame(
            overlay_frame,
            text="MekiOverlayer 설정",
            padx=8,
            pady=8,
        )
        overlayer_style.pack(fill=tk.X, pady=(8, 2))
        self.overlay_only_widgets.append(overlayer_style)

        bg_button = tk.Button(
            overlayer_style,
            text="배경색깔",
            command=lambda: self._choose_color(self.overlayer_bg_color_var, bg_button),
        )
        bg_button.pack(fill=tk.X, pady=2)
        self._color_buttons.append((bg_button, self.overlayer_bg_color_var))
        self.overlay_only_widgets.append(bg_button)

        opacity_row = tk.Frame(overlayer_style)
        opacity_row.pack(fill=tk.X, pady=2)
        tk.Label(opacity_row, text="배경 투명도").pack(side=tk.LEFT)
        opacity_scale = tk.Scale(
            opacity_row,
            from_=10,
            to=100,
            orient=tk.HORIZONTAL,
            showvalue=True,
            variable=self.overlayer_opacity_var,
            length=180,
        )
        opacity_scale.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([opacity_row, opacity_scale])

        text_color_button = tk.Button(
            overlayer_style,
            text="글씨 색깔",
            command=lambda: self._choose_color(
                self.overlayer_text_color_var, text_color_button
            ),
        )
        text_color_button.pack(fill=tk.X, pady=2)
        self._color_buttons.append((text_color_button, self.overlayer_text_color_var))
        self.overlay_only_widgets.append(text_color_button)

        size_row = tk.Frame(overlayer_style)
        size_row.pack(fill=tk.X, pady=2)
        tk.Label(size_row, text="글씨 크기").pack(side=tk.LEFT)
        size_spin = tk.Spinbox(
            size_row,
            from_=8,
            to=96,
            width=6,
            textvariable=self.overlayer_text_size_var,
        )
        size_spin.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([size_row, size_spin])

        font_row = tk.Frame(overlayer_style)
        font_row.pack(fill=tk.X, pady=2)
        tk.Label(font_row, text="글씨 폰트").pack(side=tk.LEFT)
        font_names = _korean_font_families(self)
        current_font = _normalize_font_name(self.overlayer_text_font_var.get())
        if font_names:
            selected_font = current_font if current_font in font_names else (
                "Malgun Gothic" if "Malgun Gothic" in font_names else font_names[0]
            )
            self.overlayer_text_font_var.set(selected_font)
            menu_values = font_names
        else:
            self.overlayer_text_font_var.set("")
            menu_values = [""]
        self.korean_font_names = font_names
        font_menu = tk.OptionMenu(
            font_row,
            self.overlayer_text_font_var,
            *menu_values,
        )
        font_menu.config(width=20)
        if not font_names:
            font_menu.config(state=tk.DISABLED)
        font_menu.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([font_row, font_menu])

        button_row = tk.Frame(body)
        button_row.pack(fill=tk.X, pady=(14, 0))
        save_button = tk.Button(button_row, text="저장", command=self._on_save)
        save_button.pack(side=tk.RIGHT, padx=(8, 0))
        close_button = tk.Button(button_row, text="닫기", command=self._on_close)
        close_button.pack(side=tk.RIGHT)
        self._refresh_color_buttons()

    def _choose_color(self, variable: tk.StringVar, button: tk.Button) -> None:
        color = colorchooser.askcolor(color=variable.get(), parent=self)[1]
        if not color:
            return
        variable.set(color)
        self._refresh_color_buttons()

    def _refresh_color_buttons(self) -> None:
        for button, variable in self._color_buttons:
            color = variable.get()
            button.config(bg=color, activebackground=color)

    def _mode_action_label(self) -> str:
        return "번역 후 표시" if self.overlay_mode_var.get() else "인식 후 복사"

    def _update_mode_labels(self) -> None:
        action_label = self._mode_action_label()
        for widget, suffix in self.detached_label_controls:
            widget.configure(text=f"분리된 '{action_label}' {suffix}")

    def _on_overlay_mode_changed(self) -> None:
        self._update_mode_labels()
        self._update_overlay_controls()

    def _update_overlay_controls(self) -> None:
        state = tk.NORMAL if self.overlay_mode_var.get() else tk.DISABLED
        for widget in self.overlay_only_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                for child in widget.winfo_children():
                    try:
                        child.configure(state=state)
                    except tk.TclError:
                        pass

    def _read_port(self, variable: tk.IntVar, label: str) -> int:
        try:
            return _normalize_port(variable.get(), 0)
        except (tk.TclError, ValueError):
            raise ValueError(f"{label} 포트 번호는 1부터 65535 사이의 숫자여야 합니다.")

    def _collect_settings(self) -> AppSettings:
        current = self.owner.settings
        detached_geometry = load_detached_geometry(current.detached_geometry)
        hytrans_port = self._read_port(self.hytrans_port_var, "HYTrans")
        overlayer_port = self._read_port(self.overlayer_port_var, "MekiOverlayer")
        if hytrans_port == overlayer_port:
            raise ValueError("HYTrans와 MekiOverlayer의 포트 번호는 같을 수 없습니다.")
        opacity = max(0.1, min(1.0, self.overlayer_opacity_var.get() / 100.0))
        text_size = max(8, min(96, int(self.overlayer_text_size_var.get())))
        return AppSettings(
            minimize_to_tray=self.minimize_to_tray_var.get(),
            main_always_on_top=self.main_topmost_var.get(),
            detached_always_on_top=self.detached_topmost_var.get(),
            detached_hide_titlebar=self.detached_hide_titlebar_var.get(),
            detached_fixed_size=self.detached_fixed_size_var.get(),
            simple_copy_complete=self.simple_copy_complete_var.get(),
            detached_geometry=detached_geometry,
            detached_fixed_width=current.detached_fixed_width,
            detached_fixed_height=current.detached_fixed_height,
            overlay_translation_mode=self.overlay_mode_var.get(),
            hytrans_port=hytrans_port,
            overlayer_port=overlayer_port,
            overlayer_always_on_top=self.overlayer_topmost_var.get(),
            overlayer_hide_titlebar=self.overlayer_hide_titlebar_var.get(),
            overlayer_fixed_size=self.overlayer_fixed_size_var.get(),
            overlayer_exclude_from_capture=self.overlayer_exclude_capture_var.get(),
            overlayer_bg_color=self.overlayer_bg_color_var.get(),
            overlayer_bg_opacity=opacity,
            overlayer_text_color=self.overlayer_text_color_var.get(),
            overlayer_text_size=text_size,
            overlayer_text_font=_normalize_font_name(self.overlayer_text_font_var.get()),
            suppress_magpie_launch_notice=self.suppress_magpie_notice_var.get(),
            debug_logging=self.debug_logging_var.get(),
        )

    def _on_save(self) -> None:
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            messagebox.showerror("MekiCopy", str(exc), parent=self)
            return
        if settings.detached_fixed_size:
            detached_size = _geometry_size(settings.detached_geometry)
            if detached_size:
                width, height = detached_size
                settings.detached_fixed_width = width
                settings.detached_fixed_height = height
        self.owner.apply_settings(settings, persist=True)
        self._on_close()

    def _on_test_connection(self) -> None:
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            messagebox.showerror("MekiCopy", str(exc), parent=self)
            return
        self.owner.apply_settings(settings, persist=True)
        self.owner._on_test_overlay_connection(parent=self)

    def _on_close(self) -> None:
        self.owner.settings_window = None
        self.destroy()


class DetachedOcrButtonApp:
    MIN_WIDTH = 120
    MIN_HEIGHT = 80
    RESIZE_MARGIN = 10
    SETTINGS_POLL_MS = 500

    def __init__(self) -> None:
        _enable_dpi_awareness()
        _set_app_user_model_id()
        _prepare_tk_library_paths()
        self.root = tk.Tk()
        self.settings = load_settings()
        self.root.title(DETACHED_WINDOW_TITLE)
        _set_window_icon(self.root)
        self._drag_action: str | None = None
        self._drag_start: tuple[int, int] | None = None
        self._drag_geometry: tuple[int, int, int, int] | None = None
        self._suppress_next_click = False
        self._settings_signature: tuple | None = None
        self._settings_after_id: str | None = None
        self._geometry_after_id: str | None = None
        self._closing = False

        self.button = tk.Button(
            self.root,
            text=self.ocr_action_label(),
            command=self._on_button_command,
            font=("Segoe UI", 14, "bold"),
        )
        self.button.pack(fill=tk.BOTH, expand=True)

        self.root.geometry(
            load_detached_geometry(
                self.settings.detached_geometry or DETACHED_DEFAULT_GEOMETRY
            )
        )
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self._on_configure)
        self.button.bind("<ButtonPress-1>", self._on_mouse_down, add="+")
        self.button.bind("<B1-Motion>", self._on_mouse_drag, add="+")
        self.button.bind("<ButtonRelease-1>", self._on_mouse_release, add="+")
        self.apply_settings()
        self._schedule_settings_poll()

    def run(self) -> None:
        self.root.mainloop()

    def ocr_action_label(self) -> str:
        return "번역 후 표시" if self.settings.overlay_translation_mode else "인식 후 복사"

    def current_size(self) -> tuple[int, int]:
        self.root.update_idletasks()
        return (
            max(self.MIN_WIDTH, self.root.winfo_width()),
            max(self.MIN_HEIGHT, self.root.winfo_height()),
        )

    def capture_geometry(self, persist: bool = True) -> str:
        if not self.root.winfo_exists():
            return ""
        self.root.update_idletasks()
        geometry = self.root.geometry()
        self.settings.detached_geometry = geometry
        width, height = self.current_size()
        if self.settings.detached_fixed_size:
            self.settings.detached_fixed_width = width
            self.settings.detached_fixed_height = height
        if persist:
            save_detached_geometry(geometry)
        return geometry

    def apply_settings(self) -> None:
        settings = self.settings
        self.button.config(text=self.ocr_action_label())
        self.root.withdraw()
        self.root.overrideredirect(settings.detached_hide_titlebar)
        if settings.detached_fixed_size:
            width = max(self.MIN_WIDTH, settings.detached_fixed_width)
            height = max(self.MIN_HEIGHT, settings.detached_fixed_height)
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.root.resizable(False, False)
            self.root.minsize(width, height)
            self.root.maxsize(width, height)
        else:
            self.root.resizable(True, True)
            self.root.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
            self.root.maxsize(10000, 10000)
        self.root.deiconify()
        self.root.attributes("-topmost", settings.detached_always_on_top)
        self._set_maximize_button_enabled(not settings.detached_fixed_size)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._settings_after_id:
            try:
                self.root.after_cancel(self._settings_after_id)
            except tk.TclError:
                pass
        if self._geometry_after_id:
            try:
                self.root.after_cancel(self._geometry_after_id)
            except tk.TclError:
                pass
        self.capture_geometry(persist=True)
        self.root.destroy()

    def _on_button_command(self) -> None:
        if self._suppress_next_click:
            self._suppress_next_click = False
            return
        self.settings = load_settings()
        region = load_detached_region()
        if not region:
            messagebox.showerror(
                "MekiCopy",
                "설정된 영역이 없습니다. MekiCopy에서 영역을 확정해주세요.",
                parent=self.root,
            )
            return
        try:
            region, _, _, _ = get_capture_manager().resolve_region(region)
            save_detached_region(region)
        except Exception as exc:
            _log_runtime_error("detached_prepare_region", exc)
            messagebox.showerror(
                "MekiCopy",
                f"캡처 영역 확인 실패:\n{exc}",
                parent=self.root,
            )
            return

        if self.settings.overlay_translation_mode:
            self._translate_and_show(region)
            return
        simple_feedback = self.settings.simple_copy_complete
        ocr_and_copy(
            region.left,
            region.top,
            region.width,
            region.height,
            notify=not simple_feedback,
            parent=self.root,
            on_copy_complete=self._show_feedback if simple_feedback else None,
        )

    def _translate_and_show(self, region: Region) -> None:
        text = ocr_region(
            region.left,
            region.top,
            region.width,
            region.height,
            parent=self.root,
        )
        if text is None:
            return
        if not text.strip():
            messagebox.showwarning(
                "MekiCopy",
                "인식된 텍스트가 없습니다.",
                parent=self.root,
            )
            return
        try:
            self._send_overlayer_config()
            _json_request(
                f"http://127.0.0.1:{self.settings.hytrans_port}/translate-and-show",
                {
                    "text": text,
                    "overlayUrl": (
                        f"http://127.0.0.1:{self.settings.overlayer_port}/show"
                    ),
                },
                timeout=130,
                method="POST",
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            _log_runtime_error("detached_translate_http", exc)
            messagebox.showerror(
                "MekiCopy",
                f"번역 후 표시 실패:\nHTTP {exc.code}\n{detail}",
                parent=self.root,
            )
            return
        except Exception as exc:
            _log_runtime_error("detached_translate", exc)
            messagebox.showerror(
                "MekiCopy",
                f"번역 후 표시 실패:\n{exc}",
                parent=self.root,
            )
            return
        if self.settings.simple_copy_complete:
            self._show_feedback()
        else:
            messagebox.showinfo(
                "MekiCopy",
                "번역 결과를 MekiOverlayer에 표시했습니다.",
                parent=self.root,
            )

    def _send_overlayer_config(self) -> None:
        settings = self.settings
        _json_request(
            f"http://127.0.0.1:{settings.overlayer_port}/config",
            {
                "topmost": settings.overlayer_always_on_top,
                "hide_titlebar": settings.overlayer_hide_titlebar,
                "fixed_size": settings.overlayer_fixed_size,
                "exclude_from_capture": settings.overlayer_exclude_from_capture,
                "bg_color": settings.overlayer_bg_color,
                "opacity": settings.overlayer_bg_opacity,
                "text_color": settings.overlayer_text_color,
                "text_size": settings.overlayer_text_size,
                "text_font": settings.overlayer_text_font,
            },
            timeout=3,
            method="POST",
        )

    def _show_feedback(self) -> None:
        button = self.button
        if not button.winfo_exists():
            return
        original = {
            "text": self.ocr_action_label(),
            "bg": button.cget("bg"),
            "fg": button.cget("fg"),
            "activebackground": button.cget("activebackground"),
            "activeforeground": button.cget("activeforeground"),
            "font": button.cget("font"),
        }
        button.update_idletasks()
        font_size = 72 if button.winfo_height() >= 220 else 42
        button.config(
            text="완료",
            bg="#16a34a",
            fg="white",
            activebackground="#16a34a",
            activeforeground="white",
            font=("Segoe UI", font_size, "bold"),
        )

        def restore() -> None:
            if button.winfo_exists():
                original["text"] = self.ocr_action_label()
                button.config(**original)

        button.after(1000, restore)

    def _settings_key(self, settings: AppSettings) -> tuple:
        return (
            settings.detached_always_on_top,
            settings.detached_hide_titlebar,
            settings.detached_fixed_size,
            settings.detached_fixed_width,
            settings.detached_fixed_height,
            settings.overlay_translation_mode,
        )

    def _schedule_settings_poll(self) -> None:
        if not self._closing:
            self._settings_after_id = self.root.after(
                self.SETTINGS_POLL_MS,
                self._poll_settings,
            )

    def _poll_settings(self) -> None:
        self._settings_after_id = None
        if self._closing:
            return
        latest = load_settings()
        signature = self._settings_key(latest)
        if signature != self._settings_signature:
            self.capture_geometry(persist=True)
            self.settings = latest
            self.apply_settings()
            self._settings_signature = signature
        else:
            self.settings = latest
        self._schedule_settings_poll()

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget == self.root and self.root.winfo_exists():
            self.settings.detached_geometry = self.root.geometry()
            if self._geometry_after_id:
                try:
                    self.root.after_cancel(self._geometry_after_id)
                except tk.TclError:
                    pass
            self._geometry_after_id = self.root.after(250, self._persist_geometry)

    def _persist_geometry(self) -> None:
        self._geometry_after_id = None
        if not self._closing and self.root.winfo_exists():
            self.capture_geometry(persist=True)

    def _on_mouse_down(self, event: tk.Event) -> None:
        if not self.settings.detached_hide_titlebar:
            return
        self.root.update_idletasks()
        self._drag_action = self._hit_test(event.x_root, event.y_root)
        self._drag_start = (event.x_root, event.y_root)
        self._drag_geometry = (
            self.root.winfo_x(),
            self.root.winfo_y(),
            self.root.winfo_width(),
            self.root.winfo_height(),
        )
        self._suppress_next_click = False

    def _on_mouse_drag(self, event: tk.Event) -> None:
        if not self._drag_action or not self._drag_start or not self._drag_geometry:
            return
        start_x, start_y = self._drag_start
        origin_x, origin_y, origin_width, origin_height = self._drag_geometry
        dx = event.x_root - start_x
        dy = event.y_root - start_y
        if abs(dx) + abs(dy) > 3:
            self._suppress_next_click = True

        if self.settings.detached_fixed_size or self._drag_action == "move":
            self.root.geometry(f"{origin_width}x{origin_height}+{origin_x + dx}+{origin_y + dy}")
            return

        x = origin_x
        y = origin_y
        width = origin_width
        height = origin_height
        if "e" in self._drag_action:
            width = max(self.MIN_WIDTH, origin_width + dx)
        if "s" in self._drag_action:
            height = max(self.MIN_HEIGHT, origin_height + dy)
        if "w" in self._drag_action:
            width = max(self.MIN_WIDTH, origin_width - dx)
            x = origin_x + origin_width - width
        if "n" in self._drag_action:
            height = max(self.MIN_HEIGHT, origin_height - dy)
            y = origin_y + origin_height - height
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _on_mouse_release(self, _event: tk.Event) -> None:
        self._drag_action = None
        self._drag_start = None
        self._drag_geometry = None

    def _hit_test(self, x_root: int, y_root: int) -> str:
        if self.settings.detached_fixed_size:
            return "move"
        x = x_root - self.root.winfo_rootx()
        y = y_root - self.root.winfo_rooty()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        left = x <= self.RESIZE_MARGIN
        right = x >= width - self.RESIZE_MARGIN
        top = y <= self.RESIZE_MARGIN
        bottom = y >= height - self.RESIZE_MARGIN
        if top and left:
            return "nw"
        if top and right:
            return "ne"
        if bottom and left:
            return "sw"
        if bottom and right:
            return "se"
        if left:
            return "w"
        if right:
            return "e"
        if top:
            return "n"
        if bottom:
            return "s"
        return "move"

    def _set_maximize_button_enabled(self, enabled: bool) -> None:
        if os.name != "nt" or self.settings.detached_hide_titlebar:
            return
        try:
            hwnd = self.root.winfo_id()
            user32 = ctypes.windll.user32
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                get_window_long = user32.GetWindowLongPtrW
                set_window_long = user32.SetWindowLongPtrW
            else:
                get_window_long = user32.GetWindowLongW
                set_window_long = user32.SetWindowLongW
            get_window_long.restype = ctypes.c_void_p
            set_window_long.restype = ctypes.c_void_p
            style = int(get_window_long(hwnd, -16))
            maximize_box = 0x00010000
            if enabled:
                style |= maximize_box
            else:
                style &= ~maximize_box
            set_window_long(hwnd, -16, style)
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass


class MainWindow(tk.Tk):
    def __init__(self) -> None:
        _enable_dpi_awareness()
        _set_app_user_model_id()
        _prepare_tk_library_paths()
        super().__init__()
        self.settings = load_settings()
        self.detached_process: subprocess.Popen | None = None
        self.settings_window: SettingsWindow | None = None
        self.hytrans_process: subprocess.Popen | None = None
        self.overlayer_process: subprocess.Popen | None = None
        self.magpie_process: subprocess.Popen | None = None
        self._magpie_install_results: queue.Queue[tuple[bool, str]] = queue.Queue()
        self._magpie_installing = False
        self.tray_icon = WindowsTrayIcon(self, "MekiCopy", self._restore_from_tray)
        self.title("MekiCopy")
        _set_window_icon(self)
        self.geometry("460x400")
        self.resizable(False, False)
        self.draft_region: Region | None = None
        self.active_region: Region | None = None
        self.active_region_ratio: RegionRatio | None = None
        self.active_monitor: MonitorInfo | None = None
        self.capture_status_text = "캡처 준비"
        self._closing = False
        self._restoring_from_tray = False
        self._build_ui()
        self.apply_settings(self.settings, persist=False)
        self.bind("<Unmap>", self._on_unmap)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        header = tk.Label(self, text="MekiCopy", font=("Segoe UI", 12, "bold"))
        header.pack(pady=(2, 0))

        body = tk.Frame(self)
        body.pack(padx=10, pady=(0, 7), fill=tk.BOTH, expand=True)

        self.tab_buttons: dict[str, tk.Button] = {}
        self.tab_frames: dict[str, tk.Frame] = {}
        self.active_tab_name = "영역"

        tab_bar = tk.Frame(body, width=118)
        tab_bar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        tab_bar.pack_propagate(False)

        content = tk.Frame(body)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for tab_name in ("영역", "캡쳐", "도구/설정", "행동"):
            button = tk.Button(
                tab_bar,
                text=tab_name,
                command=lambda name=tab_name: self._select_tab(name),
                anchor="w",
                padx=10,
                pady=8,
                relief=tk.FLAT,
            )
            button.pack(fill=tk.X, pady=2)
            self.tab_buttons[tab_name] = button

            frame = tk.Frame(content)
            self.tab_frames[tab_name] = frame

        self._build_region_tab(self.tab_frames["영역"])
        self._build_capture_tab(self.tab_frames["캡쳐"])
        self._build_tools_tab(self.tab_frames["도구/설정"])
        self._build_action_tab(self.tab_frames["행동"])

        self._select_tab(self.active_tab_name)
        self._update_status()

    def _build_region_tab(self, frame: tk.Frame) -> None:
        self.status_label = tk.Label(frame, text="", justify="left", wraplength=300)
        self.status_label.pack(pady=(4, 12), fill=tk.X)

        buttons = [
            ("임시 영역 선택", self._on_select_region),
            ("현재 임시/확정 영역 보기", self._on_view_regions),
            ("임시 영역을 확정", self._on_set_region),
            ("확정 영역을 북마크에 추가", self._on_save_active_bookmark),
            ("북마크 영역 불러오기", self._on_load_bookmark),
        ]
        for text, command in buttons:
            self._pack_tab_button(frame, text, command)

    def _build_capture_tab(self, frame: tk.Frame) -> None:
        self.capture_status_label = tk.Label(
            frame,
            text=self.capture_status_text,
            justify="left",
            wraplength=300,
            fg="#1d4ed8",
        )
        self.capture_status_label.pack(pady=(4, 12), fill=tk.X)

        buttons = [
            ("캡쳐 진단 저장", self._on_capture_diagnostics),
            ("모니터 다시 검색", self._on_refresh_capture_monitors),
            ("비율 좌표로 영역 복구", self._on_restore_region_from_ratio),
        ]
        for text, command in buttons:
            self._pack_tab_button(frame, text, command)
        self.magpie_button = self._pack_tab_button(
            frame,
            "Magpie 실행",
            self._on_start_magpie,
        )

    def _build_tools_tab(self, frame: tk.Frame) -> None:
        self.hytrans_button = self._pack_tab_button(
            frame,
            "HYTrans 서버 실행",
            self._on_start_hytrans,
        )
        self.overlayer_button = self._pack_tab_button(
            frame,
            "MekiOverlayer 실행",
            self._on_start_overlayer,
        )
        self._pack_tab_button(
            frame,
            "모든 도구 연결 상태확인",
            self._on_test_overlay_connection,
        )
        self._pack_tab_button(frame, "설정", self._on_open_settings)

    def _build_action_tab(self, frame: tk.Frame) -> None:
        self.detach_button = self._pack_tab_button(
            frame,
            "",
            self._on_detach_ocr_button,
        )

        ocr_button_frame = tk.Frame(frame, height=OCR_BUTTON_HEIGHT_PX)
        ocr_button_frame.pack(fill=tk.X, pady=(10, 4))
        ocr_button_frame.pack_propagate(False)
        self.ocr_button = tk.Button(
            ocr_button_frame,
            text="인식 후 복사",
            command=lambda: self._on_ocr_copy(source_button=self.ocr_button),
            font=("Segoe UI", 14, "bold"),
        )
        self.ocr_button.pack(fill=tk.BOTH, expand=True)

    def _pack_tab_button(
        self,
        frame: tk.Frame,
        text: str,
        command: Callable[[], None],
    ) -> tk.Button:
        button = tk.Button(frame, text=text, command=command, anchor="center")
        button.pack(fill=tk.X, pady=4)
        return button

    def _select_tab(self, name: str) -> None:
        self.active_tab_name = name
        for tab_name, frame in self.tab_frames.items():
            if tab_name == name:
                frame.pack(fill=tk.BOTH, expand=True)
            else:
                frame.pack_forget()
        for tab_name, button in self.tab_buttons.items():
            if tab_name == name:
                button.config(relief=tk.SUNKEN, bg="#dbeafe")
            else:
                button.config(relief=tk.FLAT, bg=self.cget("bg"))

    def ocr_action_label(self) -> str:
        if self.settings.overlay_translation_mode:
            return "번역 후 표시"
        return "인식 후 복사"

    def _apply_overlay_mode_ui(self) -> None:
        overlay_state = tk.NORMAL if self.settings.overlay_translation_mode else tk.DISABLED
        for button_name in ("hytrans_button", "overlayer_button"):
            button = getattr(self, button_name, None)
            if button is not None and button.winfo_exists():
                button.config(state=overlay_state)
        self.ocr_button.config(text=self.ocr_action_label())
        self.detach_button.config(text=f"'{self.ocr_action_label()}' 버튼 분리하기")

    def _format_region(self, region: Region | None) -> str:
        if not region:
            return "설정되지 않음"
        return f"left={region.left}, top={region.top}, width={region.width}, height={region.height}"

    def _update_status(self) -> None:
        draft_text = self._format_region(self.draft_region)
        active_text = self._format_region(self.active_region)
        self.status_label.config(
            text=f"임시 영역 :\n{draft_text}\n확정 영역 :\n{active_text}"
        )

    def _set_capture_status(self, text: str) -> None:
        self.capture_status_text = text
        if hasattr(self, "capture_status_label") and self.capture_status_label.winfo_exists():
            self.capture_status_label.config(text=text)

    def _set_capture_status_from_last_result(self) -> None:
        result = get_capture_manager().last_result
        if result is None:
            return
        stats = ""
        if result.stats is not None:
            stats = f" / mean={result.stats.mean:.1f}, std={result.stats.stddev:.1f}"
        self._set_capture_status(
            f"캡처 상태: {result.status} / 방식: {result.strategy}{stats}"
        )

    def _remember_active_region_profile(self) -> None:
        if not self.active_region:
            self.active_region_ratio = None
            self.active_monitor = None
            return
        try:
            save_detached_region(self.active_region)
        except OSError as exc:
            _log_runtime_error("save_detached_region", exc)
        try:
            manager = get_capture_manager()
            manager.refresh_if_display_changed()
            monitors = manager.get_real_monitors()
            monitor = _find_monitor_for_region(self.active_region, monitors)
            if monitor is None:
                self.active_region_ratio = None
                self.active_monitor = None
                self._set_capture_status("캡처 상태: OCR 영역과 일치하는 모니터를 찾지 못했습니다.")
                return
            self.active_monitor = monitor
            self.active_region_ratio = _ratio_for_region(self.active_region, monitor)
            self._set_capture_status(
                (
                    "캡처 상태: 영역 프로필 저장됨 "
                    f"(monitor={monitor.index}, ratio={self.active_region_ratio.to_dict()})"
                )
            )
        except Exception as exc:
            _log_runtime_error("remember_active_region_profile", exc)
            self._set_capture_status(f"캡처 상태: 영역 프로필 저장 실패 - {exc}")

    def _prepare_active_region_for_capture(self) -> bool:
        if not self.active_region:
            messagebox.showerror("MekiCopy", "설정된 영역이 없습니다.", parent=self)
            return False
        try:
            region, monitor, ratio, messages = get_capture_manager().resolve_region(
                self.active_region,
                previous_monitor=self.active_monitor,
                ratio=self.active_region_ratio,
            )
        except Exception as exc:
            _log_runtime_error("prepare_active_region_for_capture", exc)
            messagebox.showerror("MekiCopy", f"캡처 영역 확인 실패:\n{exc}", parent=self)
            return False

        if region.width < MIN_SIZE_PX or region.height < MIN_SIZE_PX:
            messagebox.showerror(
                "MekiCopy",
                "OCR 영역이 현재 화면에서 너무 작습니다. 영역을 다시 지정해주세요.",
                parent=self,
            )
            return False

        changed = not _regions_equal(region, self.active_region)
        self.active_region = region
        try:
            save_detached_region(region)
        except OSError as exc:
            _log_runtime_error("save_detached_region", exc)
        self.active_monitor = monitor
        self.active_region_ratio = ratio
        if changed:
            self.draft_region = Region(
                left=region.left,
                top=region.top,
                width=region.width,
                height=region.height,
            )
            self._update_status()
        if messages:
            self._set_capture_status(" / ".join(messages))
        return True

    def _on_refresh_capture_monitors(self) -> None:
        try:
            get_capture_manager().reinitialize("manual_refresh")
            self._prepare_active_region_for_capture()
            monitors = get_capture_manager().get_monitors()
            self._set_capture_status(
                f"캡처 상태: 모니터 {len(monitors)}개 재검색 완료 / {_monitor_signature(monitors)}"
            )
        except Exception as exc:
            _log_runtime_error("refresh_capture_monitors", exc)
            messagebox.showerror("MekiCopy", f"모니터 재검색 실패:\n{exc}", parent=self)

    def _on_restore_region_from_ratio(self) -> None:
        if not self.active_region_ratio:
            messagebox.showerror("MekiCopy", "복구할 비율 좌표가 없습니다.", parent=self)
            return
        monitors = get_capture_manager().get_real_monitors()
        monitor = _match_monitor_by_previous_rect(self.active_monitor, monitors)
        if monitor is None:
            messagebox.showerror("MekiCopy", "대상 모니터를 찾지 못했습니다.", parent=self)
            return
        region = _region_from_ratio(monitor, self.active_region_ratio)
        region = _clamp_region_to_monitor(region, monitor)
        self.active_region = region
        self.draft_region = Region(
            left=region.left,
            top=region.top,
            width=region.width,
            height=region.height,
        )
        self.active_monitor = monitor
        self.active_region_ratio = _ratio_for_region(region, monitor)
        try:
            save_detached_region(region)
        except OSError as exc:
            _log_runtime_error("save_detached_region", exc)
        self._update_status()
        self._set_capture_status("캡처 상태: 비율 좌표로 OCR 영역을 복구했습니다.")

    def _on_capture_diagnostics(self) -> None:
        try:
            directory = run_capture_diagnostics(self.active_region)
            self._set_capture_status(f"캡처 진단 저장 완료: {directory}")
            messagebox.showinfo(
                "MekiCopy",
                f"캡처 진단 파일을 저장했습니다:\n{directory}",
                parent=self,
            )
        except Exception as exc:
            _log_runtime_error("capture_diagnostics", exc)
            messagebox.showerror("MekiCopy", f"캡처 진단 실패:\n{exc}", parent=self)

    def _launch_magpie(self, executable: str) -> None:
        if not self.settings.suppress_magpie_launch_notice:
            messagebox.showinfo("MagPie 실행 안내", MAGPIE_LAUNCH_NOTICE, parent=self)
        try:
            self.magpie_process = subprocess.Popen(
                [executable],
                cwd=os.path.dirname(executable),
            )
            self._set_capture_status(f"MagPie 실행됨: {executable}")
            _log_runtime_message("start_magpie", executable)
        except Exception as exc:
            _log_runtime_error("start_magpie", exc)
            messagebox.showerror("MekiCopy", f"MagPie 실행 실패:\n{exc}", parent=self)

    def _on_start_magpie(self) -> None:
        executable = _find_magpie_executable()
        if executable:
            self._launch_magpie(executable)
            return
        if self._magpie_installing:
            messagebox.showinfo(
                "MekiCopy",
                "MagPie 최신 버전을 다운로드하고 있습니다.",
                parent=self,
            )
            return

        self._magpie_installing = True
        self.magpie_button.config(state=tk.DISABLED, text="MagPie 다운로드 중...")
        self._set_capture_status("MagPie 최신 버전을 다운로드하고 압축을 푸는 중입니다...")

        def install_worker() -> None:
            try:
                installed_executable = _install_latest_magpie()
                self._magpie_install_results.put((True, installed_executable))
            except Exception as exc:
                _log_runtime_error("install_magpie", exc)
                self._magpie_install_results.put((False, str(exc)))

        threading.Thread(target=install_worker, daemon=True).start()
        self.after(100, self._poll_magpie_install_result)

    def _poll_magpie_install_result(self) -> None:
        try:
            succeeded, result = self._magpie_install_results.get_nowait()
        except queue.Empty:
            if self._magpie_installing and not self._closing:
                self.after(100, self._poll_magpie_install_result)
            return

        self._magpie_installing = False
        self.magpie_button.config(state=tk.NORMAL, text="Magpie 실행")
        if succeeded:
            self._set_capture_status(f"MagPie 설치 완료: {result}")
            self._launch_magpie(result)
        else:
            self._set_capture_status("MagPie 다운로드 또는 설치 실패")
            messagebox.showerror(
                "MekiCopy",
                f"MagPie 다운로드 또는 설치 실패:\n{result}",
                parent=self,
            )

    def _on_select_region(self) -> None:
        initial = self.draft_region or self.active_region
        selection = run_selection(
            initial_region=initial,
            capture_on_enter=False,
            parent=self,
        )
        if selection:
            self.draft_region = selection
            self._update_status()

    def _on_view_regions(self) -> None:
        if not self.draft_region and not self.active_region:
            messagebox.showerror("MekiCopy", "표시할 영역이 없습니다.")
            return
        run_region_view(self.draft_region, self.active_region, parent=self)

    def _on_save_active_bookmark(self) -> None:
        if not self.active_region:
            messagebox.showerror("MekiCopy", "확정 영역이 없습니다.")
            return
        name = simpledialog.askstring("MekiCopy", "북마크 이름을 입력하세요", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        bookmarks = load_bookmarks()
        if name in bookmarks and not messagebox.askyesno(
            "MekiCopy",
            "같은 이름의 북마크가 있습니다. 덮어쓸까요?",
            parent=self,
        ):
            return
        bookmarks[name] = Bookmark(
            name=name,
            left=self.active_region.left,
            top=self.active_region.top,
            width=self.active_region.width,
            height=self.active_region.height,
        )
        save_bookmarks(bookmarks)
        messagebox.showinfo("MekiCopy", "확정 영역이 북마크에 추가되었습니다!", parent=self)

    def _load_bookmark_region(self, bookmark: Bookmark) -> None:
        region = Region(
            left=bookmark.left,
            top=bookmark.top,
            width=bookmark.width,
            height=bookmark.height,
        )
        self.draft_region = region
        self.active_region = Region(
            left=region.left,
            top=region.top,
            width=region.width,
            height=region.height,
        )
        self._remember_active_region_profile()
        self._update_status()

    def _on_load_bookmark(self) -> None:
        bookmark = pick_bookmark(parent=self)
        if not bookmark:
            return
        self._load_bookmark_region(bookmark)

    def _on_set_region(self) -> None:
        if not self.draft_region:
            messagebox.showerror("MekiCopy", "먼저 영역을 지정하거나 북마크를 불러오세요.")
            return
        self.active_region = Region(
            left=self.draft_region.left,
            top=self.draft_region.top,
            width=self.draft_region.width,
            height=self.draft_region.height,
        )
        self._remember_active_region_profile()
        self._update_status()
        messagebox.showinfo("MekiCopy", "인식 영역이 설정되었습니다!")

    def _on_detach_ocr_button(self) -> None:
        if self.active_region:
            try:
                save_detached_region(self.active_region)
            except OSError as exc:
                _log_runtime_error("save_detached_region", exc)
        try:
            self.detached_process = _launch_detached_button_process()
        except Exception as exc:
            _log_runtime_error("start_detached_button", exc)
            messagebox.showerror(
                "MekiCopy",
                f"분리 버튼 실행 실패:\n{exc}",
                parent=self,
            )

    def _on_open_settings(self) -> None:
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        self.settings_window = SettingsWindow(self)

    def _hytrans_base_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.hytrans_port}"

    def _overlayer_base_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.overlayer_port}"

    def _overlayer_show_url(self) -> str:
        return f"{self._overlayer_base_url()}/show"

    def _overlayer_config_payload(self) -> dict:
        return {
            "topmost": self.settings.overlayer_always_on_top,
            "hide_titlebar": self.settings.overlayer_hide_titlebar,
            "fixed_size": self.settings.overlayer_fixed_size,
            "exclude_from_capture": self.settings.overlayer_exclude_from_capture,
            "bg_color": self.settings.overlayer_bg_color,
            "opacity": self.settings.overlayer_bg_opacity,
            "text_color": self.settings.overlayer_text_color,
            "text_size": self.settings.overlayer_text_size,
            "text_font": self.settings.overlayer_text_font,
            "debug_log": self.settings.debug_logging,
        }

    def _send_overlayer_config(self, log_errors: bool = True) -> bool:
        try:
            _json_request(
                f"{self._overlayer_base_url()}/config",
                self._overlayer_config_payload(),
                timeout=2,
                method="POST",
            )
            return True
        except Exception as exc:
            if log_errors:
                _log_runtime_error("send_overlayer_config", exc)
            return False

    def _on_start_hytrans(self) -> None:
        try:
            _json_request(f"{self._hytrans_base_url()}/health", timeout=1)
            try:
                ready = _json_request(f"{self._hytrans_base_url()}/ready", timeout=1)
                if not (ready.get("workerConnected") or ready.get("ready")):
                    _json_request(
                        f"{self._hytrans_base_url()}/worker/reopen",
                        {},
                        timeout=3,
                        method="POST",
                    )
                    messagebox.showinfo(
                        "MekiCopy",
                        "HYTrans Worker 창을 다시 열었습니다.",
                        parent=self,
                    )
                    return
            except Exception as exc:
                _log_runtime_error("restart_hytrans_worker", exc)
            messagebox.showinfo("MekiCopy", "HYTrans 서버가 이미 실행 중입니다.", parent=self)
            return
        except Exception:
            pass

        command = _find_companion_executable("HYTrans", "hytrans_main.py")
        if not command:
            messagebox.showerror(
                "MekiCopy",
                "HYTrans 실행 파일을 찾을 수 없습니다.",
                parent=self,
            )
            return

        command += [
            "--port",
            str(self.settings.hytrans_port),
            "--overlay-url",
            self._overlayer_show_url(),
        ]
        if self.settings.debug_logging:
            command.append("--debug-log")
        try:
            self.hytrans_process = subprocess.Popen(
                command,
                cwd=_get_app_dir(),
                startupinfo=_startupinfo_for_background(),
            )
            _log_runtime_message("start_hytrans", " ".join(command))
            messagebox.showinfo("MekiCopy", "HYTrans 서버를 실행했습니다.", parent=self)
        except Exception as exc:
            _log_runtime_error("start_hytrans", exc)
            messagebox.showerror("MekiCopy", f"HYTrans 실행 실패:\n{exc}", parent=self)

    def _on_start_overlayer(self) -> None:
        try:
            _json_request(f"{self._overlayer_base_url()}/health", timeout=1)
            self._send_overlayer_config()
            messagebox.showinfo(
                "MekiCopy",
                "MekiOverlayer가 이미 실행 중입니다. 현재 설정을 적용했습니다.",
                parent=self,
            )
            return
        except Exception:
            pass

        command = _find_companion_executable("MekiOverlayer", "meki_overlayer.py")
        if not command:
            messagebox.showerror(
                "MekiCopy",
                "MekiOverlayer 실행 파일을 찾을 수 없습니다.",
                parent=self,
            )
            return

        cfg = self._overlayer_config_payload()
        command += [
            "--port",
            str(self.settings.overlayer_port),
            "--topmost",
            "1" if cfg["topmost"] else "0",
            "--hide-titlebar",
            "1" if cfg["hide_titlebar"] else "0",
            "--fixed-size",
            "1" if cfg["fixed_size"] else "0",
            "--exclude-from-capture",
            "1" if cfg["exclude_from_capture"] else "0",
            "--bg-color",
            str(cfg["bg_color"]),
            "--opacity",
            str(cfg["opacity"]),
            "--text-color",
            str(cfg["text_color"]),
            "--text-size",
            str(cfg["text_size"]),
            "--text-font",
            str(cfg["text_font"]),
        ]
        if self.settings.debug_logging:
            command.append("--debug-log")
        try:
            self.overlayer_process = subprocess.Popen(
                command,
                cwd=_get_app_dir(),
                startupinfo=None,
            )
            _log_runtime_message("start_overlayer", " ".join(command))
            messagebox.showinfo("MekiCopy", "MekiOverlayer를 실행했습니다.", parent=self)
        except Exception as exc:
            _log_runtime_error("start_overlayer", exc)
            messagebox.showerror("MekiCopy", f"MekiOverlayer 실행 실패:\n{exc}", parent=self)

    def _on_test_overlay_connection(self, parent: tk.Misc | None = None) -> None:
        owner = parent or self
        failures: list[str] = []
        try:
            _json_request(f"{self._hytrans_base_url()}/health", timeout=2)
        except Exception as exc:
            failures.append(f"HYTrans 연결 실패: {exc}")
        try:
            _json_request(f"{self._overlayer_base_url()}/health", timeout=2)
        except Exception as exc:
            failures.append(f"MekiOverlayer 연결 실패: {exc}")

        if failures:
            messagebox.showerror("MekiCopy", "\n".join(failures), parent=owner)
            return

        self._send_overlayer_config()
        try:
            _json_request(
                f"{self._hytrans_base_url()}/overlay-test",
                {"text": "MekiCopy 연결 테스트", "overlayUrl": self._overlayer_show_url()},
                timeout=5,
                method="POST",
            )
            ready = _json_request(f"{self._hytrans_base_url()}/ready", timeout=2)
            ready_text = "번역 모델 준비됨" if ready.get("ready") else "번역 모델 로딩 중"
            messagebox.showinfo(
                "MekiCopy",
                f"HYTrans -> MekiOverlayer 표시 흐름이 정상입니다.\n{ready_text}",
                parent=owner,
            )
        except Exception as exc:
            _log_runtime_error("test_overlay_connection", exc)
            messagebox.showerror("MekiCopy", f"연결 테스트 실패:\n{exc}", parent=owner)

    def _request_translate_and_show(self, text: str) -> dict:
        return _json_request(
            f"{self._hytrans_base_url()}/translate-and-show",
            {"text": text, "overlayUrl": self._overlayer_show_url()},
            timeout=130,
            method="POST",
        )

    def _on_ocr_copy(self, source_button: tk.Button | None = None) -> None:
        if not self.active_region:
            messagebox.showerror("MekiCopy", "설정된 영역이 없습니다.", parent=self)
            return
        if not self._prepare_active_region_for_capture():
            return
        simple_feedback = self.settings.simple_copy_complete
        if self.settings.overlay_translation_mode:
            self._ocr_translate_and_show(
                source_button=source_button or self.ocr_button,
                simple_feedback=simple_feedback,
            )
            return
        ocr_and_copy(
            self.active_region.left,
            self.active_region.top,
            self.active_region.width,
            self.active_region.height,
            notify=not simple_feedback,
            parent=self,
            on_copy_complete=(
                lambda: self._show_copy_feedback(source_button or self.ocr_button)
                if simple_feedback
                else None
            ),
        )
        self._set_capture_status_from_last_result()

    def _ocr_translate_and_show(
        self,
        source_button: tk.Button,
        simple_feedback: bool,
    ) -> None:
        assert self.active_region is not None
        text = ocr_region(
            self.active_region.left,
            self.active_region.top,
            self.active_region.width,
            self.active_region.height,
            parent=self,
        )
        self._set_capture_status_from_last_result()
        if text is None:
            return
        if not text.strip():
            messagebox.showwarning("MekiCopy", "인식된 텍스트가 없습니다.", parent=self)
            return
        try:
            self._send_overlayer_config()
            self._request_translate_and_show(text)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            _log_runtime_error("ocr_translate_and_show_http", exc)
            messagebox.showerror(
                "MekiCopy",
                f"번역 후 표시 실패:\nHTTP {exc.code}\n{detail}",
                parent=self,
            )
            return
        except Exception as exc:
            _log_runtime_error("ocr_translate_and_show", exc)
            messagebox.showerror("MekiCopy", f"번역 후 표시 실패:\n{exc}", parent=self)
            return
        if simple_feedback:
            self._show_copy_feedback(source_button)
        else:
            messagebox.showinfo(
                "MekiCopy",
                "번역 결과를 MekiOverlayer에 표시했습니다.",
                parent=self,
            )

    def _show_copy_feedback(self, button: tk.Button) -> None:
        if not button.winfo_exists():
            return
        previous_after_id = getattr(button, "_mekicopy_feedback_after_id", None)
        if previous_after_id:
            try:
                button.after_cancel(previous_after_id)
            except tk.TclError:
                pass
        original = getattr(button, "_mekicopy_feedback_original", None)
        if original is None:
            original = {
                "text": button.cget("text"),
                "bg": button.cget("bg"),
                "fg": button.cget("fg"),
                "activebackground": button.cget("activebackground"),
                "activeforeground": button.cget("activeforeground"),
                "font": button.cget("font"),
            }
            setattr(button, "_mekicopy_feedback_original", original)

        button.update_idletasks()
        font_size = 72 if button.winfo_height() >= 220 else 42
        button.config(
            text="✓",
            bg="#16a34a",
            fg="white",
            activebackground="#16a34a",
            activeforeground="white",
            font=("Segoe UI", font_size, "bold"),
        )

        def restore() -> None:
            if not button.winfo_exists():
                return
            button.config(**original)
            setattr(button, "_mekicopy_feedback_original", None)
            setattr(button, "_mekicopy_feedback_after_id", None)

        after_id = button.after(1000, restore)
        setattr(button, "_mekicopy_feedback_after_id", after_id)

    def apply_settings(self, settings: AppSettings, persist: bool) -> None:
        self.settings = settings
        self.attributes("-topmost", self.settings.main_always_on_top)
        self._apply_overlay_mode_ui()
        if self.settings.overlay_translation_mode:
            self._send_overlayer_config(log_errors=False)
        if persist:
            save_settings(self.settings)

    def _on_unmap(self, event: tk.Event) -> None:
        if event.widget != self or not self.settings.minimize_to_tray:
            return
        if self._closing or self._restoring_from_tray:
            return
        if self.state() == "iconic":
            self.after(0, self._minimize_to_tray)

    def _minimize_to_tray(self) -> None:
        if self.tray_icon.show():
            self.withdraw()

    def _restore_from_tray(self) -> None:
        self._restoring_from_tray = True
        try:
            self.tray_icon.hide()
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
            self.attributes("-topmost", self.settings.main_always_on_top)
        finally:
            self.after(100, lambda: setattr(self, "_restoring_from_tray", False))

    def _on_close(self) -> None:
        self._closing = True
        self.tray_icon.close()
        _close_detached_window()
        save_settings(self.settings)
        for process in (self.hytrans_process, self.overlayer_process):
            if _is_process_alive(process):
                try:
                    process.terminate()
                except Exception:
                    pass
        self.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiCopy 영역 OCR 도구")
    parser.add_argument("--bookmark", help="저장된 북마크 이름으로 캡처")
    parser.add_argument("--pick-bookmark", action="store_true", help="북마크 목록에서 선택")
    parser.add_argument("--adjust-bookmark", help="북마크 영역을 불러와 미세조정 후 저장")
    parser.add_argument("--self-test-runtime", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test-ui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--self-test-tray-stress",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--detached-button", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--self-test-detached-button",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--self-test-detached-survival",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--self-test-detached-launcher",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def run_detached_button() -> None:
    mutex = _WindowsNamedMutex(DETACHED_MUTEX_NAME)
    if mutex.already_exists:
        _activate_detached_window()
        mutex.close()
        return
    app: DetachedOcrButtonApp | None = None
    try:
        app = DetachedOcrButtonApp()
        app.run()
    except Exception as exc:
        _log_runtime_error("detached_button", exc)
        raise
    finally:
        try:
            if app and app.root.winfo_exists():
                app.close()
        except tk.TclError:
            pass
        finally:
            mutex.close()


def run_detached_button_self_test() -> None:
    _log_runtime_message("self_test_detached_button", "starting")
    app: DetachedOcrButtonApp | None = None
    try:
        app = DetachedOcrButtonApp()
        app.root.update_idletasks()
        if not app.root.winfo_exists() or not app.button.winfo_exists():
            raise RuntimeError("detached button UI was not created")
        if app.root.title() != DETACHED_WINDOW_TITLE:
            raise RuntimeError("detached button window title is not stable")
        if app.button.cget("text") != app.ocr_action_label():
            raise RuntimeError("detached button label does not match settings")
        command = _mekicopy_process_command("--detached-button")
        if "--detached-button" not in command:
            raise RuntimeError("detached process command is invalid")
        _log_runtime_message(
            "self_test_detached_button",
            f"command: {command}\nwindow: {app.root.geometry()}",
        )
    except Exception as exc:
        _log_runtime_error("self_test_detached_button", exc)
        raise SystemExit(1)
    finally:
        if app and app.root.winfo_exists():
            app._closing = True
            app.root.destroy()


def run_detached_survival_self_test() -> None:
    if os.name != "nt":
        return
    _log_runtime_message("self_test_detached_survival", "starting")
    if _find_detached_window():
        _close_detached_window()
        deadline = time.monotonic() + 5.0
        while _find_detached_window() and time.monotonic() < deadline:
            time.sleep(0.05)
        if _find_detached_window():
            raise RuntimeError("an existing detached button window could not be closed")

    launcher = subprocess.Popen(
        _mekicopy_process_command("--self-test-detached-launcher"),
        cwd=_get_app_dir(),
        close_fds=True,
    )
    try:
        exit_code = launcher.wait(timeout=30)
    except subprocess.TimeoutExpired:
        launcher.kill()
        _close_detached_window()
        raise RuntimeError("detached survival launcher did not exit")
    if exit_code != 0:
        _close_detached_window()
        raise RuntimeError(f"detached survival launcher exited with {exit_code}")

    hwnd = None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        hwnd = _find_detached_window()
        if hwnd:
            break
        time.sleep(0.05)
    if not hwnd:
        _close_detached_window()
        raise RuntimeError("detached button did not survive its launcher process")

    process_id = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value or process_id.value == launcher.pid:
        _close_detached_window()
        raise RuntimeError("detached button is not running in an independent process")
    _log_runtime_message(
        "self_test_detached_survival",
        f"launcher_pid: {launcher.pid}\ndetached_pid: {process_id.value}",
    )
    # FindWindow can succeed while Tk is still finishing constructor work,
    # before mainloop has started dispatching WM_CLOSE.
    time.sleep(1.0)
    _close_detached_window()
    deadline = time.monotonic() + 30.0
    while _find_detached_window() and time.monotonic() < deadline:
        time.sleep(0.05)
    if _find_detached_window():
        raise RuntimeError("detached button did not close after survival test")


def run_tray_stress_self_test(cycles: int = 100) -> None:
    _log_runtime_message("self_test_tray_stress", f"starting cycles={cycles}")
    app: MainWindow | None = None
    try:
        app = MainWindow()
        app.settings.minimize_to_tray = True
        app.update_idletasks()
        if os.name != "nt":
            return

        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        tray_hwnd = None
        for cycle in range(1, cycles + 1):
            app.iconify()
            minimize_deadline = time.monotonic() + 3.0
            while time.monotonic() < minimize_deadline:
                app.update()
                if app.tray_icon._active and app.state() == "withdrawn":
                    break
                time.sleep(0.005)
            if not app.tray_icon._active or app.state() != "withdrawn":
                raise RuntimeError(
                    f"cycle {cycle}: minimize-to-tray failed (state={app.state()})"
                )

            current_hwnd = app.tray_icon._hwnd
            if not current_hwnd:
                raise RuntimeError(f"cycle {cycle}: tray callback window is missing")
            if tray_hwnd is None:
                tray_hwnd = current_hwnd
            elif current_hwnd != tray_hwnd:
                raise RuntimeError(f"cycle {cycle}: tray callback HWND changed")

            # Explorer emits the double-click notification asynchronously. Two
            # queued notifications also verify duplicate clicks are coalesced.
            for _ in range(2):
                if not user32.PostMessageW(
                    current_hwnd,
                    app.tray_icon.WM_TRAY_CALLBACK,
                    1,
                    app.tray_icon.WM_LBUTTONDBLCLK,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())

            restore_deadline = time.monotonic() + 3.0
            while time.monotonic() < restore_deadline:
                app.update()
                if (
                    app.state() == "normal"
                    and not app.tray_icon._active
                    and not app._restoring_from_tray
                ):
                    break
                time.sleep(0.005)
            if app.state() != "normal" or app.tray_icon._active:
                raise RuntimeError(
                    f"cycle {cycle}: tray restore failed "
                    f"(state={app.state()}, active={app.tray_icon._active})"
                )

        _log_runtime_message(
            "self_test_tray_stress",
            f"completed cycles={cycles}\ntray_hwnd={tray_hwnd}",
        )
    except Exception as exc:
        _log_runtime_error("self_test_tray_stress", exc)
        raise SystemExit(1)
    finally:
        if app:
            try:
                app.tray_icon.close()
                app.destroy()
            except tk.TclError:
                pass


def run_ui_self_test() -> None:
    _log_runtime_message("self_test_ui", "starting")
    app: MainWindow | None = None
    try:
        app = MainWindow()
        app.update_idletasks()
        if app.winfo_height() != 400:
            raise RuntimeError(
                f"main window height is {app.winfo_height()}, expected 400"
            )
        expected_tabs = {"영역", "캡쳐", "도구/설정", "행동"}
        if set(app.tab_frames) != expected_tabs:
            raise RuntimeError(f"unexpected main tabs: {set(app.tab_frames)}")
        app._select_tab("행동")
        app.update_idletasks()
        ocr_button_height = app.ocr_button.master.winfo_height()
        if ocr_button_height != OCR_BUTTON_HEIGHT_PX:
            raise RuntimeError(
                f"main OCR button height is {ocr_button_height}, expected {OCR_BUTTON_HEIGHT_PX}"
            )
        app._select_tab("도구/설정")
        app.update_idletasks()
        expected_overlay_state = (
            tk.NORMAL if app.settings.overlay_translation_mode else tk.DISABLED
        )
        if app.hytrans_button.cget("state") != expected_overlay_state:
            raise RuntimeError("HYTrans button state does not match overlay mode")
        if app.overlayer_button.cget("state") != expected_overlay_state:
            raise RuntimeError("MekiOverlayer button state does not match overlay mode")
        app._select_tab("영역")
        app.update_idletasks()

        bookmark = Bookmark(name="self-test", left=11, top=22, width=333, height=44)
        expected_region = Region(left=11, top=22, width=333, height=44)
        app._load_bookmark_region(bookmark)
        if app.active_region != expected_region:
            raise RuntimeError(f"bookmark did not populate active region: {app.active_region}")
        if "확정 영역 :\n설정되지 않음" in app.status_label.cget("text"):
            raise RuntimeError("bookmark status still reports no active region")

        tray_roundtrip = False
        tray_message_window_reused = False
        if os.name == "nt":
            tray_hwnd = None
            for _ in range(3):
                if not app.tray_icon.show():
                    break
                tray_roundtrip = True
                current_hwnd = app.tray_icon._hwnd
                if tray_hwnd is None:
                    tray_hwnd = current_hwnd
                elif current_hwnd != tray_hwnd:
                    raise RuntimeError("tray message window was recreated during restore")
                app.withdraw()
                app.update_idletasks()
                ctypes.windll.user32.SendMessageW(
                    current_hwnd,
                    app.tray_icon.WM_TRAY_CALLBACK,
                    1,
                    app.tray_icon.WM_LBUTTONDBLCLK,
                )
                ctypes.windll.user32.SendMessageW(
                    current_hwnd,
                    app.tray_icon.WM_TRAY_CALLBACK,
                    1,
                    app.tray_icon.WM_LBUTTONDBLCLK,
                )
                deadline = time.monotonic() + 2.0
                while app.state() != "normal" and time.monotonic() < deadline:
                    app.update()
                    time.sleep(0.01)
                if app.state() != "normal":
                    raise RuntimeError(f"tray restore left app in state: {app.state()}")
            tray_message_window_reused = bool(
                tray_roundtrip
                and app.tray_icon._window_class_atom
                and app.tray_icon._hwnd == tray_hwnd
            )
            if tray_roundtrip and not tray_message_window_reused:
                raise RuntimeError("tray message window did not survive restore")

        if load_detached_region() != expected_region:
            raise RuntimeError("active region was not published for detached process")
        if "--detached-button" not in _mekicopy_process_command("--detached-button"):
            raise RuntimeError("detached OCR button process command was not created")

        app._on_open_settings()
        app.update_idletasks()
        if not app.settings_window or not app.settings_window.winfo_exists():
            raise RuntimeError("settings window was not created")
        if not app.settings_window.korean_font_names:
            raise RuntimeError("font menu contains no Korean-capable fonts")
        unsupported_fonts = [
            name
            for name in app.settings_window.korean_font_names
            if not _font_has_character(name)
        ]
        if unsupported_fonts:
            raise RuntimeError(f"font menu contains unsupported fonts: {unsupported_fonts}")
        korean_font_count = len(app.settings_window.korean_font_names)

        app.settings_window._on_close()
        save_settings(app.settings)
        _log_runtime_message(
            "self_test_ui",
            (
                f"main_ocr_button_height: {ocr_button_height}\n"
                f"main_window_height: {app.winfo_height()}\n"
                f"tray_roundtrip: {tray_roundtrip}\n"
                f"tray_message_window_reused: {tray_message_window_reused}\n"
                f"korean_font_count: {korean_font_count}\n"
                f"settings_file: {SETTINGS_FILE}\n"
                f"icon_path: {_get_icon_path()}"
            ),
        )
    except Exception as exc:
        _log_runtime_error("self_test_ui", exc)
        raise SystemExit(1)
    finally:
        if app and app.winfo_exists():
            app.tray_icon.close()
            app.destroy()


def main() -> None:
    _enable_dpi_awareness()
    _set_app_user_model_id()
    args = parse_args()
    if args.self_test_detached_launcher:
        _launch_detached_button_process()
        os._exit(0)
    if args.self_test_runtime:
        _log_runtime_message("self_test_runtime", "starting")
        from importlib.metadata import version

        meikiocr_version = version("meikiocr")
        if meikiocr_version != "0.3.4":
            raise RuntimeError(
                f"unexpected meikiocr version: {meikiocr_version} (expected 0.3.4)"
            )
        engine = _get_ocr_engine()
        _log_runtime_message(
            "self_test_runtime",
            (
                f"meikiocr_version: {meikiocr_version}\n"
                f"active_provider: {getattr(engine, 'active_provider', 'unknown')}"
            ),
        )
        return
    _prepare_tk_library_paths()
    if args.detached_button:
        run_detached_button()
        return
    if args.self_test_detached_button:
        run_detached_button_self_test()
        return
    if args.self_test_detached_survival:
        run_detached_survival_self_test()
        return
    if args.self_test_tray_stress:
        run_tray_stress_self_test()
        return
    if args.self_test_ui:
        run_ui_self_test()
        return
    if args.bookmark:
        bookmarks = load_bookmarks()
        bookmark = bookmarks.get(args.bookmark)
        if not bookmark:
            messagebox.showerror("MekiCopy", "북마크를 찾을 수 없습니다.")
            return
        ocr_and_copy(bookmark.left, bookmark.top, bookmark.width, bookmark.height)
        return
    if args.pick_bookmark:
        run_picker_and_capture()
        return
    if args.adjust_bookmark:
        bookmarks = load_bookmarks()
        bookmark = bookmarks.get(args.adjust_bookmark)
        if not bookmark:
            messagebox.showerror("MekiCopy", "북마크를 찾을 수 없습니다.")
            return
        run_selection(initial_region=bookmark, capture_on_enter=False)
        return
    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
