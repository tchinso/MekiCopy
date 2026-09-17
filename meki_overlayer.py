from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import socket
import sys
import threading
import traceback
from dataclasses import dataclass
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from runtime_paths import is_ascii_path, path_for_tcl, sync_tk_runtime, tk_runtime_roots
from runtime_paths import prepare_tk_environment

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
prepare_tk_environment(TK_RUNTIME_DIRNAME)
import tkinter as tk
from tkinter import messagebox

from app_identity import apply_tk_icon, set_windows_app_id
from companion_liveness import UiHeartbeat
from companion_manual_close import MANUAL_CLOSE_EXIT_CODE, publish_manual_close_signal
from service_ports import OVERLAYER_DEFAULT_PORT
from system_logging import (
    capture_windowed_streams,
    configure_system_logging,
    install_exception_hooks,
    install_tk_exception_hook,
    log_directory,
    log_debug as system_debug,
    log_error as system_error,
    set_debug_enabled,
)

DEFAULT_PORT = OVERLAYER_DEFAULT_PORT
DEFAULT_GEOMETRY = "780x180+120+120"
MAX_REQUEST_BYTES = 1024 * 1024
_WINDOW_STREAM = None
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def _get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def normalize_font_name(font_name: str) -> str:
    normalized = str(font_name).strip().lstrip("@").strip()
    return normalized or "Malgun Gothic"


def _get_resource_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", _get_app_dir())
    return os.path.dirname(os.path.abspath(__file__))


def log_error(stage: str, exc: BaseException | str) -> None:
    system_error(stage, exc, component="MekiOverlayer")


def log_debug(enabled: bool, stage: str, message: str) -> None:
    system_debug(stage, message, component="MekiOverlayer", enabled=enabled)


def _prepare_windowed_streams() -> None:
    capture_windowed_streams()


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

    for safe_root_path in tk_runtime_roots(TK_RUNTIME_DIRNAME):
        safe_root = path_for_tcl(safe_root_path)
        if not is_ascii_path(safe_root):
            continue
        try:
            safe_tcl, safe_tk = sync_tk_runtime(source_tcl, source_tk, safe_root)
            if use_tk_paths(str(safe_tcl), str(safe_tk)):
                return
        except OSError as exc:
            log_error("prepare_tk_library_paths", exc)

    use_tk_paths(source_tcl, source_tk)


