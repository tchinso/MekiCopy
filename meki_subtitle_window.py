"""MekiCopy-styled UI for creating Korean SRT files from video.

The window is opened from MekiCopy's ``새로운 자막 생성`` tab.  It deliberately
does not own any model assets: the job pipeline resolves the MekiAudioCapture
speech/VAD cache and sends translation requests to the already shared HYTrans
service.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from typing import Any, Callable

from hytrans.model_files import get_model_profile
from meki_subtitle_paths import shared_stt_model_root
from meki_subtitle_pipeline import (
    SubtitleCancelledError,
    SubtitleProcessSummary,
    process_video,
    translate_via_hytrans,
)
from mekicopy_theme import (
    BG,
    BORDER,
    BUTTON_FONT,
    DEFAULT_FONT,
    INK,
    MUTED,
    ROSE,
    SOFT,
    SUCCESS,
    SURFACE,
    TITLE_FONT,
    RoundedButton,
    style_standard_button,
    style_tree,
)


VIDEO_TYPES = [
    ("영상 파일", "*.mp4 *.mkv *.webm *.avi *.mov *.m4v *.ts *.mts *.m2ts *.wmv"),
    ("모든 파일", "*.*"),
]


class MekiSubtitleWindow(tk.Toplevel):
    """A model-sharing subtitle creator owned by a :class:`MainWindow`.

    ``start_hytrans`` is invoked only when a job starts.  It may return before
    HYTrans has finished loading or downloading its model; this window waits
    in its background job and keeps the UI responsive until the service is
    ready.
    """

    _POLL_INTERVAL_MS = 80
    _HYTRANS_READY_TIMEOUT_SECONDS = 600.0

    def __init__(
        self,
        parent: tk.Misc,
        *,
        settings: Any,
        hytrans_url: Callable[[], str],
        start_hytrans: Callable[[], bool | None],
        on_destroy: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._hytrans_url = hytrans_url
        self._start_hytrans = start_hytrans
        self._on_destroy_callback = on_destroy
        self._events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._closing = False
        self._output_was_edited = False

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.precision_var = tk.StringVar(
            value=str(getattr(settings, "audio_stt_precision", "int8"))
        )
        self.status_var = tk.StringVar(value="영상을 선택해 주세요.")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.translation_model_var = tk.StringVar()

        self.title("MekiSubtitle")
        self.geometry("860x700")
        self.minsize(700, 540)
        self.configure(bg=BG)
        self._set_icon(parent)
        self._load_stt_models()
        self._refresh_translation_model_label()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(self._POLL_INTERVAL_MS, self._poll_events)

    def _set_icon(self, parent: tk.Misc) -> None:
        try:
            icon_path = getattr(parent, "_get_icon_path", None)
            if callable(icon_path):
                self.iconbitmap(icon_path())
        except Exception:
            # The main process already applies the packaged icon.  A missing
            # icon must not make a subtitle task unavailable in source runs.
            pass

    def _load_stt_models(self) -> None:
        # Importing this registry does not load sherpa-onnx; it only exposes
        # the selected model labels and makes the window safe to open before a
        # first model download.
        from audio_capture_core import DEFAULT_STT_MODEL, STT_MODELS, normalize_stt_model

        self._stt_models = STT_MODELS
        self._stt_labels = {model.label: key for key, model in STT_MODELS.items()}
        configured = normalize_stt_model(
            getattr(self._settings, "audio_stt_model", DEFAULT_STT_MODEL)
        )
        self.stt_model_var = tk.StringVar(
            value=STT_MODELS[configured].label,
        )

    def _refresh_translation_model_label(self) -> None:
        profile = get_model_profile(
            getattr(self._settings, "hytrans_model_id", "mt1.5")
        )
        suffix = " (실험용)" if profile.key == "mt2" else " (기본)"
        self.translation_model_var.set(f"{profile.display_label}{suffix}")

    def refresh_settings(self, settings: Any) -> None:
        """Refresh defaults while idle after MekiCopy saves settings."""

        self._settings = settings
        self._refresh_translation_model_label()
        if self._worker and self._worker.is_alive():
            return
        from audio_capture_core import DEFAULT_STT_MODEL, normalize_stt_model

        selected = normalize_stt_model(
            getattr(settings, "audio_stt_model", DEFAULT_STT_MODEL)
        )
        self.stt_model_var.set(self._stt_models[selected].label)
        self.precision_var.set(
            str(getattr(settings, "audio_stt_precision", "int8"))
        )
        self._on_stt_model_changed()

    def _build_ui(self) -> None:
        outer = tk.Frame(self, bg=BG, padx=20, pady=18)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=1)

        tk.Label(
            outer,
            text="MekiSubtitle",
            bg=BG,
            fg=ROSE,
            font=("Malgun Gothic", 20, "bold"),
        ).grid(row=0, column=0, sticky=tk.W)
        tk.Label(
            outer,
            text="원본 오디오 → FAST VAD → 일본어 STT → HYTrans 한국어 SRT",
            bg=BG,
            fg=MUTED,
            font=DEFAULT_FONT,
        ).grid(row=1, column=0, sticky=tk.W, pady=(3, 15))

        paths = tk.LabelFrame(
            outer,
            text="파일",
            bg=SURFACE,
            fg=ROSE,
            font=BUTTON_FONT,
            padx=12,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        paths.grid(row=2, column=0, sticky=tk.EW)
        paths.columnconfigure(1, weight=1)

        tk.Label(paths, text="영상", bg=SURFACE, fg=INK).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 10)
        )
        self.input_entry = tk.Entry(
            paths,
            textvariable=self.input_var,
            bg=SURFACE,
            fg=INK,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ROSE,
        )
        self.input_entry.grid(row=0, column=1, sticky=tk.EW)
        self.browse_input = tk.Button(paths, text="선택…", command=self._choose_input)
        style_standard_button(self.browse_input)
        self.browse_input.grid(row=0, column=2, padx=(8, 0))

        tk.Label(paths, text="SRT", bg=SURFACE, fg=INK).grid(
            row=1, column=0, sticky=tk.W, padx=(0, 10), pady=(10, 0)
        )
        self.output_entry = tk.Entry(
            paths,
            textvariable=self.output_var,
            bg=SURFACE,
            fg=INK,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ROSE,
        )
        self.output_entry.grid(row=1, column=1, sticky=tk.EW, pady=(10, 0))
        self.output_entry.bind("<Key>", lambda _event: self._mark_output_edited())
        self.browse_output = tk.Button(
            paths,
            text="저장 위치…",
            command=self._choose_output,
        )
        style_standard_button(self.browse_output)
        self.browse_output.grid(row=1, column=2, padx=(8, 0), pady=(10, 0))

        options = tk.LabelFrame(
            outer,
            text="처리 설정",
            bg=SURFACE,
            fg=ROSE,
            font=BUTTON_FONT,
            padx=12,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        options.grid(row=3, column=0, sticky=tk.EW, pady=(12, 0))
        options.columnconfigure(1, weight=1)
        options.columnconfigure(3, weight=1)

        tk.Label(options, text="일본어 STT 모델", bg=SURFACE, fg=INK).grid(
            row=0, column=0, sticky=tk.W
        )
        self.stt_model_combo = ttk.Combobox(
            options,
            textvariable=self.stt_model_var,
            values=tuple(model.label for model in self._stt_models.values()),
            state="readonly",
            width=38,
        )
        self.stt_model_combo.grid(row=0, column=1, sticky=tk.W, padx=(10, 24))
        self.stt_model_combo.bind("<<ComboboxSelected>>", self._on_stt_model_changed)

        tk.Label(options, text="Reazon 정밀도", bg=SURFACE, fg=INK).grid(
            row=0, column=2, sticky=tk.W
        )
        self.precision_combo = ttk.Combobox(
            options,
            textvariable=self.precision_var,
            values=("int8", "fp32"),
            state="readonly",
            width=10,
        )
        self.precision_combo.grid(row=0, column=3, sticky=tk.W, padx=(10, 0))

        tk.Label(options, text="번역 모델", bg=SURFACE, fg=INK).grid(
            row=1, column=0, sticky=tk.W, pady=(10, 0)
        )
        tk.Label(
            options,
            textvariable=self.translation_model_var,
            bg=SURFACE,
            fg=INK,
            anchor=tk.W,
        ).grid(row=1, column=1, columnspan=3, sticky=tk.EW, padx=(10, 0), pady=(10, 0))

        tk.Label(
            options,
            text=(
                "VAD는 MekiAudioCapture FAST 기준: silence 0.25초 · 최대 20초 · "
                "앞/뒤 여백 0.15/0.35초\n"
                "번역 모델은 MekiCopy 설정의 HYTrans 선택을 공유합니다."
            ),
            bg=SURFACE,
            fg=MUTED,
            justify=tk.LEFT,
            anchor=tk.W,
            font=DEFAULT_FONT,
        ).grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=(10, 0))
        self._on_stt_model_changed()

        progress_frame = tk.Frame(outer, bg=BG)
        progress_frame.grid(row=4, column=0, sticky=tk.EW, pady=(15, 0))
        self.progress = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        )
        self.progress.pack(fill=tk.X)
        tk.Label(
            progress_frame,
            textvariable=self.status_var,
            bg=BG,
            fg=INK,
            anchor=tk.W,
        ).pack(fill=tk.X, pady=(5, 0))

        log_frame = tk.LabelFrame(
            outer,
            text="처리 기록",
            bg=SURFACE,
            fg=ROSE,
            font=BUTTON_FONT,
            padx=8,
            pady=8,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        log_frame.grid(row=5, column=0, sticky=tk.NSEW, pady=(12, 0))
        self.log_text = tk.Text(
            log_frame,
            wrap=tk.WORD,
            height=11,
            state=tk.DISABLED,
            bg="#fffafa",
            fg=INK,
            insertbackground=ROSE,
            relief=tk.FLAT,
            font=("Malgun Gothic", 9),
        )
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        buttons = tk.Frame(outer, bg=BG)
        buttons.grid(row=6, column=0, sticky=tk.EW, pady=(13, 0))
        self.open_folder_button = tk.Button(
            buttons,
            text="출력 폴더 열기",
            command=self._open_output_folder,
            state=tk.DISABLED,
        )
        style_standard_button(self.open_folder_button)
        self.open_folder_button.pack(side=tk.LEFT)
        self.cancel_button = tk.Button(
            buttons,
            text="취소",
            command=self._cancel,
            state=tk.DISABLED,
        )
        style_standard_button(self.cancel_button)
        self.cancel_button.pack(side=tk.RIGHT)
        self.start_button = RoundedButton(
            buttons,
            text="한국어 SRT 만들기",
            command=self._start,
            variant="primary",
            height=39,
            radius=18,
        )
        self.start_button.pack(side=tk.RIGHT, padx=(0, 8))

        style_tree(self)

    def _mark_output_edited(self) -> None:
        self._output_was_edited = True

    def _choose_input(self) -> None:
        selected = filedialog.askopenfilename(
            title="영상 파일 선택",
            filetypes=VIDEO_TYPES,
            parent=self,
        )
        if not selected:
            return
        self.input_var.set(selected)
        if not self._output_was_edited or not self.output_var.get().strip():
            source = Path(selected)
            self.output_var.set(str(source.with_suffix(".ko.srt")))
        self.status_var.set("준비되었습니다.")

    def _choose_output(self) -> None:
        current = self.output_var.get().strip()
        selected = filedialog.asksaveasfilename(
            title="SRT 저장 위치",
            initialdir=str(Path(current).parent) if current else None,
            initialfile=Path(current).name if current else "subtitle.ko.srt",
            defaultextension=".srt",
            filetypes=[("SubRip 자막", "*.srt")],
            parent=self,
        )
        if selected:
            self.output_var.set(selected)
            self._output_was_edited = True

    def _selected_stt_key(self) -> str:
        from audio_capture_core import DEFAULT_STT_MODEL

        return self._stt_labels.get(self.stt_model_var.get(), DEFAULT_STT_MODEL)

    def _on_stt_model_changed(self, _event: object = None) -> None:
        model = self._stt_models[self._selected_stt_key()]
        if model.supports_fp32:
            self.precision_combo.configure(state="readonly")
        else:
            self.precision_var.set("int8")
            self.precision_combo.configure(state=tk.DISABLED)

    def _set_running(self, running: bool) -> None:
        field_state = tk.DISABLED if running else tk.NORMAL
        combo_state = tk.DISABLED if running else "readonly"
        for widget in (
            self.input_entry,
            self.output_entry,
            self.browse_input,
            self.browse_output,
        ):
            widget.configure(state=field_state)
        self.stt_model_combo.configure(state=combo_state)
        self.precision_combo.configure(state=tk.DISABLED if running else "readonly")
        if not running:
            self._on_stt_model_changed()
        self.start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.cancel_button.configure(state=tk.NORMAL if running else tk.DISABLED)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, str(text).rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _start(self) -> None:
        input_text = self.input_var.get().strip()
        output_text = self.output_var.get().strip()
        input_path = Path(input_text)
        if not input_text or not input_path.is_file():
            messagebox.showerror("MekiSubtitle", "유효한 영상 파일을 선택해 주세요.", parent=self)
            return
        if not output_text:
            messagebox.showerror("MekiSubtitle", "SRT 저장 경로를 지정해 주세요.", parent=self)
            return
        output_path = Path(output_text)
        if output_path.exists() and not messagebox.askyesno(
            "MekiSubtitle",
            f"이미 존재하는 파일을 덮어쓸까요?\n\n{output_path}",
            parent=self,
        ):
            return

        # Start the shared worker before expensive STT preparation.  HYTrans
        # itself keeps local files first and starts its verified downloader only
        # when this selected model is absent.
        try:
            started = self._start_hytrans()
        except Exception as exc:
            messagebox.showerror("MekiSubtitle", f"HYTrans 실행 실패:\n{exc}", parent=self)
            return
        if started is False:
            # MainWindow has already shown the concrete launch/port error.
            # Do not begin STT only to leave this job waiting for a server that
            # could not be created.
            return

        self._cancel_event.clear()
        self._set_running(True)
        self.open_folder_button.configure(state=tk.DISABLED)
        self.progress_var.set(0)
        self.status_var.set("자막 생성을 시작합니다.")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)
        stt_model = self._selected_stt_key()
        precision = self.precision_var.get()
        self._worker = threading.Thread(
            target=self._run_job,
            args=(input_path, output_path, stt_model, precision),
            name="MekiSubtitle-Job",
            daemon=False,
        )
        self._worker.start()

    def _emit_status(self, ratio: float, message: str) -> None:
        self._events.put(("status", ratio, message))

    def _wait_for_hytrans_ready(self) -> str:
        """Wait for the active HYTrans worker without blocking Tk."""

        import urllib.error
        import urllib.request
        import json

        base_url = self._hytrans_url().rstrip("/")
        deadline = time.monotonic() + self._HYTRANS_READY_TIMEOUT_SECONDS
        last_message = ""
        requested_reopen = False
        while True:
            if self._cancel_event.is_set():
                raise SubtitleCancelledError("사용자가 자막 생성을 취소했습니다.")
            try:
                request = urllib.request.Request(f"{base_url}/ready")
                with urllib.request.urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if payload.get("ready") and payload.get("workerConnected"):
                    return base_url
                state = str(payload.get("state") or "HYTrans 준비 중")
                error = str(payload.get("error") or "").strip()
                message = state if not error else f"{state}: {error}"
                if message != last_message:
                    self._emit_status(0.66, f"HYTrans 번역 모델을 기다리고 있습니다: {message}")
                    last_message = message
                if state.upper() == "ERROR" and not requested_reopen:
                    reopen = urllib.request.Request(
                        f"{base_url}/worker/reopen",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(reopen, timeout=5):
                        pass
                    requested_reopen = True
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
                message = f"HYTrans 서버 연결 대기 중: {exc}"
                if message != last_message:
                    self._emit_status(0.66, message)
                    last_message = message
            if time.monotonic() >= deadline:
                raise TimeoutError("HYTrans 번역 모델 준비 시간이 10분을 초과했습니다.")
            time.sleep(0.35)

    def _translate(self, text: str, *, timeout: float = 600.0, cancel_event=None) -> str:
        del cancel_event
        base_url = self._wait_for_hytrans_ready()
        return translate_via_hytrans(
            base_url,
            text,
            timeout=timeout,
            cancel_event=self._cancel_event,
        )

    def _run_job(
        self,
        input_path: Path,
        output_path: Path,
        stt_model: str,
        precision: str,
    ) -> None:
        try:
            summary = process_video(
                input_path,
                output_path,
                stt_model=stt_model,
                precision=precision,
                vad_preset="FAST",
                translate=self._translate,
                stt_model_root=shared_stt_model_root(),
                status=self._emit_status,
                log=lambda text: self._events.put(("log", text)),
                cancel_event=self._cancel_event,
            )
            self._events.put(("done", summary))
        except SubtitleCancelledError as exc:
            self._events.put(("cancelled", str(exc)))
        except Exception as exc:
            self._events.put(("error", str(exc), traceback.format_exc()))

    def _poll_events(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "status":
                    self.progress_var.set(max(0, min(100, float(event[1]) * 100)))
                    self.status_var.set(str(event[2]))
                elif kind == "log":
                    self._append_log(str(event[1]))
                elif kind == "done":
                    self._finish_success(event[1])
                elif kind == "cancelled":
                    self._finish_cancelled(str(event[1]))
                elif kind == "error":
                    self._finish_error(str(event[1]), str(event[2]))
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(self._POLL_INTERVAL_MS, self._poll_events)

    def _finish_success(self, summary: SubtitleProcessSummary) -> None:
        self._worker = None
        self._set_running(False)
        self.progress_var.set(100)
        self.status_var.set(
            f"완료: {summary.recognized}개 인식, {summary.translated}개 번역 "
            f"({summary.elapsed:.1f}초)"
        )
        self.open_folder_button.configure(state=tk.NORMAL)
        warning = (
            f"\n번역 실패 {summary.translation_failures}개는 일본어 원문으로 표시했습니다."
            if summary.translation_failures
            else ""
        )
        if self._closing:
            self._notify_destroyed()
            self.destroy()
            return
        messagebox.showinfo(
            "MekiSubtitle",
            f"한국어 SRT를 저장했습니다.\n\n{summary.output_path}{warning}",
            parent=self,
        )

    def _finish_cancelled(self, message: str) -> None:
        self._worker = None
        self._set_running(False)
        self.status_var.set(message)
        self._append_log(message)
        if self._closing:
            self._notify_destroyed()
            self.destroy()

    def _finish_error(self, message: str, detail: str) -> None:
        self._worker = None
        self._set_running(False)
        self.status_var.set(f"처리 실패: {message}")
        self._append_log(detail)
        if self._closing:
            self._notify_destroyed()
            self.destroy()
            return
        messagebox.showerror("MekiSubtitle", f"처리하지 못했습니다.\n\n{message}", parent=self)

    def _cancel(self) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel_event.set()
            self.cancel_button.configure(state=tk.DISABLED)
            self.status_var.set("취소하고 임시 파일을 정리하고 있습니다…")

    @property
    def is_running(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def cancel_for_parent_shutdown(self) -> None:
        """Request cancellation without showing another nested close dialog."""

        self._closing = True
        self._cancel()
        try:
            self.withdraw()
        except tk.TclError:
            pass

    def _open_output_folder(self) -> None:
        output = Path(self.output_var.get().strip())
        folder = output.parent if output.parent.is_dir() else Path.cwd()
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("MekiSubtitle", str(folder), parent=self)

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno(
                "MekiSubtitle",
                "진행 중인 작업을 취소하고 종료할까요?",
                parent=self,
            ):
                return
            self._closing = True
            self._cancel()
            self.withdraw()
            return
        self._notify_destroyed()
        self.destroy()

    def _notify_destroyed(self) -> None:
        callback = self._on_destroy_callback
        self._on_destroy_callback = None
        if callback is not None:
            callback()
