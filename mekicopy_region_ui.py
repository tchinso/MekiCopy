from __future__ import annotations

import ctypes
import tkinter as tk
from ctypes import wintypes
from typing import Callable

import mss
from tkinter import messagebox
from mekicopy_capture import MIN_SIZE_PX, Region, enable_dpi_awareness as _enable_dpi_awareness
from mekicopy_ocr import ocr_and_copy
from mekicopy_runtime import _prepare_tk_library_paths, _set_window_icon
from mekicopy_settings import Bookmark, Rect, load_bookmarks, save_detached_region

EDGE_GRAB_PX = 8
SELECTION_INSTRUCTION_FONT_SIZE = 36

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
