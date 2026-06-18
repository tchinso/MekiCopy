import argparse
import configparser
import ctypes
import os
import shutil
import sys
import tempfile

if getattr(sys, "frozen", False):
    _frozen_resource_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    os.environ.setdefault(
        "TCL_LIBRARY",
        os.path.join(_frozen_resource_dir, "tcl", "tcl8.6"),
    )
    os.environ.setdefault(
        "TK_LIBRARY",
        os.path.join(_frozen_resource_dir, "tcl", "tk8.6"),
    )

import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, simpledialog
from ctypes import wintypes
from typing import Callable
import traceback

from PIL import Image
import mss

EDGE_GRAB_PX = 8
MIN_SIZE_PX = 10
OCR_BUTTON_HEIGHT_PX = 400
SELECTION_INSTRUCTION_FONT_SIZE = 36
DETACHED_DEFAULT_GEOMETRY = "260x160+120+120"
ICON_FILENAME = "MekiCopy.ico"
_OCR_ENGINE = None
_DLL_DIR_HANDLES = []
_RUNTIME_PATH_READY = False
_WINDOW_STREAM = None
_ORT_PRELOAD_READY = False


def _get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_resource_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", _get_app_dir())
    return os.path.dirname(os.path.abspath(__file__))


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


def _prepare_tk_library_paths() -> None:
    if os.name != "nt":
        return

    resource_dir = _get_resource_dir()
    tcl_candidates = [
        os.path.join(resource_dir, "_tcl_data"),
        os.path.join(resource_dir, "tcl", "tcl8.6"),
        os.path.join(sys.base_prefix, "tcl", "tcl8.6"),
    ]
    tk_candidates = [
        os.path.join(resource_dir, "_tk_data"),
        os.path.join(resource_dir, "tcl", "tk8.6"),
        os.path.join(sys.base_prefix, "tcl", "tk8.6"),
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

    safe_root = os.path.join(os.path.expanduser("~"), "MekiCopyRuntime")
    safe_tcl = os.path.join(safe_root, "tcl8.6")
    safe_tk = os.path.join(safe_root, "tk8.6")
    try:
        if not os.path.exists(os.path.join(safe_tcl, "init.tcl")):
            shutil.copytree(source_tcl, safe_tcl, dirs_exist_ok=True)
        if not os.path.exists(os.path.join(safe_tk, "tk.tcl")):
            shutil.copytree(source_tk, safe_tk, dirs_exist_ok=True)
        os.environ["TCL_LIBRARY"] = safe_tcl
        os.environ["TK_LIBRARY"] = safe_tk
    except OSError as exc:
        _log_runtime_error("prepare_tk_library_paths", exc)
        os.environ["TCL_LIBRARY"] = source_tcl
        os.environ["TK_LIBRARY"] = source_tk


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
class Region:
    left: int
    top: int
    width: int
    height: int


@dataclass
class AppSettings:
    minimize_to_tray: bool = False
    main_always_on_top: bool = False
    detached_always_on_top: bool = False
    detached_hide_titlebar: bool = False
    detached_fixed_size: bool = False
    simple_copy_complete: bool = False
    detached_geometry: str = DETACHED_DEFAULT_GEOMETRY
    detached_fixed_width: int = 260
    detached_fixed_height: int = 160


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
    }
    with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
        parser.write(handle)


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


def _log_runtime_error(stage: str, exc: Exception) -> None:
    log_path = os.path.join(_get_app_dir(), "mekicopy_error.log")
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("\n=== ERROR ===\n")
            handle.write(f"stage: {stage}\n")
            handle.write(f"type: {type(exc).__name__}\n")
            handle.write(f"message: {exc}\n")
            handle.write(traceback.format_exc())
    except OSError:
        pass