@dataclass
class OverlayConfig:
    topmost: bool = True
    hide_titlebar: bool = False
    fixed_size: bool = False
    exclude_from_capture: bool = False
    bg_color: str = "#111111"
    opacity: float = 0.78
    text_color: str = "#ffffff"
    text_size: int = 28
    text_font: str = "Malgun Gothic"
    debug_log: bool = False

    def __post_init__(self) -> None:
        self.text_font = normalize_font_name(self.text_font)

    def update_from_dict(self, data: dict[str, Any]) -> None:
        for key in (
            "topmost",
            "hide_titlebar",
            "fixed_size",
            "exclude_from_capture",
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
            if key in {
                "topmost",
                "hide_titlebar",
                "fixed_size",
                "exclude_from_capture",
                "debug_log",
            }:
                setattr(self, key, bool(value))
            elif key == "opacity":
                setattr(self, key, max(0.1, min(1.0, float(value))))
            elif key == "text_size":
                setattr(self, key, max(8, min(96, int(value))))
            elif key == "text_font":
                self.text_font = normalize_font_name(str(value))
            else:
                setattr(self, key, str(value))
        set_debug_enabled(self.debug_log)


class OverlayerApp:
    # Keep translation display responsive during a burst without burning a
    # 20 Hz Tk timer while the overlay is unchanged.
    ACTIVE_EVENT_POLL_MS = 50
    IDLE_EVENT_POLL_MS = 200
    # Text updates are coalesced into one latest-value slot.  Configuration
    # changes retain their ordering, but their queue is bounded so a stalled
    # Tk window cannot consume unbounded memory.
    MAX_PENDING_CONFIG_EVENTS = 32
    MAX_CONFIG_EVENTS_PER_DRAIN = 8
    MAX_TEXT_CHARS = 32 * 1024
    MAX_CONFIG_VALUE_CHARS = 4 * 1024

    def __init__(
        self,
        root: tk.Tk,
        config: OverlayConfig,
        *,
        on_confirmed_close: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.config = config
        self._on_confirmed_close = on_confirmed_close
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue(
            maxsize=self.MAX_PENDING_CONFIG_EVENTS
        )
        self._text_lock = threading.Lock()
        self._pending_text: str | None = None
        self._event_count_lock = threading.Lock()
        self._dropped_config_count = 0
        self._trimmed_payload_count = 0
        self.last_text = ""
        self._drag_start: tuple[int, int] | None = None
        self._window_start: tuple[int, int] | None = None

        self.root.title("MekiOverlayer")
        self.root.geometry(DEFAULT_GEOMETRY)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

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
        self.root.after(self.IDLE_EVENT_POLL_MS, self._drain_events)

    @staticmethod
    def _should_report_count(count: int) -> bool:
        """Log the first overload and then only exponentially spaced repeats."""
        return count == 1 or (count > 0 and count & (count - 1) == 0)

    def _record_payload_trim(self, field: str, original_length: int) -> None:
        with self._event_count_lock:
            self._trimmed_payload_count += 1
            count = self._trimmed_payload_count
        if self._should_report_count(count):
            log_error(
                "event_payload_trimmed",
                (
                    f"Trimmed oversized MekiOverlayer {field} payload (chars={original_length}); "
                    f"trimmed_total={count}."
                ),
            )

    def _bounded_text(self, field: str, value: Any, limit: int) -> str:
        text = str(value)
        if len(text) <= limit:
            return text
        self._record_payload_trim(field, len(text))
        return text[:limit]

    def _compact_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """Retain only configuration fields that ``OverlayConfig`` consumes."""
        source = data if isinstance(data, dict) else {}
        compact: dict[str, Any] = {}
        for key in OverlayConfig.__dataclass_fields__:
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, str):
                value = self._bounded_text(
                    key,
                    value,
                    self.MAX_CONFIG_VALUE_CHARS,
                )
            compact[key] = value
        return compact

    def _record_config_overflow(self) -> None:
        with self._event_count_lock:
            self._dropped_config_count += 1
            count = self._dropped_config_count
        if self._should_report_count(count):
            log_error(
                "event_queue_overflow",
                (
                    "Dropped a MekiOverlayer config event because the "
                    f"{self.MAX_PENDING_CONFIG_EVENTS}-item queue is full; "
                    f"dropped_total={count}."
                ),
            )

    def enqueue_text(self, text: str) -> None:
        # Showing an outdated translation is worse than skipping intermediate
        # frames.  One locked slot bounds memory and always lets Tk consume the
        # newest available text on its next tick.
        value = self._bounded_text("text", text, self.MAX_TEXT_CHARS)
        with self._text_lock:
            self.last_text = value
            self._pending_text = value

    def enqueue_config(self, data: dict[str, Any]) -> bool:
        try:
            self.events.put_nowait(("config", self._compact_config(data)))
            return True
        except queue.Full:
            self._record_config_overflow()
            return False

    def _take_pending_text(self) -> str | None:
        with self._text_lock:
            text = self._pending_text
            self._pending_text = None
            return text

    def _has_pending_text(self) -> bool:
        with self._text_lock:
            return self._pending_text is not None

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
            font=(normalize_font_name(cfg.text_font), cfg.text_size, "bold"),
        )
        self._update_wraplength()
        self.root.deiconify()
        self.root.update_idletasks()
        self._apply_capture_exclusion()

    def _apply_capture_exclusion(self) -> bool:
        if os.name != "nt":
            return False
        affinity = WDA_EXCLUDEFROMCAPTURE if self.config.exclude_from_capture else WDA_NONE
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            set_window_display_affinity = user32.SetWindowDisplayAffinity
            set_window_display_affinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            set_window_display_affinity.restype = wintypes.BOOL
            widget_hwnd = self.root.winfo_id()
            top_level_hwnd = user32.GetParent(widget_hwnd) or widget_hwnd
            if set_window_display_affinity(top_level_hwnd, affinity):
                log_debug(
                    self.config.debug_log,
                    "capture_exclusion",
                    f"SetWindowDisplayAffinity=0x{affinity:08X}",
                )
                return True
            error_code = ctypes.get_last_error()
            raise ctypes.WinError(error_code)
        except Exception as exc:
            log_error("set_window_display_affinity", exc)
            return False

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

    def close(self) -> None:
        if not messagebox.askyesno(
            "MekiOverlayer 종료",
            (
                "MekiOverlayer 창을 닫을까요?\n\n"
                "MekiCopy에서 실행한 경우 자동 복구 대상에서 제외됩니다. 다시 사용하려면 "
                "MekiCopy에서 MekiOverlayer를 실행하세요."
            ),
            parent=self.root,
        ):
            return
        if self._on_confirmed_close is not None:
            try:
                self._on_confirmed_close()
            except Exception as exc:
                log_error("manual_close_signal", exc)
        self.root.destroy()

    def _drain_events(self) -> None:
        handled_event = False
        for _ in range(self.MAX_CONFIG_EVENTS_PER_DRAIN):
            try:
                event_type, payload = self.events.get_nowait()
            except queue.Empty:
                break
            handled_event = True
            if event_type == "config":
                self.config.update_from_dict(payload)
                self.apply_config()
        text = self._take_pending_text()
        if text is not None:
            handled_event = True
            self.label.configure(text=text)
            self.root.deiconify()
            self.root.lift()
        self.root.after(
            (
                self.ACTIVE_EVENT_POLL_MS
                if handled_event or not self.events.empty() or self._has_pending_text()
                else self.IDLE_EVENT_POLL_MS
            ),
            self._drain_events,
        )


