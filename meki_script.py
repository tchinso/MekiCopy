from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from runtime_paths import prepare_tk_environment

prepare_tk_environment("MekiCopyRuntime")
import tkinter as tk


DEFAULT_PORT = 6552
DEFAULT_GEOMETRY = "780x560+120+120"
_WINDOW_STREAM = None


def normalize_font_name(value: str) -> str:
    return str(value).strip().lstrip("@").strip() or "Malgun Gothic"


@dataclass
class ScriptConfig:
    topmost: bool = True
    bg_color: str = "#111111"
    opacity: float = 0.90
    original_color: str = "#f4f4f5"
    original_size: int = 20
    original_font: str = "Yu Gothic UI"
    translated_color: str = "#7dd3fc"
    translated_size: int = 20
    translated_font: str = "Malgun Gothic"

    def update(self, data: dict[str, Any]) -> None:
        for key in self.__dataclass_fields__:
            if key not in data:
                continue
            value = data[key]
            if key == "topmost":
                value = bool(value)
            elif key == "opacity":
                value = max(0.1, min(1.0, float(value)))
            elif key.endswith("_size"):
                value = max(8, min(96, int(value)))
            elif key.endswith("_font"):
                value = normalize_font_name(str(value))
            else:
                value = str(value)
            setattr(self, key, value)


class ScriptWindow:
    def __init__(self, root: tk.Tk, config: ScriptConfig) -> None:
        self.root = root
        self.config = config
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.entry_ids: set[str] = set()
        root.title("MekiScript")
        root.geometry(DEFAULT_GEOMETRY)
        frame = tk.Frame(root)
        frame.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(frame, wrap=tk.WORD, padx=18, pady=14, spacing3=5, cursor="arrow")
        scrollbar = tk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.apply_config()
        root.after(50, self.drain)

    def apply_config(self) -> None:
        cfg = self.config
        self.root.attributes("-topmost", cfg.topmost)
        self.root.attributes("-alpha", cfg.opacity)
        self.root.configure(bg=cfg.bg_color)
        self.text.configure(bg=cfg.bg_color, fg=cfg.original_color, insertbackground=cfg.original_color)
        self.text.tag_configure("original", foreground=cfg.original_color, font=(normalize_font_name(cfg.original_font), cfg.original_size, "bold"), spacing1=8)
        self.text.tag_configure("translated", foreground=cfg.translated_color, font=(normalize_font_name(cfg.translated_font), cfg.translated_size), spacing3=10)
        self.text.tag_configure("pending", foreground=cfg.translated_color, font=(normalize_font_name(cfg.translated_font), max(8, cfg.translated_size - 2), "italic"))

    def enqueue(self, kind: str, payload: Any) -> None:
        self.events.put((kind, payload))

    def _append(self, payload: dict[str, Any]) -> None:
        entry_id = str(payload.get("id", "")).strip()
        original = str(payload.get("text", "")).strip()
        if not entry_id or not original or entry_id in self.entry_ids:
            return
        self.entry_ids.add(entry_id)
        self.text.configure(state=tk.NORMAL)
        if self.text.index("end-1c") != "1.0":
            self.text.insert(tk.END, "\n")
        self.text.insert(tk.END, original + "\n", ("original",))
        start_mark = f"translation_start_{entry_id}"
        end_mark = f"translation_end_{entry_id}"
        self.text.mark_set(start_mark, tk.END + "-1c")
        self.text.mark_gravity(start_mark, tk.LEFT)
        self.text.insert(tk.END, "번역 대기 중…\n", ("pending",))
        self.text.mark_set(end_mark, tk.END + "-1c")
        self.text.mark_gravity(end_mark, tk.RIGHT)
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def _translation(self, payload: dict[str, Any]) -> None:
        entry_id = str(payload.get("id", "")).strip()
        start_mark = f"translation_start_{entry_id}"
        end_mark = f"translation_end_{entry_id}"
        if start_mark not in self.text.mark_names() or end_mark not in self.text.mark_names():
            return
        translated = str(payload.get("text", "")).strip() or "(번역 결과 없음)"
        self.text.configure(state=tk.NORMAL)
        self.text.delete(start_mark, end_mark)
        self.text.insert(start_mark, translated + "\n", ("translated",))
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def drain(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "append":
                self._append(payload)
            elif kind == "translation":
                self._translation(payload)
            elif kind == "config":
                self.config.update(payload)
                self.apply_config()
            elif kind == "clear":
                self.text.configure(state=tk.NORMAL)
                self.text.delete("1.0", tk.END)
                self.text.configure(state=tk.DISABLED)
                self.entry_ids.clear()
        self.root.after(50, self.drain)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    return json.loads(handler.rfile.read(length).decode("utf-8")) if length else {}


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(window: ScriptWindow):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == "/health":
                _write_json(self, 200, {"ok": True, "app": "MekiScript", "entries": len(window.entry_ids)})
            else:
                _write_json(self, 404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            try:
                payload = _read_json(self)
                routes = {"/append": "append", "/translation": "translation", "/config": "config", "/clear": "clear"}
                kind = routes.get(self.path)
                if not kind:
                    _write_json(self, 404, {"ok": False, "error": "not found"})
                    return
                window.enqueue(kind, payload)
                _write_json(self, 200, {"ok": True})
            except Exception as exc:
                _write_json(self, 500, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiScript")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--topmost", type=int, default=1)
    parser.add_argument("--bg-color", default="#111111")
    parser.add_argument("--opacity", type=float, default=0.90)
    parser.add_argument("--original-color", default="#f4f4f5")
    parser.add_argument("--original-size", type=int, default=20)
    parser.add_argument("--original-font", default="Yu Gothic UI")
    parser.add_argument("--translated-color", default="#7dd3fc")
    parser.add_argument("--translated-size", type=int, default=20)
    parser.add_argument("--translated-font", default="Malgun Gothic")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    global _WINDOW_STREAM
    if sys.stderr is None:
        _WINDOW_STREAM = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = _WINDOW_STREAM
    args = parse_args()
    if args.self_test:
        ScriptConfig(opacity=args.opacity)
        return 0
    config = ScriptConfig(
        topmost=bool(args.topmost), bg_color=args.bg_color, opacity=args.opacity,
        original_color=args.original_color, original_size=args.original_size, original_font=args.original_font,
        translated_color=args.translated_color, translated_size=args.translated_size, translated_font=args.translated_font,
    )
    root = tk.Tk()
    window = ScriptWindow(root, config)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(window))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root.mainloop()
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
