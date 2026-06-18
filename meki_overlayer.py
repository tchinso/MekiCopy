from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import json
import os
import queue
import shutil
import sys
import threading
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from runtime_paths import log_dir as runtime_log_dir
from runtime_paths import path_for_tcl, tk_runtime_roots

TK_RUNTIME_DIRNAME = "MekiCopyRuntime"
_DLL_DIR_HANDLES = []

if getattr(sys, "frozen", False):
    _frozen_resource_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    if os.name == "nt":
        path_items = os.environ.get("PATH", "").split(os.pathsep)
        if _frozen_resource_dir not in path_items:
            os.environ["PATH"] = os.pathsep.join([_frozen_resource_dir, *path_items])
        if hasattr(os, "add_dll_directory"):
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(_frozen_resource_dir))
            except OSError:
                pass
    os.environ.setdefault(
        "TCL_LIBRARY",
        os.path.join(_frozen_resource_dir, "tcl", "tcl8.6"),
    )
    os.environ.setdefault(
        "TK_LIBRARY",
        os.path.join(_frozen_resource_dir, "tcl", "tk8.6"),
    )


import tkinter as tk

DEFAULT_PORT = 6551
DEFAULT_GEOMETRY = "780x180+120+120"
_WINDOW_STREAM = None


def _get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_resource_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", _get_app_dir())
    return os.path.dirname(os.path.abspath(__file__))


def _tcl_runtime_can_read(path: str) -> bool:
    if os.name != "nt":
        return os.path.exists(path)
    try:
        dll = ctypes.CDLL("tcl86t.dll")
        dll.Tcl_CreateInterp.restype = ctypes.c_void_p
        interp = dll.Tcl_CreateInterp()
        if not interp:
            return os.path.exists(path)
        dll.Tcl_Eval.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.Tcl_Eval.restype = ctypes.c_int
        dll.Tcl_GetStringResult.argtypes = [ctypes.c_void_p]
        dll.Tcl_GetStringResult.restype = ctypes.c_char_p
        tcl_path = path.replace("\\", "/")
        dll.Tcl_Eval(interp, f"file exists {{{tcl_path}}}".encode("utf-8"))
        result = dll.Tcl_GetStringResult(interp)
        return bool(result and result.decode("ascii", errors="ignore") == "1")
    except Exception:
        return os.path.exists(path)


def _log_dir(kind: str) -> Path:
    return runtime_log_dir("MekiOverlayer", kind)


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_log(kind: str, filename: str, text: str) -> None:
    content = text.rstrip() + "\n"
    try:
        with (_log_dir(kind) / filename).open("a", encoding="utf-8") as handle:
            handle.write(content)
            return
    except OSError:
        pass

    try:
        fallback = Path(_get_app_dir()) / kind
        fallback.mkdir(parents=True, exist_ok=True)
        with (fallback / filename).open("a", encoding="utf-8") as handle:
            handle.write(content)
    except OSError:
        pass


def log_error(stage: str, exc: BaseException | str) -> None:
    lines = ["", "=== ERROR ===", f"time: {_timestamp()}", f"stage: {stage}"]
    if isinstance(exc, BaseException):
        lines.append(f"type: {type(exc).__name__}")
        lines.append(f"message: {exc}")
        lines.append(traceback.format_exc())
    else:
        lines.append(f"message: {exc}")
    _write_log("error_log", "mekioverlayer_error.log", "\n".join(lines))


def log_debug(enabled: bool, stage: str, message: str) -> None:
    if not enabled:
        return
    lines = ["", "=== DEBUG ===", f"time: {_timestamp()}", f"stage: {stage}", message]
    _write_log("debug_log", "mekioverlayer_debug.log", "\n".join(lines))


