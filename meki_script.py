from __future__ import annotations

import argparse
from collections import deque
import json
import os
import queue
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from app_identity import apply_tk_icon, set_windows_app_id
from companion_liveness import UiHeartbeat
from companion_manual_close import MANUAL_CLOSE_EXIT_CODE, publish_manual_close_signal
from runtime_paths import prepare_tk_environment
from service_ports import SCRIPT_DEFAULT_PORT
from system_logging import (
    capture_windowed_streams,
    configure_system_logging,
    is_debug_enabled,
    install_exception_hooks,
    install_tk_exception_hook,
    log_debug,
    log_directory,
    log_error,
    set_debug_enabled,
)

prepare_tk_environment("MekiCopyRuntime")
import tkinter as tk
from tkinter import messagebox


DEFAULT_PORT = SCRIPT_DEFAULT_PORT
DEFAULT_GEOMETRY = "780x560+120+120"
MAX_REQUEST_BYTES = 1024 * 1024
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
    debug_log: bool = False

    def update(self, data: dict[str, Any]) -> None:
        for key in self.__dataclass_fields__:
            if key not in data:
                continue
            value = data[key]
            if key == "topmost":
                value = bool(value)
            elif key == "debug_log":
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
        set_debug_enabled(self.debug_log)


@dataclass
class _TranscriptHistoryEntry:
    """One displayed transcript entry and its stable end boundary."""

    entry_id: str
    end_mark: str
    text_length: int


