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
from service_ports import (
    AUDIO_CAPTURE_DEFAULT_PORT,
    HYTRANS_DEFAULT_PORT,
    OVERLAYER_DEFAULT_PORT,
    SCRIPT_DEFAULT_PORT,
    normalize_port,
    validate_unique_ports,
)

prepare_tk_environment("MekiCopyRuntime")

import tkinter as tk
from dataclasses import dataclass
from tkinter import colorchooser, font as tkfont, messagebox, simpledialog, ttk
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
from mekicopy_companions import (
    _WindowsNamedMutex,
    _activate_detached_window,
    _close_detached_window,
    _detached_process_creation_flags,
    _find_companion_executable,
    _find_detached_window,
    _find_magpie_executable,
    _install_latest_magpie,
    _is_process_alive,
    _json_request,
    _launch_detached_button_process,
    _mekicopy_process_command,
    _probe_service,
    _startupinfo_for_background,
    _validated_translation_text,
)
from mekicopy_ocr import (
    _get_ocr_engine,
    _log_runtime_error,
    _log_runtime_message,
    capture_region,
    ocr_and_copy,
)
from mekicopy_region_ui import (
    build_initial_rect,
    pick_bookmark,
    run_picker_and_capture,
    run_region_view,
    run_selection,
)
from mekicopy_runtime import (
    _get_app_dir,
    _get_icon_path,
    _get_resource_dir,
    _prepare_native_runtime_paths,
    _prepare_tk_library_paths,
    _prepare_windowed_streams,
    _set_app_user_model_id,
    _set_window_icon,
)
from mekicopy_settings import (
    AppSettings,
    Bookmark,
    Rect,
    SETTINGS_FILE,
    _font_has_character,
    _geometry_size,
    load_bookmarks,
    load_detached_geometry,
    load_detached_region,
    load_settings,
    save_bookmarks,
    save_detached_geometry,
    save_detached_region,
    save_settings,
)
from mekicopy_settings_window import SettingsWindow
from mekicopy_tray import WindowsTrayIcon
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
            hytrans_url = f"http://127.0.0.1:{self.settings.hytrans_port}"
            overlayer_url = f"http://127.0.0.1:{self.settings.overlayer_port}"
            _probe_service("HYTrans", hytrans_url)
            _probe_service("MekiOverlayer", overlayer_url)
            self._send_overlayer_config()
            response = _json_request(
                f"{hytrans_url}/translate-and-show",
                {
                    "text": text,
                    "overlayUrl": f"{overlayer_url}/show",
                },
                timeout=130,
                method="POST",
            )
            _validated_translation_text(response)
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
        self.audio_capture_process: subprocess.Popen | None = None
        self.script_process: subprocess.Popen | None = None
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

        for tab_name in ("영역", "캡쳐", "음성인식", "도구/설정", "행동"):
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
        self._build_audio_tab(self.tab_frames["음성인식"])
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

    def _build_audio_tab(self, frame: tk.Frame) -> None:
        self._pack_tab_button(frame, "MekiAudioCapture 실행", self._on_start_audio_capture)
        self._pack_tab_button(frame, "HYTrans 실행", self._on_start_hytrans)
        self._pack_tab_button(frame, "MekiScript 실행", self._on_start_script)
        self._pack_tab_button(
            frame,
            "모든 도구 연결 상태 확인",
            self._on_test_audio_connection,
        )

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

    def _script_base_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.script_port}"

    def _audio_capture_base_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.audio_capture_port}"

    def _script_config_payload(self) -> dict:
        return {
            "topmost": self.settings.script_always_on_top,
            "bg_color": self.settings.script_bg_color,
            "opacity": self.settings.script_bg_opacity,
            "original_color": self.settings.script_original_text_color,
            "original_size": self.settings.script_original_text_size,
            "original_font": self.settings.script_original_text_font,
            "translated_color": self.settings.script_translated_text_color,
            "translated_size": self.settings.script_translated_text_size,
            "translated_font": self.settings.script_translated_text_font,
        }

    def _audio_capture_config_payload(self) -> dict:
        return {
            "precision": self.settings.audio_stt_precision,
            "preset": self.settings.audio_chunk_preset,
            "scriptUrl": self._script_base_url(),
            "hytransUrl": self._hytrans_base_url(),
        }

    def _send_script_config(self, log_errors: bool = True) -> bool:
        try:
            _json_request(
                f"{self._script_base_url()}/config",
                self._script_config_payload(),
                timeout=2,
                method="POST",
            )
            return True
        except Exception as exc:
            if log_errors:
                _log_runtime_error("send_script_config", exc)
            return False

    def _send_audio_capture_config(self, log_errors: bool = True) -> bool:
        try:
            _json_request(
                f"{self._audio_capture_base_url()}/config",
                self._audio_capture_config_payload(),
                timeout=2,
                method="POST",
            )
            return True
        except Exception as exc:
            if log_errors:
                _log_runtime_error("send_audio_capture_config", exc)
            return False

    def _on_start_script(self) -> None:
        try:
            _json_request(f"{self._script_base_url()}/health", timeout=1)
            self._send_script_config()
            messagebox.showinfo(
                "MekiCopy",
                "MekiScript가 이미 실행 중입니다. 현재 설정을 적용했습니다.",
                parent=self,
            )
            return
        except Exception:
            pass
        command = _find_companion_executable("MekiScript", "meki_script.py")
        if not command:
            messagebox.showerror("MekiCopy", "MekiScript 실행 파일을 찾을 수 없습니다.", parent=self)
            return
        cfg = self._script_config_payload()
        command += [
            "--port", str(self.settings.script_port),
            "--topmost", "1" if cfg["topmost"] else "0",
            "--bg-color", str(cfg["bg_color"]),
            "--opacity", str(cfg["opacity"]),
            "--original-color", str(cfg["original_color"]),
            "--original-size", str(cfg["original_size"]),
            "--original-font", str(cfg["original_font"]),
            "--translated-color", str(cfg["translated_color"]),
            "--translated-size", str(cfg["translated_size"]),
            "--translated-font", str(cfg["translated_font"]),
        ]
        try:
            self.script_process = subprocess.Popen(command, cwd=_get_app_dir())
            _log_runtime_message("start_script", " ".join(command))
            messagebox.showinfo("MekiCopy", "MekiScript를 실행했습니다.", parent=self)
        except Exception as exc:
            _log_runtime_error("start_script", exc)
            messagebox.showerror("MekiCopy", f"MekiScript 실행 실패:\n{exc}", parent=self)

    def _on_start_audio_capture(self) -> None:
        try:
            _json_request(f"{self._audio_capture_base_url()}/health", timeout=1)
            self._send_audio_capture_config()
            messagebox.showinfo(
                "MekiCopy",
                "MekiAudioCapture가 이미 실행 중입니다. 현재 설정을 적용했습니다.",
                parent=self,
            )
            return
        except Exception:
            pass
        command = _find_companion_executable("MekiAudioCapture", "meki_audio_capture.py")
        if not command:
            messagebox.showerror("MekiCopy", "MekiAudioCapture 실행 파일을 찾을 수 없습니다.", parent=self)
            return
        cfg = self._audio_capture_config_payload()
        command += [
            "--port", str(self.settings.audio_capture_port),
            "--precision", str(cfg["precision"]),
            "--preset", str(cfg["preset"]),
            "--script-url", str(cfg["scriptUrl"]),
            "--hytrans-url", str(cfg["hytransUrl"]),
        ]
        try:
            self.audio_capture_process = subprocess.Popen(command, cwd=_get_app_dir())
            _log_runtime_message("start_audio_capture", " ".join(command))
            messagebox.showinfo("MekiCopy", "MekiAudioCapture를 실행했습니다.", parent=self)
        except Exception as exc:
            _log_runtime_error("start_audio_capture", exc)
            messagebox.showerror("MekiCopy", f"MekiAudioCapture 실행 실패:\n{exc}", parent=self)

    def _on_test_audio_connection(self) -> None:
        services = (
            ("MekiAudioCapture", self._audio_capture_base_url()),
            ("HYTrans", self._hytrans_base_url()),
            ("MekiScript", self._script_base_url()),
        )
        successes: list[str] = []
        failures: list[str] = []
        for name, base_url in services:
            try:
                state = _probe_service(name, base_url)
                successes.append(f"{name}: {state}")
            except Exception as exc:
                failures.append(f"{name}: 연결 실패 ({exc})")
        if failures:
            messagebox.showerror(
                "MekiCopy",
                "\n".join([*successes, *failures]),
                parent=self,
            )
        else:
            self._send_script_config(log_errors=False)
            self._send_audio_capture_config(log_errors=False)
            messagebox.showinfo(
                "MekiCopy",
                "세 도구의 연결 상태가 정상입니다.\n" + "\n".join(successes),
                parent=self,
            )

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
            hytrans_state = _probe_service("HYTrans", self._hytrans_base_url())
        except Exception as exc:
            failures.append(f"HYTrans 연결 실패: {exc}")
        try:
            _probe_service("MekiOverlayer", self._overlayer_base_url())
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
            messagebox.showinfo(
                "MekiCopy",
                f"HYTrans -> MekiOverlayer 표시 흐름이 정상입니다.\n번역 모델: {hytrans_state}",
                parent=owner,
            )
        except Exception as exc:
            _log_runtime_error("test_overlay_connection", exc)
            messagebox.showerror("MekiCopy", f"연결 테스트 실패:\n{exc}", parent=owner)

    def _request_translate_and_show(self, text: str) -> dict:
        _probe_service("HYTrans", self._hytrans_base_url())
        _probe_service("MekiOverlayer", self._overlayer_base_url())
        response = _json_request(
            f"{self._hytrans_base_url()}/translate-and-show",
            {"text": text, "overlayUrl": self._overlayer_show_url()},
            timeout=130,
            method="POST",
        )
        _validated_translation_text(response)
        return response

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
        self._send_script_config(log_errors=False)
        self._send_audio_capture_config(log_errors=False)
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
        for process in (
            self.hytrans_process,
            self.overlayer_process,
            self.audio_capture_process,
            self.script_process,
        ):
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
        expected_tabs = {"영역", "캡쳐", "음성인식", "도구/설정", "행동"}
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