def _log_runtime_message(stage: str, message: str) -> None:
    log_path = os.path.join(_get_app_dir(), "mekicopy_error.log")
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("\n=== INFO ===\n")
            handle.write(f"stage: {stage}\n")
            handle.write(message.rstrip() + "\n")
    except OSError:
        pass


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
    with mss.mss() as sct:
        region = {"left": left, "top": top, "width": width, "height": height}
        sct_image = sct.grab(region)
        return Image.frombytes("RGB", sct_image.size, sct_image.rgb)


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


def ocr_and_copy(
    left: int,
    top: int,
    width: int,
    height: int,
    notify: bool = True,
    parent: tk.Misc | None = None,
    on_copy_complete: Callable[[], None] | None = None,
) -> None:
    if width < MIN_SIZE_PX or height < MIN_SIZE_PX:
        messagebox.showerror("MekiCopy", "캡처 영역이 너무 작습니다.", parent=parent)
        return
    image = capture_region(left, top, width, height)
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
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040
    IDI_APPLICATION = 32512
    GWLP_WNDPROC = -4

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
        self._old_wndproc: int | None = None
        self._wndproc = None
        self._message_window: tk.Toplevel | None = None
        self._active = False
        self._restore_pending = False

    def show(self) -> bool:
        if os.name != "nt" or self._active:
            return False
        try:
            self.root.update_idletasks()
            self._message_window = tk.Toplevel(self.root)
            self._message_window.withdraw()
            self._message_window.title(self.tooltip)
            self._message_window.update_idletasks()
            self._hwnd = int(self._message_window.winfo_id())
            if not self._hwnd:
                self._destroy_message_window()
                return False
            self._subclass_window()
            self._hicon = self._load_icon()
            data = self._build_notify_data(self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP)
            shell32 = ctypes.windll.shell32
            if not shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(data)):
                self._restore_window_proc()
                self._destroy_message_window()
                return False
            self._active = True
            return True
        except Exception:
            self._restore_window_proc()
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
        self._restore_pending = False
        self._restore_window_proc()
        self._destroy_message_window()
        self._hwnd = None

    def _destroy_message_window(self) -> None:
        if not self._message_window:
            return
        try:
            if self._message_window.winfo_exists():
                self._message_window.destroy()
        except tk.TclError:
            pass
        self._message_window = None

    def _queue_restore(self) -> None:
        if self._restore_pending:
            return
        self._restore_pending = True
        self.root.after(0, self._handle_restore_requested)

    def _handle_restore_requested(self) -> None:
        if not self._active:
            self._restore_pending = False
            return
        try:
            self.on_restore()
        except Exception as exc:
            _log_runtime_error("tray_restore", exc)
            self._restore_pending = False

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

    def _subclass_window(self) -> None:
        user32 = ctypes.windll.user32
        wndproc_type = ctypes.WINFUNCTYPE(
            _LRESULT,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        def _window_proc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY_CALLBACK and lparam in (
                self.WM_LBUTTONUP,
                self.WM_LBUTTONDBLCLK,
                self.WM_RBUTTONUP,
            ):
                self._queue_restore()
                return 0
            if self._old_wndproc:
                return user32.CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = wndproc_type(_window_proc)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            set_window_long = user32.SetWindowLongPtrW
        else:
            set_window_long = user32.SetWindowLongW
        set_window_long.restype = ctypes.c_void_p
        set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        self._old_wndproc = set_window_long(
            self._hwnd,
            self.GWLP_WNDPROC,
            ctypes.cast(self._wndproc, ctypes.c_void_p),
        )
        user32.CallWindowProcW.restype = _LRESULT
        user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = _LRESULT
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]

    def _restore_window_proc(self) -> None:
        if os.name != "nt" or not self._hwnd or not self._old_wndproc:
            return
        try:
            user32 = ctypes.windll.user32
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                set_window_long = user32.SetWindowLongPtrW
            else:
                set_window_long = user32.SetWindowLongW
            set_window_long.restype = ctypes.c_void_p
            set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            set_window_long(self._hwnd, self.GWLP_WNDPROC, self._old_wndproc)
        except Exception:
            pass
        self._old_wndproc = None
        self._wndproc = None


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


