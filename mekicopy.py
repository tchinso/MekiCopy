import argparse
import os
import subprocess
import sys
import tempfile
import tkinter as tk
from dataclasses import dataclass
from typing import Callable
from tkinter import messagebox, simpledialog

from PIL import Image
import mss
import pyperclip

BOOKMARKS_FILE = os.path.join(os.path.dirname(__file__), "bookmarks.txt")
EDGE_GRAB_PX = 8
MIN_SIZE_PX = 10


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

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


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


def run_meikiocr(image_path: str) -> str:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    repo_root = os.path.dirname(__file__)
    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{repo_root}{os.pathsep}{pythonpath}" if pythonpath else repo_root
    )
    result = subprocess.run(
        [sys.executable, "-m", "meikiocr.cli", image_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    output = result.stdout.strip()
    if not output:
        output = result.stderr.strip()
    return postprocess_text(output)


def capture_region(left: int, top: int, width: int, height: int) -> Image.Image:
    with mss.mss() as sct:
        region = {"left": left, "top": top, "width": width, "height": height}
        sct_image = sct.grab(region)
        return Image.frombytes("RGB", sct_image.size, sct_image.rgb)


def copy_text_to_clipboard(text: str) -> None:
    pyperclip.copy(text)
    messagebox.showinfo("MekiCopy", "복사되었습니다!")


def ocr_and_copy(left: int, top: int, width: int, height: int) -> None:
    if width < MIN_SIZE_PX or height < MIN_SIZE_PX:
        messagebox.showerror("MekiCopy", "캡처 영역이 너무 작습니다.")
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
    copy_text_to_clipboard(text)


class SelectionUI:
    def __init__(
        self,
        root: tk.Tk | tk.Toplevel,
        initial_rect: Rect | None = None,
        on_confirm: Callable[[Region], None] | None = None,
        capture_on_confirm: bool = True,
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
        self.capture_on_confirm = capture_on_confirm
        self.bookmarks = load_bookmarks()

        with mss.mss() as sct:
            monitor = sct.monitors[0]
        self.virtual_left = monitor["left"]
        self.virtual_top = monitor["top"]
        self.virtual_width = monitor["width"]
        self.virtual_height = monitor["height"]

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
        self.root.bind("<Key-s>", self._on_save_bookmark)

    def _draw_instructions(self) -> None:
        text = "드래그로 영역 선택 → 가장자리 드래그로 미세 조정 → Enter 확정 / S 북마크 저장 / Esc 종료"
        self.canvas.create_text(
            20,
            20,
            anchor="nw",
            text=text,
            fill="white",
            font=("Segoe UI", 12, "bold"),
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
        if self.on_confirm:
            self.on_confirm(Region(left=left, top=top, width=width, height=height))
            self.root.destroy()
            return
        self.root.withdraw()
        self.root.update_idletasks()
        if self.capture_on_confirm:
            ocr_and_copy(left, top, width, height)
        self.root.destroy()

    def _on_save_bookmark(self, event: tk.Event | None = None) -> None:
        if not self.selection:
            return
        name = simpledialog.askstring("MekiCopy", "북마크 이름을 입력하세요")
        if not name:
            return
        rect = self.selection.normalized()
        left, top = self._screen_coords(rect.left, rect.top)
        self.bookmarks[name] = Bookmark(
            name=name,
            left=left,
            top=top,
            width=rect.width,
            height=rect.height,
        )
        save_bookmarks(self.bookmarks)
        messagebox.showinfo("MekiCopy", "북마크가 저장되었습니다!")

    def _on_cancel(self, event: tk.Event | None = None) -> None:
        self.root.destroy()


class BookmarkPicker(tk.Toplevel):
    def __init__(self, parent: tk.Tk, bookmarks: dict[str, Bookmark]):
        super().__init__(parent)
        self.title("MekiCopy 북마크 선택")
        self.bookmarks = bookmarks
        self.selected: Bookmark | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.geometry("320x240")
        self.indicator = tk.Label(self, text="북마크를 선택하세요")
        self.indicator.pack(pady=10)
        self.listbox = tk.Listbox(self)
        for name in sorted(self.bookmarks):
            self.listbox.insert(tk.END, name)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=10)
        button = tk.Button(self, text="선택", command=self._on_select)
        button.pack(pady=10)
        self.bind("<Return>", lambda _event: self._on_select())

    def _on_select(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        name = self.listbox.get(selection[0])
        self.selected = self.bookmarks[name]
        self.destroy()


def pick_bookmark(parent: tk.Tk) -> Bookmark | None:
    bookmarks = load_bookmarks()
    if not bookmarks:
        messagebox.showerror("MekiCopy", "저장된 북마크가 없습니다.")
        return None
    picker = BookmarkPicker(parent, bookmarks)
    picker.transient(parent)
    picker.grab_set()
    parent.wait_window(picker)
    return picker.selected


def run_picker_and_capture() -> None:
    root = tk.Tk()
    root.withdraw()
    bookmark = pick_bookmark(root)
    if bookmark:
        ocr_and_copy(bookmark.left, bookmark.top, bookmark.width, bookmark.height)
    root.destroy()


def _region_to_canvas_rect(region: Region) -> Rect:
    with mss.mss() as sct:
        monitor = sct.monitors[0]
    left_offset = monitor["left"]
    top_offset = monitor["top"]
    return Rect(
        region.left - left_offset,
        region.top - top_offset,
        region.right - left_offset,
        region.bottom - top_offset,
    )


def run_selection(initial_bookmark: Bookmark | None = None) -> None:
    root = tk.Tk()
    initial_rect = None
    if initial_bookmark:
        initial_rect = _region_to_canvas_rect(
            Region(
                left=initial_bookmark.left,
                top=initial_bookmark.top,
                width=initial_bookmark.width,
                height=initial_bookmark.height,
            )
        )
    SelectionUI(root, initial_rect=initial_rect)
    root.mainloop()


def run_selection_dialog(
    parent: tk.Tk,
    initial_region: Region | None = None,
    on_confirm: Callable[[Region], None] | None = None,
) -> None:
    initial_rect = _region_to_canvas_rect(initial_region) if initial_region else None
    overlay = tk.Toplevel(parent)
    SelectionUI(overlay, initial_rect=initial_rect, on_confirm=on_confirm)
    overlay.transient(parent)
    overlay.grab_set()
    parent.wait_window(overlay)


class ControlPanel(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MekiCopy")
        self.resizable(False, False)
        self.preview_region: Region | None = None
        self.capture_region: Region | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.geometry("420x260")
        self.status_preview = tk.Label(self, text=self._preview_text(), anchor="w")
        self.status_preview.pack(fill=tk.X, padx=12, pady=(12, 4))

        self.status_capture = tk.Label(self, text=self._capture_text(), anchor="w")
        self.status_capture.pack(fill=tk.X, padx=12, pady=(0, 12))

        button_frame = tk.Frame(self)
        button_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        tk.Button(
            button_frame, text="영역 지정", command=self._select_region
        ).pack(fill=tk.X, pady=4)
        tk.Button(
            button_frame, text="북마크 영역 불러오기", command=self._load_bookmark
        ).pack(fill=tk.X, pady=4)
        tk.Button(
            button_frame, text="영역 설정", command=self._set_capture_region
        ).pack(fill=tk.X, pady=4)
        tk.Button(
            button_frame, text="인식 후 복사", command=self._capture_and_copy
        ).pack(fill=tk.X, pady=4)

    def _preview_text(self) -> str:
        return f"현재 영역: {self._format_region(self.preview_region)}"

    def _capture_text(self) -> str:
        return f"기준 영역: {self._format_region(self.capture_region)}"

    @staticmethod
    def _format_region(region: Region | None) -> str:
        if not region:
            return "없음"
        return (
            f"좌상단({region.left}, {region.top}) "
            f"크기({region.width}x{region.height})"
        )

    def _refresh_labels(self) -> None:
        self.status_preview.configure(text=self._preview_text())
        self.status_capture.configure(text=self._capture_text())

    def _select_region(self) -> None:
        def on_confirm(region: Region) -> None:
            self.preview_region = region
            self._refresh_labels()

        run_selection_dialog(
            self, initial_region=self.preview_region, on_confirm=on_confirm
        )

    def _load_bookmark(self) -> None:
        bookmark = pick_bookmark(self)
        if not bookmark:
            return
        self.preview_region = Region(
            left=bookmark.left,
            top=bookmark.top,
            width=bookmark.width,
            height=bookmark.height,
        )
        self._refresh_labels()

    def _set_capture_region(self) -> None:
        if not self.preview_region:
            messagebox.showerror("MekiCopy", "먼저 영역을 지정하세요.")
            return
        self.capture_region = Region(
            left=self.preview_region.left,
            top=self.preview_region.top,
            width=self.preview_region.width,
            height=self.preview_region.height,
        )
        self._refresh_labels()
        messagebox.showinfo("MekiCopy", "기준 영역이 설정되었습니다.")

    def _capture_and_copy(self) -> None:
        if not self.capture_region:
            messagebox.showerror("MekiCopy", "기준 영역이 설정되어 있지 않습니다.")
            return
        ocr_and_copy(
            self.capture_region.left,
            self.capture_region.top,
            self.capture_region.width,
            self.capture_region.height,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiCopy 영역 OCR 도구")
    parser.add_argument("--bookmark", help="저장된 북마크 이름으로 캡처")
    parser.add_argument("--pick-bookmark", action="store_true", help="북마크 목록에서 선택")
    parser.add_argument("--adjust-bookmark", help="북마크 영역을 불러와 미세조정 후 저장")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
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
        run_selection(initial_bookmark=bookmark)
        return
    ControlPanel().mainloop()


if __name__ == "__main__":
    main()