def _read_request_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length < 0 or length > MAX_REQUEST_BYTES:
        raise ValueError("요청 본문이 너무 큽니다.")
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


def make_handler(
    app_ref: OverlayerApp,
    ui_heartbeat: UiHeartbeat | None = None,
):
    def health_payload() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "app": "MekiOverlayer",
            "text": getattr(app_ref, "last_text", ""),
        }
        if ui_heartbeat is not None:
            payload.update(ui_heartbeat.health_payload())
        return payload

    class OverlayerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                _write_json(self, 200, health_payload())
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
                    if not app_ref.enqueue_config(payload):
                        # Settings must not look persisted when Tk has fallen
                        # behind its bounded queue. Native callers can retry
                        # this transient condition just like text delivery.
                        _write_json(
                            self,
                            503,
                            {
                                "ok": False,
                                "error": "MekiOverlayer UI queue is busy; retry shortly",
                            },
                        )
                        return
                    _write_json(self, 200, {"ok": True})
                    return
                _write_json(self, 404, {"ok": False, "error": "not found"})
            except Exception as exc:
                log_error("http_post", exc)
                _write_json(self, 500, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            log_debug(app_ref.config.debug_log, "http", format % args)

    return OverlayerHandler


def run_server(server: ThreadingHTTPServer, app_ref: OverlayerApp, port: int) -> None:
    log_debug(app_ref.config.debug_log, "server", f"listening on 127.0.0.1:{port}")
    server.serve_forever()


def ensure_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"MekiOverlayer를 시작할 수 없습니다. 127.0.0.1:{port} 포트가 이미 사용 중입니다."
            ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiOverlayer")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--topmost", type=int, default=1)
    parser.add_argument("--hide-titlebar", type=int, default=0)
    parser.add_argument("--fixed-size", type=int, default=0)
    parser.add_argument("--exclude-from-capture", type=int, default=0)
    parser.add_argument("--bg-color", default="#111111")
    parser.add_argument("--opacity", type=float, default=0.78)
    parser.add_argument("--text-color", default="#ffffff")
    parser.add_argument("--text-size", type=int, default=28)
    parser.add_argument("--text-font", default="Malgun Gothic")
    parser.add_argument("--watchdog-manual-close-file", help=argparse.SUPPRESS)
    parser.add_argument("--watchdog-manual-close-token", help=argparse.SUPPRESS)
    parser.add_argument("--debug-log", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_system_logging("MekiOverlayer", args.debug_log)
    set_debug_enabled(args.debug_log)
    install_exception_hooks()
    _prepare_windowed_streams()
    _prepare_tk_library_paths()
    set_windows_app_id("MekiOverlayer")
    config = OverlayConfig(
        topmost=bool(args.topmost),
        hide_titlebar=bool(args.hide_titlebar),
        fixed_size=bool(args.fixed_size),
        exclude_from_capture=bool(args.exclude_from_capture),
        bg_color=args.bg_color,
        opacity=args.opacity,
        text_color=args.text_color,
        text_size=args.text_size,
        text_font=args.text_font,
        debug_log=args.debug_log,
    )
    server: ThreadingHTTPServer | None = None
    server_started = False
    try:
        root = tk.Tk()
        install_tk_exception_hook(root)
        apply_tk_icon(root)
        manual_close_confirmed = threading.Event()

        def on_confirmed_close() -> None:
            manual_close_confirmed.set()
            publish_manual_close_signal(
                args.watchdog_manual_close_file,
                args.watchdog_manual_close_token,
                app_name="MekiOverlayer",
            )

        app_ref = OverlayerApp(root, config, on_confirmed_close=on_confirmed_close)
        ui_heartbeat = UiHeartbeat()
        ui_heartbeat.schedule(root)
        try:
            server = ThreadingHTTPServer(
                ("127.0.0.1", args.port),
                make_handler(app_ref, ui_heartbeat),
            )
        except OSError as exc:
            raise RuntimeError(
                f"MekiOverlayer를 시작할 수 없습니다. 127.0.0.1:{args.port} 포트가 이미 사용 중입니다."
            ) from exc
        server.daemon_threads = True
        server.block_on_close = False
        thread = threading.Thread(
            target=run_server,
            args=(server, app_ref, args.port),
            daemon=True,
        )
        thread.start()
        server_started = True
        root.mainloop()
        return MANUAL_CLOSE_EXIT_CODE if manual_close_confirmed.is_set() else 0
    except Exception as exc:
        log_error("main", exc)
        if "--self-test" not in sys.argv[1:]:
            try:
                messagebox.showerror(
                    "MekiOverlayer",
                    f"예기치 않은 오류로 프로그램을 종료합니다.\n\n{exc}\n\n로그: {log_directory('error_log', 'MekiOverlayer')}",
                )
            except Exception:
                pass
        return 1
    finally:
        if server is not None:
            if server_started:
                server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