class SettingsWindow(tk.Toplevel):
    def __init__(self, owner) -> None:
        super().__init__(owner)
        self.owner = owner
        self.title("MekiCopy 설정")
        self.resizable(False, False)
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

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.transient(owner)
        self.attributes("-topmost", settings.main_always_on_top)

    def _build_ui(self) -> None:
        body = tk.Frame(self, padx=14, pady=14)
        body.pack(fill=tk.BOTH, expand=True)

        options = [
            (
                "MekiCopy가 최소화되면 시스템 트레이로 이동",
                self.minimize_to_tray_var,
            ),
            ("MekiCopy를 항상 위로", self.main_topmost_var),
            (
                "분리된 '인식 후 복사' 버튼을 항상 위로",
                self.detached_topmost_var,
            ),
            (
                "분리된 '인식 후 복사' 버튼의 제목표시줄 숨김",
                self.detached_hide_titlebar_var,
            ),
            (
                "분리된 '인식 후 복사' 버튼의 크기를 고정",
                self.detached_fixed_size_var,
            ),
            ("복사 완료를 간단하게 표시하기", self.simple_copy_complete_var),
        ]
        for text, variable in options:
            checkbox = tk.Checkbutton(body, text=text, variable=variable, anchor="w")
            checkbox.pack(fill=tk.X, pady=4)

        button_row = tk.Frame(body)
        button_row.pack(fill=tk.X, pady=(14, 0))
        save_button = tk.Button(button_row, text="저장", command=self._on_save)
        save_button.pack(side=tk.RIGHT, padx=(8, 0))
        close_button = tk.Button(button_row, text="닫기", command=self._on_close)
        close_button.pack(side=tk.RIGHT)

    def _on_save(self) -> None:
        current = self.owner.settings
        if self.owner.detached_window:
            self.owner.detached_window.capture_geometry()

        settings = AppSettings(
            minimize_to_tray=self.minimize_to_tray_var.get(),
            main_always_on_top=self.main_topmost_var.get(),
            detached_always_on_top=self.detached_topmost_var.get(),
            detached_hide_titlebar=self.detached_hide_titlebar_var.get(),
            detached_fixed_size=self.detached_fixed_size_var.get(),
            simple_copy_complete=self.simple_copy_complete_var.get(),
            detached_geometry=current.detached_geometry,
            detached_fixed_width=current.detached_fixed_width,
            detached_fixed_height=current.detached_fixed_height,
        )
        if settings.detached_fixed_size and self.owner.detached_window:
            width, height = self.owner.detached_window.current_size()
            settings.detached_fixed_width = width
            settings.detached_fixed_height = height
        self.owner.apply_settings(settings, persist=True)
        self._on_close()

    def _on_close(self) -> None:
        self.owner.settings_window = None
        self.destroy()


