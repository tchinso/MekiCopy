from __future__ import annotations

import configparser
import ctypes
import io
import json
import math
import os
import re
import shutil
import threading
import tkinter as tk
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont

from mekicopy_capture import MIN_SIZE_PX, Region
from mekicopy_runtime import _get_app_dir
from hytrans.model_files import (
    DEFAULT_MODEL_ID,
    normalize_model_id,
)
from runtime_paths import exclusive_file_lock, fallback_app_data_dirs, state_data_dir
from service_ports import (
    AUDIO_CAPTURE_DEFAULT_PORT,
    HYTRANS_DEFAULT_PORT,
    OVERLAYER_DEFAULT_PORT,
    SCRIPT_DEFAULT_PORT,
    normalize_port,
    validate_unique_ports,
)

DETACHED_DEFAULT_GEOMETRY = "260x160+120+120"
KOREAN_FONT_TEST_CHARACTER = "쿈"
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_STATE_FILENAMES = (
    "bookmarks.txt",
    "settings.cfg",
    "detached_button_region.json",
    "detached_button_geometry.json",
)
_STATE_MIGRATION_MARKER = ".state_migration_v2"
_STATE_MIGRATION_THREAD_LOCK = threading.RLock()


def _same_path(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return os.path.normcase(str(first)) == os.path.normcase(str(second))


def _write_text_atomic(path: str | Path, text: str) -> bool:
    """Durably replace a small text file without propagating filesystem errors."""
    destination = Path(path)
    temporary = destination.with_name(
        f"{destination.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return True
    except (OSError, UnicodeError):
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_copy(source: Path, destination: Path) -> bool:
    """Copy an existing legacy state file without exposing a partial target."""
    temporary = destination.with_name(
        f"{destination.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        return True
    except OSError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _migrate_legacy_state_files(
    legacy_dirs: list[Path],
    destination_dir: Path,
) -> None:
    """Copy missing state once from older secondary storage locations."""
    sources = [
        path for path in legacy_dirs if not _same_path(path, destination_dir)
    ]
    if not sources:
        return
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        marker = destination_dir / _STATE_MIGRATION_MARKER
        with _STATE_MIGRATION_THREAD_LOCK, exclusive_file_lock(
            destination_dir / ".state_migration.lock"
        ):
            if marker.is_file():
                return
            for filename in _STATE_FILENAMES:
                destination = destination_dir / filename
                if destination.exists():
                    continue
                for legacy_dir in sources:
                    source = legacy_dir / filename
                    if source.is_file() and _atomic_copy(source, destination):
                        break
            _write_text_atomic(marker, "migrated\n")
    except OSError:
        # Migration is best effort.  Save APIs below remain safe in restrictive
        # environments, and a later launch may retry if no marker was written.
        return


def _initialize_state_directory() -> Path:
    destination = state_data_dir("MekiCopy")
    program_dir = Path(_get_app_dir())
    legacy_dirs = [program_dir, *fallback_app_data_dirs("MekiCopy")]
    _migrate_legacy_state_files(legacy_dirs, destination)
    return destination


STATE_DIR = _initialize_state_directory()
BOOKMARKS_FILE = str(STATE_DIR / "bookmarks.txt")
SETTINGS_FILE = str(STATE_DIR / "settings.cfg")
DETACHED_REGION_FILE = str(STATE_DIR / "detached_button_region.json")
DETACHED_GEOMETRY_FILE = str(STATE_DIR / "detached_button_geometry.json")

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
    hytrans_model_id: str = DEFAULT_MODEL_ID
    hytrans_port: int = HYTRANS_DEFAULT_PORT
    overlayer_port: int = OVERLAYER_DEFAULT_PORT
    audio_capture_port: int = AUDIO_CAPTURE_DEFAULT_PORT
    script_port: int = SCRIPT_DEFAULT_PORT
    overlayer_always_on_top: bool = True
    overlayer_hide_titlebar: bool = False
    overlayer_fixed_size: bool = False
    overlayer_exclude_from_capture: bool = True
    overlayer_bg_color: str = "#111111"
    overlayer_bg_opacity: float = 0.78
    overlayer_text_color: str = "#ffffff"
    overlayer_text_size: int = 28
    overlayer_text_font: str = "Malgun Gothic"
    audio_stt_precision: str = "fp32"
    audio_chunk_preset: str = "BALANCED"
    script_always_on_top: bool = True
    script_bg_color: str = "#111111"
    script_bg_opacity: float = 0.90
    script_original_text_color: str = "#f4f4f5"
    script_original_text_size: int = 20
    script_original_text_font: str = "Yu Gothic UI"
    script_translated_text_color: str = "#7dd3fc"
    script_translated_text_size: int = 20
    script_translated_text_font: str = "Malgun Gothic"
    suppress_magpie_launch_notice: bool = False
    debug_logging: bool = False


def _normalize_port(port: int, fallback: int) -> int:
    return normalize_port(port, fallback)


def _alternate_port(blocked_port: int) -> int:
    for candidate in (
        OVERLAYER_DEFAULT_PORT,
        HYTRANS_DEFAULT_PORT,
        AUDIO_CAPTURE_DEFAULT_PORT,
        SCRIPT_DEFAULT_PORT,
    ):
        if candidate != blocked_port:
            return candidate
    return 65535 if blocked_port != 65535 else 65534


def _normalize_font_name(font_name: str) -> str:
    normalized = str(font_name).strip().lstrip("@").strip()
    return normalized or "Malgun Gothic"


def _normalize_hex_color(value: object, fallback: str) -> str:
    color = str(value or "").strip()
    if HEX_COLOR_RE.match(color):
        if len(color) == 4:
            color = "#" + "".join(channel * 2 for channel in color[1:])
        return color.lower()
    return fallback


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
    gdi32.GetFontUnicodeRanges.argtypes = [wintypes.HDC, ctypes.c_void_p]
    gdi32.GetFontUnicodeRanges.restype = wintypes.DWORD
    gdi32.GetTextFaceW.argtypes = [wintypes.HDC, ctypes.c_int, wintypes.LPWSTR]
    gdi32.GetTextFaceW.restype = ctypes.c_int
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
            128 if character == "ー" else 129,  # SHIFTJIS_CHARSET / HANGUL_CHARSET
            0,
            0,
            0,
            0,
            _normalize_font_name(font_name),
        )
        if not font_handle:
            return False
        old_font = gdi32.SelectObject(hdc, font_handle)
        face_length = gdi32.GetTextFaceW(hdc, 0, None)
        if face_length <= 0:
            return False
        face_buffer = ctypes.create_unicode_buffer(face_length + 1)
        if not gdi32.GetTextFaceW(hdc, len(face_buffer), face_buffer):
            return False
        if _normalize_font_name(face_buffer.value).casefold() != _normalize_font_name(font_name).casefold():
            return False
        class _GlyphSetHeader(ctypes.Structure):
            _fields_ = [
                ("cbThis", wintypes.DWORD),
                ("flAccel", wintypes.DWORD),
                ("cGlyphsSupported", wintypes.DWORD),
                ("cRanges", wintypes.DWORD),
            ]

        class _Wcrange(ctypes.Structure):
            _fields_ = [("wcLow", wintypes.WORD), ("cGlyphs", wintypes.WORD)]

        buffer_size = gdi32.GetFontUnicodeRanges(hdc, None)
        if not buffer_size:
            return False
        buffer = ctypes.create_string_buffer(buffer_size)
        if not gdi32.GetFontUnicodeRanges(hdc, ctypes.byref(buffer)):
            return False
        header = _GlyphSetHeader.from_buffer(buffer)
        ranges_type = _Wcrange * header.cRanges
        ranges = ranges_type.from_buffer(buffer, ctypes.sizeof(_GlyphSetHeader))
        return all(
            any(item.wcLow <= ord(char) < item.wcLow + item.cGlyphs for item in ranges)
            for char in character
        )
    finally:
        if old_font:
            gdi32.SelectObject(hdc, old_font)
        if font_handle:
            gdi32.DeleteObject(font_handle)
        gdi32.DeleteDC(hdc)


def _korean_font_families(root: tk.Misc) -> list[str]:
    font_names = sorted({_normalize_font_name(name) for name in tkfont.families(root) if name})
    return [name for name in font_names if _font_has_character(name)]


def _japanese_font_families(root: tk.Misc) -> list[str]:
    font_names = sorted({_normalize_font_name(name) for name in tkfont.families(root) if name})
    return [name for name in font_names if _font_has_character(name, "ー")]


_CONFIG_UNSET = configparser._UNSET


class _SafeConfigParser(configparser.ConfigParser):
    """ConfigParser variant that preserves usable settings around bad values."""

    @staticmethod
    def _fallback_or_raise(fallback: object, exc: Exception) -> object:
        if fallback is not _CONFIG_UNSET:
            return fallback
        raise exc

    def get(self, section, option, *, raw=False, vars=None, fallback=_CONFIG_UNSET):
        try:
            return super().get(section, option, raw=raw, vars=vars, fallback=fallback)
        except (configparser.Error, TypeError, ValueError) as exc:
            return self._fallback_or_raise(fallback, exc)

    def getboolean(self, section, option, *, raw=False, vars=None, fallback=_CONFIG_UNSET):
        try:
            return super().getboolean(section, option, raw=raw, vars=vars, fallback=fallback)
        except (configparser.Error, TypeError, ValueError) as exc:
            return self._fallback_or_raise(fallback, exc)

    def getint(self, section, option, *, raw=False, vars=None, fallback=_CONFIG_UNSET):
        try:
            return super().getint(section, option, raw=raw, vars=vars, fallback=fallback)
        except (configparser.Error, TypeError, ValueError) as exc:
            return self._fallback_or_raise(fallback, exc)

    def getfloat(self, section, option, *, raw=False, vars=None, fallback=_CONFIG_UNSET):
        try:
            return super().getfloat(section, option, raw=raw, vars=vars, fallback=fallback)
        except (configparser.Error, TypeError, ValueError) as exc:
            return self._fallback_or_raise(fallback, exc)


def _bounded_float(value: object, fallback: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return max(minimum, min(maximum, number))


def load_settings() -> AppSettings:
    settings = AppSettings()
    parser = _SafeConfigParser()
    try:
        parser.read(SETTINGS_FILE, encoding="utf-8")
    except (OSError, UnicodeError, configparser.Error):
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
    settings.hytrans_model_id = normalize_model_id(
        parser.get(
            section,
            "hytrans_model_id",
            fallback=settings.hytrans_model_id,
        )
    )
    settings.hytrans_port = parser.getint(
        section, "hytrans_port", fallback=settings.hytrans_port
    )
    settings.overlayer_port = parser.getint(
        section, "overlayer_port", fallback=settings.overlayer_port
    )
    settings.audio_capture_port = parser.getint(
        section, "audio_capture_port", fallback=settings.audio_capture_port
    )
    settings.script_port = parser.getint(
        section, "script_port", fallback=settings.script_port
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
    settings.audio_stt_precision = parser.get(
        section, "audio_stt_precision", fallback=settings.audio_stt_precision
    ).lower()
    settings.audio_chunk_preset = parser.get(
        section, "audio_chunk_preset", fallback=settings.audio_chunk_preset
    ).upper()
    settings.script_always_on_top = parser.getboolean(
        section, "script_always_on_top", fallback=settings.script_always_on_top
    )
    settings.script_bg_color = parser.get(
        section, "script_bg_color", fallback=settings.script_bg_color
    )
    settings.script_bg_opacity = parser.getfloat(
        section, "script_bg_opacity", fallback=settings.script_bg_opacity
    )
    settings.script_original_text_color = parser.get(
        section, "script_original_text_color", fallback=settings.script_original_text_color
    )
    settings.script_original_text_size = parser.getint(
        section, "script_original_text_size", fallback=settings.script_original_text_size
    )
    settings.script_original_text_font = parser.get(
        section, "script_original_text_font", fallback=settings.script_original_text_font
    )
    settings.script_translated_text_color = parser.get(
        section, "script_translated_text_color", fallback=settings.script_translated_text_color
    )
    settings.script_translated_text_size = parser.getint(
        section, "script_translated_text_size", fallback=settings.script_translated_text_size
    )
    settings.script_translated_text_font = parser.get(
        section, "script_translated_text_font", fallback=settings.script_translated_text_font
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
    settings.audio_capture_port = _normalize_port(
        settings.audio_capture_port, AUDIO_CAPTURE_DEFAULT_PORT
    )
    settings.script_port = _normalize_port(settings.script_port, SCRIPT_DEFAULT_PORT)
    legacy_default_ports = (
        not parser.has_option(section, "service_ports_version")
        and settings.hytrans_port == 6550
        and settings.overlayer_port == 6551
        and not parser.has_option(section, "audio_capture_port")
        and not parser.has_option(section, "script_port")
    )
    if legacy_default_ports:
        settings.hytrans_port = HYTRANS_DEFAULT_PORT
        settings.overlayer_port = OVERLAYER_DEFAULT_PORT
        settings.audio_capture_port = AUDIO_CAPTURE_DEFAULT_PORT
        settings.script_port = SCRIPT_DEFAULT_PORT
    try:
        validate_unique_ports(
            {
                "HYTrans": settings.hytrans_port,
                "MekiOverlayer": settings.overlayer_port,
                "MekiAudioCapture": settings.audio_capture_port,
                "MekiScript": settings.script_port,
            }
        )
    except ValueError:
        settings.hytrans_port = HYTRANS_DEFAULT_PORT
        settings.overlayer_port = OVERLAYER_DEFAULT_PORT
        settings.audio_capture_port = AUDIO_CAPTURE_DEFAULT_PORT
        settings.script_port = SCRIPT_DEFAULT_PORT
    settings.overlayer_bg_opacity = _bounded_float(
        settings.overlayer_bg_opacity,
        AppSettings().overlayer_bg_opacity,
        0.1,
        1.0,
    )
    settings.overlayer_bg_color = _normalize_hex_color(
        settings.overlayer_bg_color,
        AppSettings().overlayer_bg_color,
    )
    settings.overlayer_text_color = _normalize_hex_color(
        settings.overlayer_text_color,
        AppSettings().overlayer_text_color,
    )
    settings.overlayer_text_size = max(8, min(96, settings.overlayer_text_size))
    settings.overlayer_text_font = _normalize_font_name(settings.overlayer_text_font)
    if settings.audio_stt_precision not in {"fp32", "int8"}:
        settings.audio_stt_precision = "fp32"
    if settings.audio_chunk_preset not in {"FAST", "BALANCED", "LONG"}:
        settings.audio_chunk_preset = "BALANCED"
    settings.script_bg_opacity = _bounded_float(
        settings.script_bg_opacity,
        AppSettings().script_bg_opacity,
        0.1,
        1.0,
    )
    settings.script_bg_color = _normalize_hex_color(
        settings.script_bg_color,
        AppSettings().script_bg_color,
    )
    settings.script_original_text_color = _normalize_hex_color(
        settings.script_original_text_color,
        AppSettings().script_original_text_color,
    )
    settings.script_translated_text_color = _normalize_hex_color(
        settings.script_translated_text_color,
        AppSettings().script_translated_text_color,
    )
    settings.script_original_text_size = max(8, min(96, settings.script_original_text_size))
    settings.script_translated_text_size = max(8, min(96, settings.script_translated_text_size))
    settings.script_original_text_font = _normalize_font_name(settings.script_original_text_font)
    settings.script_translated_text_font = _normalize_font_name(settings.script_translated_text_font)
    return settings


def save_settings(settings: AppSettings) -> bool:
    parser = configparser.ConfigParser()
    parser["settings"] = {
        "service_ports_version": "2",
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
        "hytrans_model_id": normalize_model_id(settings.hytrans_model_id),
        "hytrans_port": str(settings.hytrans_port),
        "overlayer_port": str(settings.overlayer_port),
        "audio_capture_port": str(settings.audio_capture_port),
        "script_port": str(settings.script_port),
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
        "audio_stt_precision": settings.audio_stt_precision,
        "audio_chunk_preset": settings.audio_chunk_preset,
        "script_always_on_top": str(settings.script_always_on_top).lower(),
        "script_bg_color": settings.script_bg_color,
        "script_bg_opacity": str(settings.script_bg_opacity),
        "script_original_text_color": settings.script_original_text_color,
        "script_original_text_size": str(settings.script_original_text_size),
        "script_original_text_font": _normalize_font_name(settings.script_original_text_font),
        "script_translated_text_color": settings.script_translated_text_color,
        "script_translated_text_size": str(settings.script_translated_text_size),
        "script_translated_text_font": _normalize_font_name(settings.script_translated_text_font),
        "suppress_magpie_launch_notice": str(
            settings.suppress_magpie_launch_notice
        ).lower(),
        "debug_logging": str(settings.debug_logging).lower(),
    }
    output = io.StringIO()
    try:
        parser.write(output)
    except (TypeError, ValueError):
        return False
    return _write_text_atomic(SETTINGS_FILE, output.getvalue())


def _write_json_atomic(path: str, payload: dict) -> bool:
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return False
    return _write_text_atomic(path, text)


def save_detached_region(region: Region) -> bool:
    return _write_json_atomic(
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


def save_detached_geometry(geometry: str) -> bool:
    return _write_json_atomic(
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
    try:
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
    except (OSError, UnicodeError):
        return {}
    return bookmarks


def save_bookmarks(bookmarks: dict[str, Bookmark]) -> bool:
    lines: list[str] = []
    try:
        for name in sorted(bookmarks):
            bookmark = bookmarks[name]
            lines.append(
                f"{bookmark.name}\t{bookmark.left}\t{bookmark.top}\t{bookmark.width}\t{bookmark.height}\n"
            )
    except (AttributeError, TypeError, ValueError):
        return False
    return _write_text_atomic(BOOKMARKS_FILE, "".join(lines))