class ScriptWindow:
    # New transcript events are already delivered over a worker queue.  A
    # slower idle tick avoids waking the UI 20 times a second while it is just
    # displaying the last line; active bursts still drain promptly.
    ACTIVE_EVENT_POLL_MS = 50
    IDLE_EVENT_POLL_MS = 200
    # A remote producer must never be able to retain an unlimited transcript
    # backlog while Tk is busy repainting.  This still covers far more events
    # than a normal visual-novel line/translation burst.
    MAX_PENDING_EVENTS = 128
    MAX_EVENTS_PER_DRAIN = 24
    MAX_TRANSCRIPT_CHARS = 32 * 1024
    MAX_ENTRY_ID_CHARS = 512
    MAX_CONFIG_VALUE_CHARS = 4 * 1024
    # MekiScript is a live companion that can stay open for hours. Keep a
    # useful scrollback without retaining every historical line and ID for an
    # entire play session. The character limit also bounds rare long outputs.
    MAX_HISTORY_ENTRIES = 500
    MAX_HISTORY_CHARS = 256 * 1024
    _PENDING_TRANSLATION_TEXT = "번역 대기 중…"

    def __init__(
        self,
        root: tk.Tk,
        config: ScriptConfig,
        *,
        on_confirmed_close: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.config = config
        self._on_confirmed_close = on_confirmed_close
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue(
            maxsize=self.MAX_PENDING_EVENTS
        )
        self._event_count_lock = threading.Lock()
        self._dropped_event_count = 0
        self._trimmed_payload_count = 0
        self.entry_ids: set[str] = set()
        self._history: deque[_TranscriptHistoryEntry] = deque()
        self._history_by_id: dict[str, _TranscriptHistoryEntry] = {}
        self._history_chars = 0
        self._history_mark_serial = 0
        self.last_original = ""
        self.last_translation = ""
        self.translation_count = 0
        root.title("MekiScript")
        root.geometry(DEFAULT_GEOMETRY)
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = tk.Frame(root)
        frame.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(frame, wrap=tk.WORD, padx=18, pady=14, spacing3=5, cursor="arrow")
        scrollbar = tk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.apply_config()
        root.after(self.IDLE_EVENT_POLL_MS, self.drain)

    def apply_config(self) -> None:
        cfg = self.config
        self.root.attributes("-topmost", cfg.topmost)
        self.root.attributes("-alpha", cfg.opacity)
        self.root.configure(bg=cfg.bg_color)
        self.text.configure(bg=cfg.bg_color, fg=cfg.original_color, insertbackground=cfg.original_color)
        self.text.tag_configure("original", foreground=cfg.original_color, font=(normalize_font_name(cfg.original_font), cfg.original_size, "bold"), spacing1=8)
        self.text.tag_configure("translated", foreground=cfg.translated_color, font=(normalize_font_name(cfg.translated_font), cfg.translated_size), spacing3=10)
        self.text.tag_configure("pending", foreground=cfg.translated_color, font=(normalize_font_name(cfg.translated_font), max(8, cfg.translated_size - 2), "italic"))

    @staticmethod
    def _should_report_count(count: int) -> bool:
        """Log the first overload and then only exponentially spaced repeats."""
        return count == 1 or (count > 0 and count & (count - 1) == 0)

    def _record_payload_trim(self, kind: str, field: str, original_length: int) -> None:
        with self._event_count_lock:
            self._trimmed_payload_count += 1
            count = self._trimmed_payload_count
        if self._should_report_count(count):
            log_error(
                "event_payload_trimmed",
                (
                    f"Trimmed oversized {kind}.{field} payload (chars={original_length}); "
                    f"trimmed_total={count}."
                ),
            )

    def _bounded_text(self, kind: str, field: str, value: Any, limit: int) -> str:
        text = str(value)
        if len(text) <= limit:
            return text
        self._record_payload_trim(kind, field, len(text))
        return text[:limit]

    def _compact_event_payload(self, kind: str, payload: Any) -> Any:
        """Keep queued payloads small and discard fields the UI never consumes."""
        if kind in {"append", "translation"}:
            source = payload if isinstance(payload, dict) else {}
            return {
                "id": self._bounded_text(
                    kind,
                    "id",
                    source.get("id", ""),
                    self.MAX_ENTRY_ID_CHARS,
                ),
                "text": self._bounded_text(
                    kind,
                    "text",
                    source.get("text", ""),
                    self.MAX_TRANSCRIPT_CHARS,
                ),
            }
        if kind == "config":
            source = payload if isinstance(payload, dict) else {}
            compact: dict[str, Any] = {}
            for key in ScriptConfig.__dataclass_fields__:
                if key not in source:
                    continue
                value = source[key]
                if isinstance(value, str):
                    value = self._bounded_text(
                        kind,
                        key,
                        value,
                        self.MAX_CONFIG_VALUE_CHARS,
                    )
                compact[key] = value
            return compact
        # ``clear`` has no payload.  Dropping arbitrary data here also avoids
        # retaining a large request object for a no-op UI action.
        return None

    def _record_queue_overflow(self, kind: str) -> None:
        with self._event_count_lock:
            self._dropped_event_count += 1
            count = self._dropped_event_count
        if self._should_report_count(count):
            log_error(
                "event_queue_overflow",
                (
                    f"Dropped {kind!r} UI event because the {self.MAX_PENDING_EVENTS}-item "
                    f"MekiScript queue is full; dropped_total={count}."
                ),
            )

    def enqueue(self, kind: str, payload: Any) -> bool:
        if kind not in {"append", "translation", "config", "clear"}:
            log_error("event_queue_rejected", f"Ignoring unknown MekiScript event: {kind!r}")
            return False
        if is_debug_enabled():
            payload_detail = (
                sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            )
            log_debug("enqueue", f"kind: {kind}\npayload_keys: {payload_detail}")
        event = (kind, self._compact_event_payload(kind, payload))
        try:
            self.events.put_nowait(event)
            return True
        except queue.Full:
            self._record_queue_overflow(kind)
            return False

    def _append(self, payload: dict[str, Any]) -> None:
        entry_id = str(payload.get("id", "")).strip()
        original = str(payload.get("text", "")).strip()
        if not entry_id or not original or entry_id in self.entry_ids:
            return
        self.entry_ids.add(entry_id)
        self.last_original = original
        self.text.configure(state=tk.NORMAL)
        if self.text.index("end-1c") != "1.0":
            self.text.insert(tk.END, "\n")
        self.text.insert(tk.END, original + "\n", ("original",))
        start_mark = f"translation_start_{entry_id}"
        end_mark = f"translation_end_{entry_id}"
        self.text.mark_set(start_mark, tk.END + "-1c")
        self.text.mark_gravity(start_mark, tk.LEFT)
        self.text.insert(tk.END, self._PENDING_TRANSLATION_TEXT + "\n", ("pending",))
        self.text.mark_set(end_mark, tk.END + "-1c")
        # Keep this boundary attached to its own entry. RIGHT gravity made the
        # mark follow every later append at tk.END, so translating an earlier
        # entry deleted all transcript blocks that followed it.
        self.text.mark_gravity(end_mark, tk.LEFT)
        self._remember_history_entry(
            entry_id,
            # Account for the original, placeholder, line endings, and a
            # possible blank separator before this entry. Exact character
            # accounting is not required for the cap; translation replacement
            # below keeps the value aligned with the displayed text.
            len(original) + len(self._PENDING_TRANSLATION_TEXT) + 3,
        )
        self._trim_history()
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def _translation(self, payload: dict[str, Any]) -> None:
        entry_id = str(payload.get("id", "")).strip()
        start_mark = f"translation_start_{entry_id}"
        end_mark = f"translation_end_{entry_id}"
        if start_mark not in self.text.mark_names() or end_mark not in self.text.mark_names():
            return
        translated = str(payload.get("text", "")).strip() or "(번역 결과 없음)"
        self.last_translation = translated
        self.translation_count += 1
        self.text.configure(state=tk.NORMAL)
        self.text.delete(start_mark, end_mark)
        # Let the end mark follow only the replacement text, then pin it again
        # so later transcript appends can never expand this entry's range.
        self.text.mark_set(end_mark, start_mark)
        self.text.mark_gravity(end_mark, tk.RIGHT)
        self.text.insert(start_mark, translated + "\n", ("translated",))
        history_entry = self._history_by_id.get(entry_id)
        if history_entry is not None:
            # ``end_mark`` now sits after the replacement because it has
            # right gravity. Keep the persistent scrollback boundary there;
            # a delayed trim can then remove a whole original/translation pair
            # without leaving a stale translation marker at the document head.
            self.text.mark_set(history_entry.end_mark, end_mark)
            self.text.mark_gravity(history_entry.end_mark, tk.LEFT)
            change = len(translated) - len(self._PENDING_TRANSLATION_TEXT)
            history_entry.text_length += change
            self._history_chars += change
        self.text.mark_unset(start_mark, end_mark)
        self._trim_history()
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def _remember_history_entry(self, entry_id: str, text_length: int) -> None:
        self._history_mark_serial += 1
        end_mark = f"history_end_{self._history_mark_serial}"
        # ``tk.END`` is a stable boundary after the entry's final newline. A
        # left-gravity mark stays before later transcript appends; replacement
        # of this entry's pending translation resets it explicitly above.
        self.text.mark_set(end_mark, tk.END)
        self.text.mark_gravity(end_mark, tk.LEFT)
        entry = _TranscriptHistoryEntry(entry_id, end_mark, max(0, text_length))
        self._history.append(entry)
        self._history_by_id[entry_id] = entry
        self._history_chars += entry.text_length

    def _trim_history(self) -> None:
        """Discard whole oldest entries once bounded scrollback is exceeded."""

        while self._history and (
            len(self._history) > self.MAX_HISTORY_ENTRIES
            or self._history_chars > self.MAX_HISTORY_CHARS
        ):
            entry = self._history.popleft()
            self._history_by_id.pop(entry.entry_id, None)
            self.entry_ids.discard(entry.entry_id)
            try:
                # The end index is exclusive, so this removes exactly the
                # oldest entry (including its trailing newline) while all
                # newer boundary marks shift down with the remaining text.
                self.text.delete("1.0", entry.end_mark)
            except tk.TclError:
                # A destroyed or externally reset widget is handled by its
                # owner; still release the Python-side references below.
                pass
            self.text.mark_unset(
                entry.end_mark,
                f"translation_start_{entry.entry_id}",
                f"translation_end_{entry.entry_id}",
            )
            self._history_chars = max(0, self._history_chars - entry.text_length)

    def _clear_transcript(self) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        marks = [
            name
            for name in self.text.mark_names()
            if name.startswith(("translation_start_", "translation_end_", "history_end_"))
        ]
        if marks:
            self.text.mark_unset(*marks)
        self.text.configure(state=tk.DISABLED)
        self.entry_ids.clear()
        self._history.clear()
        self._history_by_id.clear()
        self._history_chars = 0

    def drain(self) -> None:
        handled_event = False
        for _ in range(self.MAX_EVENTS_PER_DRAIN):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            handled_event = True
            if kind == "append":
                self._append(payload)
            elif kind == "translation":
                self._translation(payload)
            elif kind == "config":
                self.config.update(payload)
                self.apply_config()
            elif kind == "clear":
                self._clear_transcript()
        self.root.after(
            (
                self.ACTIVE_EVENT_POLL_MS
                if handled_event or not self.events.empty()
                else self.IDLE_EVENT_POLL_MS
            ),
            self.drain,
        )

    def close(self) -> None:
        if not messagebox.askyesno(
            "MekiScript 종료",
            (
                "MekiScript 창을 닫을까요?\n\n"
                "MekiCopy에서 실행한 경우 자동 복구 대상에서 제외됩니다. 다시 사용하려면 "
                "MekiCopy에서 MekiScript를 실행하세요."
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


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length < 0 or length > MAX_REQUEST_BYTES:
        raise ValueError("요청 본문이 너무 큽니다.")
    return json.loads(handler.rfile.read(length).decode("utf-8")) if length else {}


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(
    window: ScriptWindow,
    ui_heartbeat: UiHeartbeat | None = None,
):
    def health_payload() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "app": "MekiScript",
            "entries": len(window.entry_ids),
            "translationCount": window.translation_count,
            "lastOriginal": window.last_original,
            "lastTranslation": window.last_translation,
        }
        if ui_heartbeat is not None:
            payload.update(ui_heartbeat.health_payload())
        return payload

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == "/health":
                _write_json(self, 200, health_payload())
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
                if not window.enqueue(kind, payload):
                    # A full bounded UI queue must be visible to producers.
                    # Returning 200 here made audio capture believe a line
                    # was displayed even though it had been discarded.
                    _write_json(
                        self,
                        503,
                        {
                            "ok": False,
                            "error": "MekiScript UI queue is busy; retry shortly",
                        },
                    )
                    return
                _write_json(self, 200, {"ok": True})
            except Exception as exc:
                log_error("http_request", exc)
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
    parser.add_argument("--watchdog-manual-close-file", help=argparse.SUPPRESS)
    parser.add_argument("--watchdog-manual-close-token", help=argparse.SUPPRESS)
    parser.add_argument("--debug-log", action="store_true")
    return parser.parse_args()


def run_transcript_self_test() -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        window = ScriptWindow(root, ScriptConfig(topmost=False))
        originals = [f"원문 {index}" for index in range(1, 9)]
        translations = [f"번역 {index}" for index in range(1, 9)]
        for index, original in enumerate(originals, 1):
            window._append({"id": f"chunk-{index}", "text": original})
        for index in range(8, 0, -1):
            window._translation({"id": f"chunk-{index}", "text": translations[index - 1]})

        transcript = window.text.get("1.0", "end-1c")
        for original, translated in zip(originals, translations):
            if transcript.count(original) != 1 or transcript.count(translated) != 1:
                raise RuntimeError("MekiScript 누적 대본 자체 검증에 실패했습니다.")
            if transcript.index(original) > transcript.index(translated):
                raise RuntimeError("원문보다 번역문이 먼저 표시되었습니다.")
        if window.translation_count != len(translations):
            raise RuntimeError("번역 완료 개수가 누적되지 않았습니다.")
        if window.last_translation != translations[0]:
            raise RuntimeError("마지막 번역 상태가 올바르지 않습니다.")

        # Verify that a long-running live session retains only bounded
        # scrollback and that removing an entry also removes its late-result
        # markers. This exercises the same Tk mark behavior used in production.
        window._clear_transcript()
        total_history_entries = window.MAX_HISTORY_ENTRIES + 8
        for index in range(total_history_entries):
            entry_id = f"history-{index}"
            window._append({"id": entry_id, "text": f"원문 history {index}"})
            window._translation({"id": entry_id, "text": f"번역 history {index}"})
        if len(window.entry_ids) != window.MAX_HISTORY_ENTRIES:
            raise RuntimeError("MekiScript 누적 대본 메모리 제한 검증에 실패했습니다.")
        transcript = window.text.get("1.0", "end-1c")
        if "원문 history 0" in transcript or f"원문 history {total_history_entries - 1}" not in transcript:
            raise RuntimeError("MekiScript 누적 대본 스크롤백 정리에 실패했습니다.")
    finally:
        root.destroy()


def main() -> int:
    args = parse_args()
    configure_system_logging("MekiScript", args.debug_log)
    install_exception_hooks()
    capture_windowed_streams()
    if args.self_test:
        ScriptConfig(opacity=args.opacity)
        run_transcript_self_test()
        return 0
    config = ScriptConfig(
        topmost=bool(args.topmost), bg_color=args.bg_color, opacity=args.opacity,
        original_color=args.original_color, original_size=args.original_size, original_font=args.original_font,
        translated_color=args.translated_color, translated_size=args.translated_size, translated_font=args.translated_font,
        debug_log=args.debug_log,
    )
    set_windows_app_id("MekiScript")
    root = tk.Tk()
    install_tk_exception_hook(root)
    apply_tk_icon(root)
    manual_close_confirmed = threading.Event()

    def on_confirmed_close() -> None:
        manual_close_confirmed.set()
        publish_manual_close_signal(
            args.watchdog_manual_close_file,
            args.watchdog_manual_close_token,
            app_name="MekiScript",
        )

    window = ScriptWindow(root, config, on_confirmed_close=on_confirmed_close)
    ui_heartbeat = UiHeartbeat()
    ui_heartbeat.schedule(root)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        make_handler(window, ui_heartbeat),
    )
    server.daemon_threads = True
    server.block_on_close = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root.mainloop()
    server.shutdown()
    server.server_close()
    return MANUAL_CLOSE_EXIT_CODE if manual_close_confirmed.is_set() else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log_error("main", exc)
        if "--self-test" not in sys.argv[1:]:
            try:
                messagebox.showerror(
                    "MekiScript",
                    f"예기치 않은 오류로 프로그램을 종료합니다.\n\n{exc}\n\n로그: {log_directory('error_log', 'MekiScript')}",
                )
            except Exception:
                pass
        raise SystemExit(1) from exc