class DetachedOcrButtonWindow:
    MIN_WIDTH = 120
    MIN_HEIGHT = 80
    RESIZE_MARGIN = 10

    def __init__(self, owner) -> None:
        self.owner = owner
        self.root = tk.Toplevel(owner)
        self.root.title("인식 후 복사")
        _set_window_icon(self.root)
        self._drag_action: str | None = None
        self._drag_start: tuple[int, int] | None = None
        self._drag_geometry: tuple[int, int, int, int] | None = None
        self._suppress_next_click = False

        self.button = tk.Button(
            self.root,
            text="인식 후 복사",
            command=self._on_button_command,
            font=("Segoe UI", 14, "bold"),
        )
        self.button.pack(fill=tk.BOTH, expand=True)

        self.root.geometry(owner.settings.detached_geometry or DETACHED_DEFAULT_GEOMETRY)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self._on_configure)
        self.button.bind("<ButtonPress-1>", self._on_mouse_down, add="+")
        self.button.bind("<B1-Motion>", self._on_mouse_drag, add="+")
        self.button.bind("<ButtonRelease-1>", self._on_mouse_release, add="+")
        self.apply_settings()

    def current_size(self) -> tuple[int, int]:
        self.root.update_idletasks()
        return (
            max(self.MIN_WIDTH, self.root.winfo_width()),
            max(self.MIN_HEIGHT, self.root.winfo_height()),
        )

    def capture_geometry(self) -> None:
        if not self.root.winfo_exists():
            return
        self.root.update_idletasks()
        self.owner.settings.detached_geometry = self.root.geometry()
        width, height = self.current_size()
        if self.owner.settings.detached_fixed_size:
            self.owner.settings.detached_fixed_width = width
            self.owner.settings.detached_fixed_height = height

    def apply_settings(self) -> None:
        settings = self.owner.settings
        if self.root.winfo_exists():
            self.capture_geometry()

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
        self.capture_geometry()
        save_settings(self.owner.settings)
        self.owner.detached_window = None
        self.root.destroy()

    def _on_button_command(self) -> None:
        if self._suppress_next_click:
            self._suppress_next_click = False
            return
        self.owner._on_ocr_copy(source_button=self.button)

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget == self.root and self.root.winfo_exists():
            self.owner.settings.detached_geometry = self.root.geometry()

    def _on_mouse_down(self, event: tk.Event) -> None:
        if not self.owner.settings.detached_hide_titlebar:
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

        if self.owner.settings.detached_fixed_size or self._drag_action == "move":
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
        if self.owner.settings.detached_fixed_size:
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
        if os.name != "nt" or self.owner.settings.detached_hide_titlebar:
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
        _prepare_tk_library_paths()
        super().__init__()
        self.settings = load_settings()
        self.detached_window: DetachedOcrButtonWindow | None = None
        self.settings_window: SettingsWindow | None = None
        self.tray_icon = WindowsTrayIcon(self, "MekiCopy", self._restore_from_tray)
        self.title("MekiCopy")
        _set_window_icon(self)
        self.geometry("460x820")
        self.resizable(False, False)
        self.draft_region: Region | None = None
        self.active_region: Region | None = None
        self._closing = False
        self._restoring_from_tray = False
        self._build_ui()
        self.apply_settings(self.settings, persist=False)
        self.bind("<Unmap>", self._on_unmap)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        header = tk.Label(self, text="MekiCopy 빠른 실행", font=("Segoe UI", 12, "bold"))
        header.pack(pady=10)

        self.status_label = tk.Label(self, text="", justify="left")
        self.status_label.pack(padx=12, pady=6, fill=tk.X)

        button_frame = tk.Frame(self)
        button_frame.pack(padx=12, pady=6, fill=tk.BOTH, expand=True)

        buttons = [
            ("임시 영역 선택", self._on_select_region),
            ("임시 영역을 확정 영역으로 덮어쓰기", self._on_set_region),
            ("영역 보기", self._on_view_regions),
            ("확정 영역을 북마크에 추가", self._on_save_active_bookmark),
            ("북마크 영역 불러오기", self._on_load_bookmark),
            ("'인식 후 복사' 버튼 분리하기", self._on_detach_ocr_button),
            ("설정", self._on_open_settings),
        ]
        for text, command in buttons:
            button = tk.Button(button_frame, text=text, command=command)
            button.pack(fill=tk.X, pady=4)

        ocr_button_frame = tk.Frame(
            button_frame,
            height=OCR_BUTTON_HEIGHT_PX,
        )
        ocr_button_frame.pack(fill=tk.X, pady=(10, 4))
        ocr_button_frame.pack_propagate(False)
        self.ocr_button = tk.Button(
            ocr_button_frame,
            text="인식 후 복사",
            command=lambda: self._on_ocr_copy(source_button=self.ocr_button),
            font=("Segoe UI", 14, "bold"),
        )
        self.ocr_button.pack(fill=tk.BOTH, expand=True)

        self._update_status()

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
        self._update_status()
        messagebox.showinfo("MekiCopy", "인식 영역이 설정되었습니다!")

    def _on_detach_ocr_button(self) -> None:
        if self.detached_window and self.detached_window.root.winfo_exists():
            self.detached_window.root.deiconify()
            self.detached_window.root.lift()
            return
        self.detached_window = DetachedOcrButtonWindow(self)

    def _on_open_settings(self) -> None:
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        self.settings_window = SettingsWindow(self)

    def _on_ocr_copy(self, source_button: tk.Button | None = None) -> None:
        if not self.active_region:
            messagebox.showerror("MekiCopy", "설정된 영역이 없습니다.", parent=self)
            return
        simple_feedback = self.settings.simple_copy_complete
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
        if self.detached_window:
            self.detached_window.capture_geometry()
        self.settings = settings
        self.attributes("-topmost", self.settings.main_always_on_top)
        if self.detached_window:
            self.detached_window.apply_settings()
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
        self.tray_icon.hide()
        if self.detached_window and self.detached_window.root.winfo_exists():
            self.detached_window.capture_geometry()
        save_settings(self.settings)
        self.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiCopy 영역 OCR 도구")
    parser.add_argument("--bookmark", help="저장된 북마크 이름으로 캡처")
    parser.add_argument("--pick-bookmark", action="store_true", help="북마크 목록에서 선택")
    parser.add_argument("--adjust-bookmark", help="북마크 영역을 불러와 미세조정 후 저장")
    parser.add_argument("--self-test-runtime", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test-ui", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def run_ui_self_test() -> None:
    _log_runtime_message("self_test_ui", "starting")
    app: MainWindow | None = None
    try:
        app = MainWindow()
        app.update_idletasks()
        ocr_button_height = app.ocr_button.winfo_height()
        if ocr_button_height != OCR_BUTTON_HEIGHT_PX:
            raise RuntimeError(
                f"main OCR button height is {ocr_button_height}, expected {OCR_BUTTON_HEIGHT_PX}"
            )

        bookmark = Bookmark(name="self-test", left=11, top=22, width=333, height=44)
        expected_region = Region(left=11, top=22, width=333, height=44)
        app._load_bookmark_region(bookmark)
        if app.active_region != expected_region:
            raise RuntimeError(f"bookmark did not populate active region: {app.active_region}")
        if "확정 영역 :\n설정되지 않음" in app.status_label.cget("text"):
            raise RuntimeError("bookmark status still reports no active region")

        tray_roundtrip = False
        if os.name == "nt" and app.tray_icon.show():
            tray_roundtrip = True
            app.withdraw()
            app.update_idletasks()
            app._restore_from_tray()
            app.update_idletasks()
            if app.state() != "normal":
                raise RuntimeError(f"tray restore left app in state: {app.state()}")

        app._on_detach_ocr_button()
        app.update_idletasks()
        if not app.detached_window or not app.detached_window.root.winfo_exists():
            raise RuntimeError("detached OCR button window was not created")

        app._on_open_settings()
        app.update_idletasks()
        if not app.settings_window or not app.settings_window.winfo_exists():
            raise RuntimeError("settings window was not created")

        app.settings_window._on_close()
        app.detached_window.close()
        save_settings(app.settings)
        _log_runtime_message(
            "self_test_ui",
            (
                f"main_ocr_button_height: {ocr_button_height}\n"
                f"tray_roundtrip: {tray_roundtrip}\n"
                f"settings_file: {SETTINGS_FILE}\n"
                f"icon_path: {_get_icon_path()}"
            ),
        )
    except Exception as exc:
        _log_runtime_error("self_test_ui", exc)
        raise SystemExit(1)
    finally:
        if app and app.winfo_exists():
            app.destroy()


def main() -> None:
    args = parse_args()
    if args.self_test_runtime:
        _log_runtime_message("self_test_runtime", "starting")
        engine = _get_ocr_engine()
        _log_runtime_message(
            "self_test_runtime",
            f"active_provider: {getattr(engine, 'active_provider', 'unknown')}",
        )
        return
    _prepare_tk_library_paths()
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
