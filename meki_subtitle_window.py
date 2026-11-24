"""MekiCopy-styled UI for creating Japanese or Korean SRT files from video.

The window is opened from MekiCopy's ``자막 생성`` tab.  It deliberately
does not own any model assets: the job pipeline resolves the MekiAudioCapture
speech/VAD cache and sends translation requests to the already shared HYTrans
service.
"""

from __future__ import annotations

from collections import deque
import os
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QVBoxLayout, QWidget,
)

from app_identity import apply_qt_icon
from qt_companion import UiWakeup
from typing import Any, Callable
from companion_http import urlopen

from hytrans.model_files import get_model_profile
from hytrans.api_settings import BACKEND_LABELS, load_api_settings, normalize_backend
from meki_subtitle_paths import shared_stt_model_root
from meki_subtitle_pipeline import (
    SubtitleCancelledError,
    SubtitleProcessSummary,
    japanese_subtitle_path,
    process_video,
    translate_via_hytrans,
)
from mekicopy_theme import RoundedButton, configure_window_theme, make_font, messagebox


VIDEO_FILTER = "영상 파일 (*.mp4 *.mkv *.webm *.avi *.mov *.m4v *.ts *.mts *.m2ts *.wmv);;모든 파일 (*)"


class MekiSubtitleWindow(QDialog):
    """A model-sharing subtitle creator owned by a :class:`MainWindow`.

    ``start_hytrans`` is invoked only when a job starts.  It may return before
    HYTrans has finished loading or downloading its model; this window waits
    in its background job and keeps the UI responsive until the service is
    ready.
    """

    _HYTRANS_READY_TIMEOUT_SECONDS = 600.0
    _PENDING_LOG_MAX_ENTRIES = 512
    _VISIBLE_LOG_MAX_LINES = 2_000

    def __init__(
        self,
        parent: QWidget,
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
        # Completion/error events must never be dropped.  Frequent status and
        # diagnostic log updates use bounded, coalesced handoff buffers below
        # so a long video cannot retain an unbounded UI backlog.
        self._events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._event_lock = threading.Lock()
        self._latest_status: tuple[float, str] | None = None
        self._pending_logs: deque[str] = deque()
        self._dropped_log_count = 0
        self._cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._closing = False
        self._output_was_edited = False

        self._wakeup = UiWakeup(self, self._poll_events)
        self.setWindowTitle("MekiSubtitle")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        configure_window_theme(self)
        apply_qt_icon(self)
        self._load_stt_models()
        self._build_ui()
        self.refresh_settings(settings)
        self.setMinimumSize(self.minimumSizeHint())
        self.resize(720, 530)

    def _load_stt_models(self) -> None:
        from audio_capture_core import STT_MODELS

        self._stt_models = STT_MODELS

    def _refresh_translation_model_label(self) -> None:
        if self.skip_translation_check.isChecked():
            self.translation_model_label.setText("사용하지 않음 · 일본어 SRT만 생성")
            return
        backend = normalize_backend(getattr(self._settings, "hytrans_backend", "local"))
        if backend in {"translator_api", "hdtrans"}:
            self.translation_model_label.setText(f"{BACKEND_LABELS[backend]} · 일본어 → 한국어")
        elif backend != "local":
            model = load_api_settings().profiles[backend].model
            self.translation_model_label.setText(f"{BACKEND_LABELS[backend]} · {model}")
        else:
            profile = get_model_profile(getattr(self._settings, "hytrans_model_id", "mt1.5"))
            self.translation_model_label.setText(profile.display_label)

    def refresh_settings(self, settings: Any) -> None:
        """Refresh defaults while idle after MekiCopy saves settings."""

        self._settings = settings
        self._refresh_translation_model_label()
        if self.is_running:
            return
        from audio_capture_core import DEFAULT_STT_MODEL, normalize_precision, normalize_stt_model

        selected = normalize_stt_model(getattr(settings, "audio_stt_model", DEFAULT_STT_MODEL))
        self.stt_model_combo.setCurrentIndex(self.stt_model_combo.findData(selected))
        self.precision_combo.setCurrentText(normalize_precision(getattr(settings, "audio_stt_precision", "int8")))
        self._on_stt_model_changed()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 14)
        outer.setSpacing(8)

        title = QLabel("MekiSubtitle")
        title.setProperty("role", "accent")
        title.setFont(make_font(("Malgun Gothic", 17, "bold")))
        outer.addWidget(title)

        paths = QGroupBox("파일")
        path_layout = QGridLayout(paths)
        path_layout.setColumnStretch(1, 1)
        path_layout.setHorizontalSpacing(8)
        path_layout.setVerticalSpacing(5)
        self.input_entry = QLineEdit()
        self.input_entry.editingFinished.connect(self._set_default_output)
        self.output_entry = QLineEdit()
        self.output_entry.textEdited.connect(self._mark_output_edited)
        self.browse_input = RoundedButton(text="영상 선택…", command=self._choose_input, height=34)
        self.browse_output = RoundedButton(text="저장 위치…", command=self._choose_output, height=34)
        for row, label, field, button in (
            (0, "영상", self.input_entry, self.browse_input),
            (1, "SRT", self.output_entry, self.browse_output),
        ):
            label_widget = QLabel(label)
            label_widget.setBuddy(field)
            path_layout.addWidget(label_widget, row, 0)
            path_layout.addWidget(field, row, 1)
            path_layout.addWidget(button, row, 2)
        self.browse_input.setAutoDefault(False)
        self.browse_output.setAutoDefault(False)
        outer.addWidget(paths)

        options = QGroupBox("처리 설정")
        options_layout = QGridLayout(options)
        options_layout.setColumnStretch(1, 1)
        options_layout.setVerticalSpacing(5)
        self.stt_model_combo = QComboBox()
        for key, model in self._stt_models.items():
            self.stt_model_combo.addItem(model.label, key)
        self.stt_model_combo.currentIndexChanged.connect(self._on_stt_model_changed)
        self.precision_combo = QComboBox()
        self.precision_combo.addItems(("int8", "fp32"))
        for column, text, combo in (
            (0, "일본어 STT 모델", self.stt_model_combo), (2, "Reazon 정밀도", self.precision_combo)
        ):
            label = QLabel(text)
            label.setBuddy(combo)
            options_layout.addWidget(label, 0, column)
            options_layout.addWidget(combo, 0, column + 1)

        self.translation_model_label = QLabel()
        self.translation_model_label.setWordWrap(True)
        self.translation_model_label.setTextFormat(Qt.TextFormat.PlainText)
        options_layout.addWidget(QLabel("번역 모델"), 1, 0)
        options_layout.addWidget(self.translation_model_label, 1, 1, 1, 3)
        self.skip_translation_check = QCheckBox("한국어로 번역하지 않음")
        self.skip_translation_check.toggled.connect(self._on_translation_changed)
        options_layout.addWidget(self.skip_translation_check, 2, 0, 1, 4)
        outer.addWidget(options)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setMaximumWidth(174)
        self.status_label = QLabel("영상을 선택해 주세요.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        status_layout = QHBoxLayout()
        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(self.progress)
        outer.addLayout(status_layout)

        log_group = QGroupBox("처리 기록")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(5, 4, 5, 5)
        self.log_text = QPlainTextEdit()
        self.log_text.setMinimumHeight(75)
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(self._VISIBLE_LOG_MAX_LINES)
        log_layout.addWidget(self.log_text)
        outer.addWidget(log_group, 1)

        buttons = QHBoxLayout()
        self.open_folder_button = RoundedButton(text="출력 폴더 열기", command=self._open_output_folder)
        self.cancel_button = RoundedButton(text="취소", command=self._cancel)
        self.start_button = RoundedButton(text="한국어 SRT 만들기", command=self._start, variant="primary")
        self.open_folder_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        buttons.addWidget(self.open_folder_button)
        buttons.addStretch()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.start_button)
        outer.addLayout(buttons)

    def _mark_output_edited(self, _text: str) -> None:
        self._output_was_edited = True

    def _on_translation_changed(self, skip_translation: bool) -> None:
        self._refresh_translation_model_label()
        self.start_button.setText("일본어 SRT 만들기" if skip_translation else "한국어 SRT 만들기")
        current = self.output_entry.text().strip()
        if current:
            output = Path(current)
            if skip_translation:
                output = japanese_subtitle_path(output)
            elif output.name.lower().endswith(".ja.srt"):
                output = output.with_name(output.name[:-7] + ".ko.srt")
            self.output_entry.setText(str(output))

    def _set_default_output(self) -> None:
        source = self.input_entry.text().strip()
        if source and Path(source).name and (not self._output_was_edited or not self.output_entry.text().strip()):
            suffix = ".ja.srt" if self.skip_translation_check.isChecked() else ".ko.srt"
            self.output_entry.setText(str(Path(source).with_suffix(suffix)))

    def _choose_input(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "영상 파일 선택", self.input_entry.text(), VIDEO_FILTER)
        if selected:
            self.input_entry.setText(selected)
            self._set_default_output()
            self.status_label.setText("준비되었습니다.")

    def _choose_output(self) -> None:
        current = self.output_entry.text().strip()
        proposed = current or ("subtitle.ja.srt" if self.skip_translation_check.isChecked() else "subtitle.ko.srt")
        dialog = QFileDialog(self, "SRT 저장 위치")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilter("SubRip 자막 (*.srt)")
        dialog.setDefaultSuffix("srt")
        dialog.selectFile(proposed)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.output_entry.setText(dialog.selectedFiles()[0])
            self._output_was_edited = True
            self._on_translation_changed(self.skip_translation_check.isChecked())

    def _selected_stt_key(self) -> str:
        return self.stt_model_combo.currentData()

    def _on_stt_model_changed(self, _index: int = 0) -> None:
        self.precision_combo.setEnabled(self._stt_models[self._selected_stt_key()].supports_fp32)

    def _set_running(self, running: bool) -> None:
        for widget in (
            self.input_entry, self.output_entry, self.browse_input, self.browse_output,
            self.skip_translation_check, self.stt_model_combo, self.start_button,
        ):
            widget.setEnabled(not running)
        self.precision_combo.setEnabled(
            not running and self._stt_models[self._selected_stt_key()].supports_fp32
        )
        self.cancel_button.setEnabled(running)

    def _append_log(self, text: str) -> None:
        self._append_logs((text,))

    def _append_logs(self, texts: tuple[str, ...] | list[str]) -> None:
        if texts:
            self.log_text.appendPlainText("\n".join(str(text).rstrip() for text in texts))

    def _start(self) -> None:
        input_text = self.input_entry.text().strip()
        output_text = self.output_entry.text().strip()
        input_path = Path(input_text)
        if not input_text or not input_path.is_file():
            messagebox.showerror("MekiSubtitle", "유효한 영상 파일을 선택해 주세요.", parent=self)
            return
        if not output_text:
            messagebox.showerror("MekiSubtitle", "SRT 저장 경로를 지정해 주세요.", parent=self)
            return
        translate_to_korean = not self.skip_translation_check.isChecked()
        output_path = Path(output_text)
        if not translate_to_korean:
            output_path = japanese_subtitle_path(output_path)
        elif output_path.name.lower().endswith(".ja.srt"):
            output_path = output_path.with_name(output_path.name[:-7] + ".ko.srt")
        self.output_entry.setText(str(output_path))
        japanese_path = japanese_subtitle_path(output_path)
        if input_path.resolve() in {output_path.resolve(), japanese_path.resolve()}:
            messagebox.showerror("MekiSubtitle", "입력 영상과 출력 SRT 경로가 같습니다.", parent=self)
            return
        existing = [path for path in dict.fromkeys((output_path, japanese_path)) if path.exists()]
        if existing:
            paths = "\n".join(str(path) for path in existing)
            if not messagebox.askyesno(
                "MekiSubtitle",
                f"이미 존재하는 파일을 덮어쓸까요?\n\n{paths}",
                parent=self,
            ):
                return

        wait_for_hytrans_before_stt = False
        if translate_to_korean:
            try:
                started = self._start_hytrans()
            except Exception as exc:
                messagebox.showerror("MekiSubtitle", f"HYTrans 실행 실패:\n{exc}", parent=self)
                return
            if started is False:
                # MainWindow has already shown the concrete launch/port error.
                return
            backend = normalize_backend(getattr(self._settings, "hytrans_backend", "local"))
            wait_for_hytrans_before_stt = backend == "local"

        precision = self.precision_combo.currentText() if self.precision_combo.isEnabled() else "int8"
        self._cancel_event.clear()
        self._set_running(True)
        self.open_folder_button.setEnabled(False)
        self.progress.setValue(0)
        self.status_label.setText("자막 생성을 시작합니다.")
        with self._event_lock:
            self._latest_status = None
            self._pending_logs.clear()
            self._dropped_log_count = 0
        self.log_text.clear()
        stt_model = self._selected_stt_key()
        self._worker = threading.Thread(
            target=self._run_job,
            args=(input_path, output_path, stt_model, precision,
                  translate_to_korean, wait_for_hytrans_before_stt),
            name="MekiSubtitle-Job",
            daemon=False,
        )
        try:
            self._worker.start()
        except Exception as exc:
            self._worker = None
            self._set_running(False)
            self.status_label.setText("자막 작업을 시작하지 못했습니다.")
            messagebox.showerror("MekiSubtitle", str(exc), parent=self)

    def _emit_status(self, ratio: float, message: str) -> None:
        # Progress is naturally superseded by the newest value.  Keeping only
        # that value prevents a fast STT pipeline from flooding the UI queue.
        with self._event_lock:
            self._latest_status = (ratio, message)
        self._wakeup.schedule()

    def _emit_log(self, text: str) -> None:
        # Logs are useful diagnostics, but retaining every recognition line
        # from a multi-hour video makes the UI increasingly expensive.  Keep
        # the newest bounded window and report any elided records to the user.
        with self._event_lock:
            if len(self._pending_logs) >= self._PENDING_LOG_MAX_ENTRIES:
                self._pending_logs.popleft()
                self._dropped_log_count += 1
            self._pending_logs.append(str(text))
        self._wakeup.schedule()

    def _take_pending_updates(self) -> tuple[tuple[float, str] | None, list[str]]:
        with self._event_lock:
            status = self._latest_status
            self._latest_status = None
            logs = list(self._pending_logs)
            self._pending_logs.clear()
            dropped_log_count = self._dropped_log_count
            self._dropped_log_count = 0
        if dropped_log_count:
            logs.insert(0, f"… 이전 처리 기록 {dropped_log_count}개는 생략했습니다.")
        return status, logs

    def _wait_for_hytrans_ready(self, *, progress_ratio: float = 0.66) -> str:
        """Wait for the active local or API translator without blocking the GUI."""

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
                with urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                backend = str(payload.get("backend") or "local")
                if payload.get("ready") and (backend not in {"local", "translator_api"} or payload.get("workerConnected")):
                    return base_url
                state = str(payload.get("state") or "HYTrans 준비 중")
                error = str(payload.get("error") or "").strip()
                message = state if not error else f"{state}: {error}"
                if backend not in {"local", "translator_api"} and error and not payload.get("ready"):
                    label = "hdTrans 설치·사전" if backend == "hdtrans" else "API 번역"
                    raise RuntimeError(f"HYTrans {label} 설정을 확인해 주세요: {error}")
                if message != last_message:
                    self._emit_status(progress_ratio, f"HYTrans 번역 모델을 기다리고 있습니다: {message}")
                    last_message = message
                if backend in {"local", "translator_api"} and state.upper() == "ERROR" and not requested_reopen:
                    reopen = urllib.request.Request(
                        f"{base_url}/worker/reopen",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(reopen, timeout=5):
                        pass
                    requested_reopen = True
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
                message = f"HYTrans 서버 연결 대기 중: {exc}"
                if message != last_message:
                    self._emit_status(progress_ratio, message)
                    last_message = message
            if time.monotonic() >= deadline:
                raise TimeoutError("HYTrans 번역 모델 준비 시간이 10분을 초과했습니다.")
            self._cancel_event.wait(0.35)

    def _translate(self, text: str, *, timeout: float = 600.0, cancel_event=None,
                   context_owner: str | None = None) -> str:
        del cancel_event
        base_url = self._wait_for_hytrans_ready()
        return translate_via_hytrans(
            base_url,
            text,
            timeout=timeout,
            cancel_event=self._cancel_event,
            context_owner=context_owner,
        )

    def _run_job(
        self,
        input_path: Path,
        output_path: Path,
        stt_model: str,
        precision: str,
        translate_to_korean: bool = True,
        wait_for_hytrans_before_stt: bool = False,
    ) -> None:
        try:
            if translate_to_korean and wait_for_hytrans_before_stt:
                self._emit_status(0.0, "번역 모델 로드가 완료된 후 일본어 인식을 시작합니다.")
                self._wait_for_hytrans_ready(progress_ratio=0.0)
            context_owner = f"subtitle:{uuid.uuid4().hex}"

            def translate_cue(text, *, timeout=600.0, cancel_event=None):
                return self._translate(text, timeout=timeout, cancel_event=cancel_event,
                                       context_owner=context_owner)

            summary = process_video(
                input_path,
                output_path,
                stt_model=stt_model,
                precision=precision,
                translate=translate_cue if translate_to_korean else None,
                translate_to_korean=translate_to_korean,
                stt_model_root=shared_stt_model_root(),
                status=self._emit_status,
                log=self._emit_log,
                cancel_event=self._cancel_event,
            )
            self._events.put(("done", summary))
        except SubtitleCancelledError as exc:
            self._events.put(("cancelled", str(exc)))
        except Exception as exc:
            self._events.put(("error", str(exc), traceback.format_exc()))
        finally:
            self._wakeup.schedule()

    def _poll_events(self) -> None:
        status, logs = self._take_pending_updates()
        if status is not None:
            self.progress.setValue(round(max(0, min(100, status[0] * 100))))
            self.status_label.setText(str(status[1]))
        if logs:
            self._append_logs(logs)
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "done":
                    self._finish_success(event[1])
                elif kind == "cancelled":
                    self._finish_cancelled(str(event[1]))
                elif kind == "error":
                    self._finish_error(str(event[1]), str(event[2]))
        except queue.Empty:
            pass

    def _finish_success(self, summary: SubtitleProcessSummary) -> None:
        self._worker = None
        self._set_running(False)
        self.progress.setValue(100)
        self.status_label.setText(
            f"완료: {summary.recognized}개 인식, {summary.translated}개 번역 "
            f"({summary.elapsed:.1f}초)"
        )
        self.open_folder_button.setEnabled(True)
        warning = (
            f"\n번역 실패 {summary.translation_failures}개는 일본어 원문으로 표시했습니다."
            f"\n일본어 SRT도 보존했습니다: {japanese_subtitle_path(summary.output_path)}"
            if summary.translation_failures
            else ""
        )
        if self._closing:
            self.close()
            return
        messagebox.showinfo(
            "MekiSubtitle",
            f"{'일본어' if self.skip_translation_check.isChecked() else '한국어'} SRT를 저장했습니다."
            f"\n\n{summary.output_path}{warning}",
            parent=self,
        )

    def _finish_cancelled(self, message: str) -> None:
        self._worker = None
        self._set_running(False)
        self.status_label.setText(message)
        self._append_log(message)
        if self._closing:
            self.close()

    def _finish_error(self, message: str, detail: str) -> None:
        self._worker = None
        self._set_running(False)
        self.status_label.setText(f"처리 실패: {message}")
        self._append_log(detail)
        if self._closing:
            self.close()
            return
        messagebox.showerror("MekiSubtitle", f"처리하지 못했습니다.\n\n{message}", parent=self)

    def _cancel(self) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("취소하고 있습니다. 저장된 일본어 SRT는 보존됩니다…")

    @property
    def is_running(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def cancel_for_parent_shutdown(self) -> None:
        """Request cancellation without showing another nested close dialog."""

        self._closing = True
        self._cancel()
        self.hide()

    def _open_output_folder(self) -> None:
        output = Path(self.output_entry.text().strip())
        folder = output.parent if output.parent.is_dir() else Path.cwd()
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("MekiSubtitle", str(folder), parent=self)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno(
                "MekiSubtitle",
                "진행 중인 작업을 취소하고 종료할까요?",
                parent=self,
            ):
                event.ignore()
                return
            self._closing = True
            self._cancel()
            self.hide()
            event.ignore()
            return
        self._notify_destroyed()
        event.accept()

    def reject(self) -> None:
        self.close()

    def _notify_destroyed(self) -> None:
        callback = self._on_destroy_callback
        self._on_destroy_callback = None
        if callback is not None:
            callback()