def _prepare_windowed_streams() -> None:
    global _WINDOW_STREAM
    if sys.stderr is None:
        _WINDOW_STREAM = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = _WINDOW_STREAM
    if sys.stdout is None:
        sys.stdout = sys.stderr


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

    def use_tk_paths(tcl_path: str, tk_path: str) -> bool:
        tcl_env = path_for_tcl(tcl_path)
        tk_env = path_for_tcl(tk_path)
        safe_init = os.path.join(tcl_env, "init.tcl")
        safe_tk_script = os.path.join(tk_env, "tk.tcl")
        if _tcl_runtime_can_read(safe_init) and _tcl_runtime_can_read(safe_tk_script):
            os.environ["TCL_LIBRARY"] = tcl_env.replace("\\", "/")
            os.environ["TK_LIBRARY"] = tk_env.replace("\\", "/")
            return True
        return False

    if use_tk_paths(source_tcl, source_tk):
        return

    for safe_root_path in tk_runtime_roots(TK_RUNTIME_DIRNAME):
        safe_root = str(safe_root_path)
        safe_tcl = os.path.join(safe_root, "tcl8.6")
        safe_tk = os.path.join(safe_root, "tk8.6")
        try:
            if not os.path.exists(os.path.join(safe_tcl, "init.tcl")):
                shutil.copytree(source_tcl, safe_tcl, dirs_exist_ok=True)
            if not os.path.exists(os.path.join(safe_tk, "tk.tcl")):
                shutil.copytree(source_tk, safe_tk, dirs_exist_ok=True)
            if use_tk_paths(safe_tcl, safe_tk):
                return
        except OSError as exc:
            log_error("prepare_tk_library_paths", exc)

    use_tk_paths(source_tcl, source_tk)


@dataclass
class OverlayConfig:
    topmost: bool = True
    hide_titlebar: bool = False
    fixed_size: bool = False
    bg_color: str = "#111111"
    opacity: float = 0.78
    text_color: str = "#ffffff"
    text_size: int = 28
    text_font: str = "Malgun Gothic"
    debug_log: bool = False

    def update_from_dict(self, data: dict[str, Any]) -> None:
        for key in (
            "topmost",
            "hide_titlebar",
            "fixed_size",
            "bg_color",
            "opacity",
            "text_color",
            "text_size",
            "text_font",
            "debug_log",
        ):
            if key not in data:
                continue
            value = data[key]
            if key in {"topmost", "hide_titlebar", "fixed_size", "debug_log"}:
                setattr(self, key, bool(value))
            elif key == "opacity":
                setattr(self, key, max(0.1, min(1.0, float(value))))
            elif key == "text_size":
                setattr(self, key, max(8, min(96, int(value))))
            else:
                setattr(self, key, str(value))


class OverlayerApp:
    def __init__(self, root: tk.Tk, config: OverlayConfig) -> None:
        self.root = root
        self.config = config
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._drag_start: tuple[int, int] | None = None
        self._window_start: tuple[int, int] | None = None

        self.root.title("MekiOverlayer")
        self.root.geometry(DEFAULT_GEOMETRY)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        self.label = tk.Label(
            self.root,
            text="",
            justify="center",
            anchor="center",
            padx=18,
            pady=14,
        )
        self.label.pack(fill=tk.BOTH, expand=True)
        self.root.bind("<Configure>", self._on_configure)
        self.root.bind("<ButtonPress-1>", self._on_drag_start)
        self.root.bind("<B1-Motion>", self._on_drag_move)
        self.label.bind("<ButtonPress-1>", self._on_drag_start)
        self.label.bind("<B1-Motion>", self._on_drag_move)
        self.apply_config()
        self.root.after(50, self._drain_events)

    def enqueue_text(self, text: str) -> None:
        self.events.put(("text", text))

    def enqueue_config(self, data: dict[str, Any]) -> None:
        self.events.put(("config", data))

    def apply_config(self) -> None:
        cfg = self.config
        self.root.withdraw()
        self.root.overrideredirect(cfg.hide_titlebar)
        self.root.configure(bg=cfg.bg_color)
        self.root.attributes("-alpha", cfg.opacity)
        self.root.attributes("-topmost", cfg.topmost)
        self.root.resizable(not cfg.fixed_size, not cfg.fixed_size)
        if cfg.fixed_size:
            self.root.update_idletasks()
            width = max(240, self.root.winfo_width())
            height = max(80, self.root.winfo_height())
            self.root.minsize(width, height)
            self.root.maxsize(width, height)
        else:
            self.root.minsize(240, 80)
            self.root.maxsize(10000, 10000)
        self.label.configure(
            bg=cfg.bg_color,
            fg=cfg.text_color,
            font=(cfg.text_font, cfg.text_size, "bold"),
        )
        self._update_wraplength()
        self.root.deiconify()

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget == self.root:
            self._update_wraplength()

    def _update_wraplength(self) -> None:
        width = max(80, self.root.winfo_width() - 36)
        self.label.configure(wraplength=width)

    def _on_drag_start(self, event: tk.Event) -> None:
        if not self.config.hide_titlebar:
            return
        self._drag_start = (event.x_root, event.y_root)
        self._window_start = (self.root.winfo_x(), self.root.winfo_y())

    def _on_drag_move(self, event: tk.Event) -> None:
        if not self._drag_start or not self._window_start:
            return
        dx = event.x_root - self._drag_start[0]
        dy = event.y_root - self._drag_start[1]
        self.root.geometry(f"+{self._window_start[0] + dx}+{self._window_start[1] + dy}")

    def _drain_events(self) -> None:
        while True:
            try:
                event_type, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event_type == "text":
                self.label.configure(text=str(payload))
                self.root.deiconify()
                self.root.lift()
            elif event_type == "config":
                self.config.update_from_dict(payload)
                self.apply_config()
        self.root.after(50, self._drain_events)


def _read_request_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(app_ref: OverlayerApp):
    class OverlayerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                _write_json(self, 200, {"ok": True, "app": "MekiOverlayer"})
                return
            if parsed.path == "/show":
                query = parse_qs(parsed.query)
                text = query.get("text", [""])[0]
                app_ref.enqueue_text(text)
                _write_json(self, 200, {"ok": True})
                return
            _write_json(self, 404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                payload = _read_request_json(self)
                if parsed.path == "/show":
                    app_ref.enqueue_text(str(payload.get("text", "")))
                    log_debug(app_ref.config.debug_log, "show", f"chars: {len(str(payload.get('text', '')))}")
                    _write_json(self, 200, {"ok": True})
                    return
                if parsed.path == "/config":
                    app_ref.enqueue_config(payload)
                    _write_json(self, 200, {"ok": True})
                    return
                _write_json(self, 404, {"ok": False, "error": "not found"})
            except Exception as exc:
                log_error("http_post", exc)
                _write_json(self, 500, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            log_debug(app_ref.config.debug_log, "http", format % args)

    return OverlayerHandler


def run_server(app_ref: OverlayerApp, port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app_ref))
    log_debug(app_ref.config.debug_log, "server", f"listening on 127.0.0.1:{port}")
    server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiOverlayer")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--topmost", type=int, default=1)
    parser.add_argument("--hide-titlebar", type=int, default=0)
    parser.add_argument("--fixed-size", type=int, default=0)
    parser.add_argument("--bg-color", default="#111111")
    parser.add_argument("--opacity", type=float, default=0.78)
    parser.add_argument("--text-color", default="#ffffff")
    parser.add_argument("--text-size", type=int, default=28)
    parser.add_argument("--text-font", default="Malgun Gothic")
    parser.add_argument("--debug-log", action="store_true")
    return parser.parse_args()


def main() -> int:
    _prepare_windowed_streams()
    _prepare_tk_library_paths()
    args = parse_args()
    config = OverlayConfig(
        topmost=bool(args.topmost),
        hide_titlebar=bool(args.hide_titlebar),
        fixed_size=bool(args.fixed_size),
        bg_color=args.bg_color,
        opacity=args.opacity,
        text_color=args.text_color,
        text_size=args.text_size,
        text_font=args.text_font,
        debug_log=args.debug_log,
    )
    try:
        root = tk.Tk()
        app_ref = OverlayerApp(root, config)
        thread = threading.Thread(target=run_server, args=(app_ref, args.port), daemon=True)
        thread.start()
        root.mainloop()
        return 0
    except Exception as exc:
        log_error("main", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
