from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import wave
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import numpy as np
import soundcard as sc

from app_identity import apply_tk_icon, set_windows_app_id
from audio_capture_core import (
    CAPTURE_SAMPLE_RATE,
    DEFAULT_STT_MODEL,
    INTERNAL_SAMPLE_RATE,
    STTResult,
    STT_MODELS,
    SpeechSegment,
    VAD_PRESETS,
    append_script_text,
    build_segments,
    cleanup_work_files,
    collect_vad_intervals,
    create_recognizer,
    create_voice_activity_detector,
    effective_stt_precision,
    ensure_models,
    get_stt_model,
    model_paths_are_valid,
    model_root_candidates,
    normalize_precision,
    normalize_preset,
    normalize_stt_model,
    remove_overlap,
    recognize_segments,
    resolve_models,
    set_script_translation,
    translate_text,
    wav_to_mono_16k,
)
from runtime_paths import prepare_tk_environment, writable_app_subdir
from mekicopy_theme import (
    BG,
    BORDER,
    BUTTON_FONT,
    DEFAULT_FONT,
    INK,
    MUTED,
    ROSE,
    SOFT,
    SURFACE,
    TITLE_FONT,
    RoundedButton,
    configure_window_theme,
)
from service_ports import (
    AUDIO_CAPTURE_DEFAULT_PORT,
    HYTRANS_DEFAULT_PORT,
    SCRIPT_DEFAULT_PORT,
)
from system_logging import (
    capture_windowed_streams,
    configure_system_logging,
    install_exception_hooks,
    install_tk_exception_hook,
    log_debug,
    log_error,
    set_debug_enabled,
)

prepare_tk_environment("MekiCopyRuntime")
import tkinter as tk
from tkinter import messagebox


DEFAULT_PORT = AUDIO_CAPTURE_DEFAULT_PORT
DEFAULT_SCRIPT_URL = f"http://127.0.0.1:{SCRIPT_DEFAULT_PORT}"
DEFAULT_HYTRANS_URL = f"http://127.0.0.1:{HYTRANS_DEFAULT_PORT}"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RECORDING_SECONDS = 4 * 60 * 60
MIN_RECORDING_FREE_BYTES = 2 * 1024 * 1024 * 1024
AUDIO_TRANSLATION_TIMEOUT_SECONDS = 300
MAX_TRANSLATION_SESSION_SECONDS = 30 * 60
MAX_CONSECUTIVE_TRANSLATION_FAILURES = 2
REALTIME_STT_QUEUE_MAX_SEGMENTS = 24
REALTIME_TRANSLATION_QUEUE_MAX_SEGMENTS = 24
_SESSION_DIRECTORY_PATTERN = re.compile(r"^\d{8}-\d{6}-\d{6}$")
_WORK_SWEEP_LOCK = threading.Lock()
_WORK_SWEEP_DONE = False
_WINDOW_STREAM = None


def app_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", app_dir()))


def work_dir() -> Path:
    global _WORK_SWEEP_DONE
    path = writable_app_subdir("MekiAudioCapture", "work")
    with _WORK_SWEEP_LOCK:
        if not _WORK_SWEEP_DONE:
            _WORK_SWEEP_DONE = True
            cutoff = time.time() - 7 * 24 * 60 * 60
            try:
                children = list(path.iterdir())
            except OSError:
                children = []
            for child in children:
                try:
                    if (
                        child.is_dir()
                        and _SESSION_DIRECTORY_PATTERN.fullmatch(child.name)
                        and child.stat().st_mtime < cutoff
                    ):
                        cleanup_work_files(child)
                except OSError:
                    pass
    return path


def prepare_streams() -> None:
    capture_windowed_streams()


@dataclass(frozen=True)
class RealtimeTranslationSummary:
    recognized: int
    delivery_failures: int
    translation_failures: int
    dropped_segments: int
    fatal_error: str = ""


class RealtimeTranslationSession:
    """Process finalized Silero VAD chunks while WASAPI recording continues.

    The recorder thread exclusively owns the native VAD instance.  Offline
    recognition and the HTTP translation calls each have a dedicated worker so
    slow HYTrans responses do not hold up the next recognized utterance.
    """

    _WORKER_POLL_SECONDS = 0.1

    def __init__(
        self,
        models: dict[str, Path],
        stt_model: str,
        precision: str,
        preset: str,
        script_url: str,
        hytrans_url: str,
        session_id: str,
        report_status: Callable[[str], None],
    ) -> None:
        self.models = dict(models)
        self.stt_model = stt_model
        self.precision = precision
        self.preset = normalize_preset(preset)
        self.script_url = script_url
        self.hytrans_url = hytrans_url
        self.session_id = session_id
        self._report_status = report_status
        self._vad = None
        self._capture_remainder = np.empty(0, dtype=np.float32)
        self._vad_remainder = np.empty(0, dtype=np.float32)
        self._next_segment_id = 1
        self._previous_text = ""
        self._stt_queue: queue.Queue[SpeechSegment | object] = queue.Queue(
            maxsize=REALTIME_STT_QUEUE_MAX_SEGMENTS
        )
        self._translation_queue: queue.Queue[tuple[STTResult, str] | object] = queue.Queue(
            maxsize=REALTIME_TRANSLATION_QUEUE_MAX_SEGMENTS
        )
        self._stt_thread = threading.Thread(target=self._run_stt, daemon=True)
        self._translation_thread = threading.Thread(target=self._run_translation, daemon=True)
        self._closed = False
        self._started = False
        self._aborted = threading.Event()
        # A timed Queue.get plus these two events deliberately replaces a
        # terminal queue item.  A terminal item can itself block forever when
        # a failed consumer leaves a bounded queue full.
        self._stt_input_closed = threading.Event()
        self._translation_input_closed = threading.Event()
        self._close_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._delivery_lock = threading.Lock()
        self._recognized = 0
        self._delivery_failures = 0
        self._translation_failures = 0
        self._dropped_segments = 0
        self._fatal_error = ""
        self._last_overload_report = 0.0
        # A live recording may legitimately last hours.  Start the aggregate
        # translation drain limit only after recording has closed, rather than
        # silently dropping otherwise healthy live translations after 30 min.
        self._translation_deadline: float | None = None
        self._consecutive_translation_failures = 0

    def start(self) -> None:
        """Initialize VAD before opening loopback, then begin worker threads."""
        if self._started:
            return
        self._vad = create_voice_activity_detector(self.models["vad"], self.preset)
        self._started = True
        self._translation_thread.start()
        self._stt_thread.start()
        self._report_live_status("실시간 음성 번역이 켜졌습니다. 발화를 기다리고 있습니다…")

    def accept_capture_block(self, block: np.ndarray) -> None:
        """Feed one 48 kHz capture block without doing STT or HTTP work here."""
        if self._closed or self._aborted.is_set():
            return
        if self._vad is None:
            raise RuntimeError("실시간 VAD가 준비되지 않았습니다.")
        samples = np.asarray(block, dtype=np.float32)
        if samples.ndim == 2:
            samples = samples.mean(axis=1, dtype=np.float32)
        elif samples.ndim != 1:
            raise ValueError("지원하지 않는 실시간 오디오 블록 형식입니다.")
        if self._capture_remainder.size:
            samples = np.concatenate((self._capture_remainder, samples))
        ratio = CAPTURE_SAMPLE_RATE // INTERNAL_SAMPLE_RATE
        if ratio <= 0 or CAPTURE_SAMPLE_RATE % INTERNAL_SAMPLE_RATE:
            raise RuntimeError("실시간 오디오 샘플레이트 변환 설정이 올바르지 않습니다.")
        usable = (len(samples) // ratio) * ratio
        self._capture_remainder = samples[usable:].copy()
        if usable:
            mono_16k = samples[:usable].reshape(-1, ratio).mean(axis=1, dtype=np.float32)
            self._feed_vad(mono_16k)

    def finish_input(self) -> None:
        """Flush final VAD audio on the recorder thread and close STT input."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._aborted.is_set() or self._vad is None:
                self._stt_input_closed.set()
                return
            try:
                if self._capture_remainder.size:
                    ratio = CAPTURE_SAMPLE_RATE // INTERNAL_SAMPLE_RATE
                    padded = np.pad(self._capture_remainder, (0, ratio - len(self._capture_remainder)))
                    self._capture_remainder = np.empty(0, dtype=np.float32)
                    self._feed_vad(padded.reshape(-1, ratio).mean(axis=1, dtype=np.float32))
                if self._vad_remainder.size:
                    padded = np.pad(self._vad_remainder, (0, 512 - len(self._vad_remainder)))
                    self._vad_remainder = np.empty(0, dtype=np.float32)
                    self._vad.accept_waveform(padded)
                    self._drain_vad()
                self._vad.flush()
                self._drain_vad()
            finally:
                # The current live translation may continue indefinitely while
                # recording.  Only its remaining post-recording drain receives
                # the same 30-minute aggregate limit as batch translation.
                self._translation_deadline = time.monotonic() + MAX_TRANSLATION_SESSION_SECONDS
                self._stt_input_closed.set()

    def abort(self) -> None:
        """Stop workers after a recorder/VAD failure; normal Stop drains instead."""
        self._aborted.set()
        with self._close_lock:
            self._closed = True
            self._stt_input_closed.set()
            self._translation_input_closed.set()

    def wait_for_completion(self) -> RealtimeTranslationSummary:
        if self._started:
            for worker in (self._stt_thread, self._translation_thread):
                if worker.ident is not None:
                    worker.join()
        with self._stats_lock:
            return RealtimeTranslationSummary(
                recognized=self._recognized,
                delivery_failures=self._delivery_failures,
                translation_failures=self._translation_failures,
                dropped_segments=self._dropped_segments,
                fatal_error=self._fatal_error,
            )

    def has_active_workers(self) -> bool:
        """Return whether a failed session is still draining or cancelling."""
        return any(worker.is_alive() for worker in (self._stt_thread, self._translation_thread))

    def _feed_vad(self, samples: np.ndarray) -> None:
        if self._vad_remainder.size:
            samples = np.concatenate((self._vad_remainder, samples))
        usable = (len(samples) // 512) * 512
        self._vad_remainder = samples[usable:].copy()
        for start in range(0, usable, 512):
            self._vad.accept_waveform(np.asarray(samples[start : start + 512], dtype=np.float32))
            self._drain_vad()

    def _drain_vad(self) -> None:
        assert self._vad is not None
        while not self._vad.empty():
            item = self._vad.front
            # ``front`` points into the native VAD queue and becomes invalid
            # after ``pop`` or the next detector call.
            samples = np.array(item.samples, dtype=np.float32, copy=True)
            start = int(item.start)
            self._vad.pop()
            if not samples.size:
                continue
            duration = len(samples) / INTERNAL_SAMPLE_RATE
            segment = SpeechSegment(
                id=self._next_segment_id,
                start_time=start / INTERNAL_SAMPLE_RATE,
                end_time=(start + len(samples)) / INTERNAL_SAMPLE_RATE,
                duration=duration,
                audio=samples,
                is_forced_cut=False,
                is_short=duration <= VAD_PRESETS[self.preset]["merge_short_under"],
                previous_overlap=0.0,
            )
            self._next_segment_id += 1
            self._enqueue_segment(segment)

    def _enqueue_segment(self, segment: SpeechSegment) -> None:
        try:
            self._stt_queue.put_nowait(segment)
        except queue.Full:
            with self._stats_lock:
                self._dropped_segments += 1
                now = time.monotonic()
                if now - self._last_overload_report >= 2.0:
                    self._last_overload_report = now
                    report_overload = True
                else:
                    report_overload = False
            log_error(
                "realtime_stt_queue",
                f"실시간 STT 대기열이 가득 차 발화 {segment.id}을(를) 건너뜁니다.",
            )
            if report_overload:
                self._report_live_status("실시간 번역 처리 지연: 일부 발화가 건너뛰어질 수 있습니다.")

    def _report_live_status(self, status: str) -> None:
        if not self._aborted.is_set():
            self._report_status(status)

    def _deliver(self, stage: str, action: Callable[[], None]) -> bool:
        for attempt in range(2):
            if self._aborted.is_set():
                return False
            try:
                # Do not launch a delayed HTTP delivery after an aborted
                # session.  An already-running request cannot be cancelled,
                # but this closes the publish-after-abort race at its source.
                with self._delivery_lock:
                    if self._aborted.is_set():
                        return False
                    action()
                    return True
            except Exception as exc:
                log_error(stage, exc)
                if attempt == 0:
                    time.sleep(0.15)
        return False

    def _set_fatal_error(self, stage: str, exc: BaseException) -> None:
        log_error(stage, exc)
        with self._stats_lock:
            if not self._fatal_error:
                self._fatal_error = f"{stage}: {exc}"

    def _run_stt(self) -> None:
        try:
            recognizer = create_recognizer(
                self.models,
                model_key=self.stt_model,
                precision=self.precision,
            )
            while True:
                if self._aborted.is_set():
                    break
                try:
                    item = self._stt_queue.get(timeout=self._WORKER_POLL_SECONDS)
                except queue.Empty:
                    if self._stt_input_closed.is_set():
                        break
                    continue
                if self._aborted.is_set():
                    break
                assert isinstance(item, SpeechSegment)
                started = time.perf_counter()
                stream = recognizer.create_stream()
                stream.accept_waveform(INTERNAL_SAMPLE_RATE, item.audio)
                recognizer.decode_stream(stream)
                if self._aborted.is_set():
                    break
                text = str(stream.result.text).strip()
                if item.previous_overlap:
                    text = remove_overlap(self._previous_text, text)
                if not text:
                    continue
                result = STTResult(
                    segment_id=item.id,
                    start_time=item.start_time,
                    end_time=item.end_time,
                    duration=item.duration,
                    text_ja=text,
                    is_forced_cut=item.is_forced_cut,
                    is_short=item.is_short,
                    stt_latency=time.perf_counter() - started,
                )
                self._previous_text = text
                entry_id = f"{self.session_id}-{result.segment_id}"
                if not self._deliver(
                    "realtime_script_append",
                    lambda: append_script_text(self.script_url, result, entry_id=entry_id),
                ):
                    if self._aborted.is_set():
                        break
                    with self._stats_lock:
                        self._delivery_failures += 1
                if self._aborted.is_set():
                    break
                with self._stats_lock:
                    self._recognized += 1
                    recognized = self._recognized
                self._report_live_status(f"실시간 음성 번역: 일본어 {recognized}개를 인식했습니다…")
                self._enqueue_translation(result, entry_id)
        except Exception as exc:
            if not self._aborted.is_set():
                self._set_fatal_error("realtime_stt", exc)
        finally:
            # No more producers can enqueue translations after this point.
            # The consumer polls for this event, so it cannot deadlock behind
            # a full queue if the translator itself has already failed.
            self._translation_input_closed.set()

    def _enqueue_translation(self, result: STTResult, entry_id: str) -> None:
        if self._aborted.is_set():
            return
        try:
            self._translation_queue.put_nowait((result, entry_id))
        except queue.Full:
            with self._stats_lock:
                self._translation_failures += 1
            skipped = "[번역 건너뜀] 실시간 번역 대기열이 가득 찼습니다."
            if not self._deliver(
                "realtime_translation_overload",
                lambda: set_script_translation(
                    self.script_url,
                    result,
                    skipped,
                    entry_id=entry_id,
                ),
            ):
                with self._stats_lock:
                    self._delivery_failures += 1
            self._report_live_status("실시간 번역 처리 지연: 일부 번역을 건너뛰었습니다.")

    def _run_translation(self) -> None:
        translated_count = 0
        try:
            while True:
                if self._aborted.is_set():
                    break
                try:
                    item = self._translation_queue.get(timeout=self._WORKER_POLL_SECONDS)
                except queue.Empty:
                    if self._translation_input_closed.is_set():
                        break
                    continue
                if self._aborted.is_set():
                    break
                result, entry_id = item
                deadline = self._translation_deadline
                if deadline is not None and time.monotonic() >= deadline:
                    translated = "[번역 건너뜀] 이번 녹음의 전체 번역 제한 시간(30분)을 초과했습니다."
                    failed = True
                elif self._consecutive_translation_failures >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    translated = "[번역 건너뜀] HYTrans가 연속으로 응답하지 않아 나머지 요청을 중단했습니다."
                    failed = True
                else:
                    try:
                        translated = translate_text(
                            self.hytrans_url,
                            result.text_ja,
                            timeout=AUDIO_TRANSLATION_TIMEOUT_SECONDS,
                        )
                        if not translated:
                            raise RuntimeError("HYTrans가 빈 번역 결과를 반환했습니다.")
                        self._consecutive_translation_failures = 0
                        failed = False
                    except Exception as exc:
                        log_error("realtime_translate", exc)
                        translated = f"[번역 실패] {exc}"
                        self._consecutive_translation_failures += 1
                        failed = True
                if self._aborted.is_set():
                    break
                if failed:
                    with self._stats_lock:
                        self._translation_failures += 1
                if not self._deliver(
                    "realtime_script_translation",
                    lambda: set_script_translation(
                        self.script_url,
                        result,
                        translated,
                        entry_id=entry_id,
                    ),
                ):
                    if self._aborted.is_set():
                        break
                    with self._stats_lock:
                        self._delivery_failures += 1
                if self._aborted.is_set():
                    break
                translated_count += 1
                self._report_live_status(f"실시간 음성 번역: 번역 {translated_count}개를 처리했습니다…")
        except Exception as exc:
            if not self._aborted.is_set():
                self._set_fatal_error("realtime_translation", exc)


class CaptureController:
    def __init__(
        self,
        precision: str,
        preset: str,
        script_url: str,
        hytrans_url: str,
        *,
        stt_model: str = DEFAULT_STT_MODEL,
        prepare_models_on_start: bool = True,
    ) -> None:
        self.stt_model = normalize_stt_model(stt_model)
        self.precision = effective_stt_precision(self.stt_model, precision)
        self.preset = normalize_preset(preset)
        self.script_url = script_url.rstrip("/")
        self.hytrans_url = hytrans_url.rstrip("/")
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stop_event = threading.Event()
        self.record_thread: threading.Thread | None = None
        self.process_thread: threading.Thread | None = None
        self.model_thread: threading.Thread | None = None
        self.start_thread: threading.Thread | None = None
        self.prepared_models: dict[str, Path] | None = None
        self.prepared_models_model = ""
        self.prepared_models_precision = ""
        self.wav_path: Path | None = None
        self.session_work_dir: Path | None = None
        self.session_id = ""
        self.realtime_session: RealtimeTranslationSession | None = None
        self.session_options = (
            self.stt_model,
            self.precision,
            self.preset,
            self.script_url,
            self.hytrans_url,
            False,
        )
        self._session_generation = 0
        self.state = "READY"
        self.status = "녹음 준비"
        self.error = ""
        self._lock = threading.RLock()
        if prepare_models_on_start:
            self.prepare_models()

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "app": "MekiAudioCapture",
                "state": self.state,
                "status": self.status,
                "sttModel": self.stt_model,
                "precision": self.precision,
                "preset": self.preset,
                "error": self.error or None,
            }

    def configure(self, payload: dict[str, Any]) -> None:
        if any(
            str(key).casefold() in {"realtimetranslation", "real_time_translation"}
            for key in payload
        ):
            raise RuntimeError(
                "실시간 음성 번역은 MekiAudioCapture 창의 체크박스에서만 켤 수 있습니다."
            )
        with self._lock:
            if self.state not in {"READY", "ERROR"}:
                raise RuntimeError("녹음 또는 처리 중에는 설정을 바꿀 수 없습니다.")
            previous_model = self.stt_model
            previous_precision = self.precision
            self.stt_model = normalize_stt_model(
                str(payload.get("sttModel", payload.get("stt_model", self.stt_model)))
            )
            self.precision = effective_stt_precision(
                self.stt_model,
                str(payload.get("precision", self.precision)),
            )
            self.preset = normalize_preset(str(payload.get("preset", self.preset)))
            self.script_url = str(payload.get("scriptUrl", self.script_url)).rstrip("/")
            self.hytrans_url = str(payload.get("hytransUrl", self.hytrans_url)).rstrip("/")
            model_changed = self.stt_model != previous_model
            stt_configuration_changed = model_changed or self.precision != previous_precision
            if stt_configuration_changed:
                self.prepared_models = None
                self.prepared_models_model = ""
                self.prepared_models_precision = ""
        if "debugLog" in payload:
            set_debug_enabled(bool(payload["debugLog"]))
        if stt_configuration_changed:
            self.prepare_models()

    def _set_state(self, state: str, status: str, error: str = "") -> None:
        with self._lock:
            self.state = state
            self.status = status
            self.error = error
        self.events.put(("status", status))
        log_debug("state", f"state: {state}\nstatus: {status}")
        if error:
            log_error("state", error)

    def _set_state_for_session(
        self,
        generation: int,
        state: str,
        status: str,
        error: str = "",
        required_state: str | None = None,
    ) -> bool:
        """Update state only if the reporting recording session is still current."""
        with self._lock:
            if generation != self._session_generation:
                return False
            if required_state is not None and self.state != required_state:
                return False
            self.state = state
            self.status = status
            self.error = error
        self.events.put(("status", status))
        log_debug("state", f"state: {state}\nstatus: {status}")
        if error:
            log_error("state", error)
        return True

    def _set_status_for_session(self, generation: int, status: str) -> bool:
        """Report live progress without changing the recorder control state."""
        with self._lock:
            if generation != self._session_generation:
                return False
            if self.state not in {"STARTING", "RECORDING", "STOPPING", "PROCESSING"}:
                return False
            self.status = status
        self.events.put(("status", status))
        log_debug("realtime_status", status)
        return True

    def prepare_models(self) -> None:
        """Prepare models immediately without blocking the Tk event loop."""
        with self._lock:
            if self.model_thread and self.model_thread.is_alive():
                return
            if self.state not in {"READY", "ERROR"}:
                return
            stt_model = self.stt_model
            precision = self.precision
            self.state = "DOWNLOADING"
            self.status = f"{get_stt_model(stt_model).status_name} 모델을 확인하고 있습니다..."
            self.error = ""
            model_thread = threading.Thread(
                target=self._prepare_models,
                args=(stt_model, precision),
                daemon=True,
            )
            self.model_thread = model_thread
        self.events.put(("status", self.status))
        log_debug("state", f"state: DOWNLOADING\nstatus: {self.status}")
        try:
            model_thread.start()
        except Exception as exc:
            log_error("prepare_models_start", exc)
            with self._lock:
                if self.model_thread is model_thread:
                    self.model_thread = None
            self._set_state(
                "ERROR",
                f"음성인식 모델 준비 시작 실패: {exc}",
                traceback.format_exc(),
            )

    def _prepare_models(self, stt_model: str, precision: str) -> None:
        try:
            models = ensure_models(
                app_dir(),
                resource_dir(),
                stt_model,
                precision,
                progress=lambda text: self._set_state("DOWNLOADING", text),
            )
            with self._lock:
                if (
                    stt_model != self.stt_model
                    or precision != self.precision
                    or self.state != "DOWNLOADING"
                ):
                    return
                self.prepared_models = models
                self.prepared_models_model = stt_model
                self.prepared_models_precision = precision
            self._set_state("READY", "녹음 준비")
        except Exception as exc:
            log_error("prepare_models", exc)
            with self._lock:
                current = (
                    stt_model == self.stt_model
                    and precision == self.precision
                    and self.state == "DOWNLOADING"
                )
            if current:
                self._set_state(
                    "ERROR",
                    f"음성인식 모델 준비 실패: {exc}",
                    traceback.format_exc(),
                )

    def _models_for_processing(
        self,
        stt_model: str | None = None,
        precision: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Path]:
        with self._lock:
            stt_model = normalize_stt_model(stt_model or self.stt_model)
            precision = effective_stt_precision(stt_model, precision or self.precision)
        with self._lock:
            if (
                self.prepared_models_model == stt_model
                and self.prepared_models_precision == precision
                and self.prepared_models
            ):
                if model_paths_are_valid(self.prepared_models):
                    return dict(self.prepared_models)
        models = ensure_models(
            app_dir(),
            resource_dir(),
            stt_model,
            precision,
            progress=progress or (lambda text: self._set_state("DOWNLOADING", text)),
        )
        with self._lock:
            self.prepared_models = models
            self.prepared_models_model = stt_model
            self.prepared_models_precision = precision
        return models

    def start(self, *, realtime_translation: bool = False) -> None:
        """Reserve a session immediately, then prepare live capture off the UI thread."""
        with self._lock:
            if self.state not in {"READY", "ERROR"}:
                return
            if self.start_thread and self.start_thread.is_alive():
                self.status = "이전 녹음 준비가 아직 진행 중입니다. 잠시 후 다시 시도해 주세요."
                self.events.put(("status", self.status))
                return
            if self.record_thread and self.record_thread.is_alive():
                self.status = "이전 녹음 장치가 아직 종료 중입니다. 잠시 후 다시 시도해 주세요."
                self.events.put(("status", self.status))
                return
            if self.realtime_session is not None:
                if self.realtime_session.has_active_workers():
                    self.status = "이전 실시간 번역 작업이 종료 중입니다. 잠시 후 다시 시도해 주세요."
                    self.events.put(("status", self.status))
                    return
                self.realtime_session = None
            # Reserve the state before allocating paths so concurrent HTTP
            # requests cannot begin a second recording session.
            self.state = "STARTING"
            self.status = "녹음 준비 중입니다…"
            self.error = ""
            self._session_generation += 1
            generation = self._session_generation
            session_options = (
                self.stt_model,
                self.precision,
                self.preset,
                self.script_url,
                self.hytrans_url,
                bool(realtime_translation),
            )
            start_thread = threading.Thread(
                target=self._start_recording,
                args=(generation, session_options),
                daemon=True,
            )
            self.start_thread = start_thread
        self.events.put(("status", self.status))
        log_debug("state", f"state: STARTING\nstatus: {self.status}")
        try:
            start_thread.start()
        except Exception as exc:
            log_error("start_recording_thread", exc)
            with self._lock:
                if self.start_thread is start_thread:
                    self.start_thread = None
            self._set_state_for_session(
                generation,
                "ERROR",
                f"녹음 시작 실패: {exc}",
                traceback.format_exc(),
            )

    def _start_recording(
        self,
        generation: int,
        session_options: tuple[str, str, str, str, str, bool],
    ) -> None:
        session_work_dir: Path | None = None
        realtime_session: RealtimeTranslationSession | None = None
        try:
            session_root = work_dir()
            if shutil.disk_usage(session_root).free < MIN_RECORDING_FREE_BYTES:
                raise RuntimeError("녹음을 시작하려면 작업 드라이브에 2GB 이상의 여유 공간이 필요합니다.")
            now = dt.datetime.now()
            session_id = f"{now:%Y%m%d-%H%M%S-%f}"
            session_work_dir = session_root / session_id
            session_work_dir.mkdir(parents=True, exist_ok=True)
            wav_path = session_work_dir / "capture.wav"
            stop_event = threading.Event()
            if realtime_translation:
                stt_model, precision, preset, script_url, hytrans_url, _ = session_options
                self._set_status_for_session(generation, "실시간 음성 번역을 준비하고 있습니다…")
                models = self._models_for_processing(
                    stt_model,
                    precision,
                    progress=lambda text: self._set_status_for_session(generation, text),
                )
                realtime_session = RealtimeTranslationSession(
                    models,
                    stt_model,
                    precision,
                    preset,
                    script_url,
                    hytrans_url,
                    session_id,
                    report_status=lambda text: self._set_status_for_session(generation, text),
                )
                # Keep the session reachable before starting native workers.
                # An initialization failure can then cancel/reap it safely and
                # prevent a new recording from overlapping old workers.
                with self._lock:
                    if generation != self._session_generation or self.state != "STARTING":
                        cleanup_work_files(session_work_dir)
                        return
                    self.realtime_session = realtime_session
                # VAD initialization happens before the loopback opens.  The
                # expensive recognizer itself stays on the STT worker.
                realtime_session.start()
            record_thread = threading.Thread(
                target=self._record_loop,
                args=(generation, stop_event, wav_path, session_work_dir, realtime_session),
                daemon=True,
            )
            with self._lock:
                if generation != self._session_generation or self.state != "STARTING":
                    if realtime_session is not None:
                        realtime_session.abort()
                    cleanup_work_files(session_work_dir)
                    return
                self.session_id = session_id
                self.session_work_dir = session_work_dir
                self.wav_path = wav_path
                self.session_options = session_options
                self.stop_event = stop_event
                self.record_thread = record_thread
                self.realtime_session = realtime_session
            record_thread.start()
            self._set_state_for_session(
                generation,
                "RECORDING",
                (
                    "컴퓨터 소리를 녹음하고 있습니다… (실시간 음성 번역 활성화)"
                    if realtime_translation
                    else "컴퓨터 소리를 녹음하고 있습니다…"
                ),
                required_state="STARTING",
            )
        except Exception as exc:
            log_error("start_recording", exc)
            if realtime_session is not None:
                realtime_session.abort()
                with self._lock:
                    if generation == self._session_generation:
                        self.realtime_session = realtime_session
            if session_work_dir is not None:
                cleanup_work_files(session_work_dir)
            self._set_state_for_session(
                generation,
                "ERROR",
                f"녹음 시작 실패: {exc}",
                traceback.format_exc(),
            )
        finally:
            with self._lock:
                if self.start_thread is threading.current_thread():
                    self.start_thread = None

    def stop(self) -> None:
        with self._lock:
            if self.state != "RECORDING":
                return
            self.state = "STOPPING"
            generation = self._session_generation
            stop_event = self.stop_event
            record_thread = self.record_thread
            wav_path = self.wav_path
            session_work_dir = self.session_work_dir
            session_id = self.session_id
            session_options = self.session_options
            realtime_session = self.realtime_session
        self._set_state_for_session(generation, "STOPPING", "녹음을 마무리하고 있습니다…")
        stop_event.set()
        process_thread = threading.Thread(
            target=self._finish_and_process,
            args=(
                generation,
                record_thread,
                wav_path,
                session_work_dir,
                session_id,
                session_options,
                realtime_session,
            ),
            daemon=True,
        )
        self.process_thread = process_thread
        process_thread.start()

    def _reap_unresponsive_recording(
        self,
        generation: int,
        record_thread: threading.Thread,
        session_work_dir: Path | None,
        realtime_session: RealtimeTranslationSession | None,
    ) -> None:
        """Clean up only after a recorder that missed its stop deadline exits."""

        def reap() -> None:
            record_thread.join()
            if realtime_session is not None:
                realtime_session.abort()
                realtime_session.wait_for_completion()
            if session_work_dir is not None:
                cleanup_work_files(session_work_dir)
            with self._lock:
                if (
                    generation == self._session_generation
                    and self.realtime_session is realtime_session
                ):
                    self.realtime_session = None

        threading.Thread(target=reap, daemon=True).start()

    def _record_loop(
        self,
        generation: int,
        stop_event: threading.Event,
        wav_path: Path,
        session_work_dir: Path,
        realtime_session: RealtimeTranslationSession | None = None,
    ) -> None:
        stop_reason = ""
        try:
            speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("기본 출력 장치를 찾을 수 없습니다.")
            loopback = sc.get_microphone(speaker.id, include_loopback=True)
            if loopback is None:
                raise RuntimeError("기본 출력 장치의 WASAPI loopback을 열 수 없습니다.")
            chunk_frames = CAPTURE_SAMPLE_RATE // 10
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(CAPTURE_SAMPLE_RATE)
                with loopback.recorder(
                    samplerate=CAPTURE_SAMPLE_RATE,
                    channels=2,
                    blocksize=chunk_frames,
                ) as recorder:
                    recorded_frames = 0
                    while not stop_event.is_set():
                        block = recorder.record(numframes=chunk_frames)
                        pcm = np.clip(block, -1.0, 1.0)
                        output.writeframes((pcm * 32767.0).astype("<i2").tobytes())
                        if realtime_session is not None:
                            # The recorder owns VAD; only finalized chunks are
                            # handed off to the STT worker.
                            realtime_session.accept_capture_block(block)
                        recorded_frames += len(block)
                        if recorded_frames >= MAX_RECORDING_SECONDS * CAPTURE_SAMPLE_RATE:
                            stop_reason = "최대 녹음 시간 4시간에 도달해 자동으로 종료합니다."
                            stop_event.set()
                            break
                        if recorded_frames % (CAPTURE_SAMPLE_RATE * 10) < chunk_frames:
                            estimated_raw_bytes = int(recorded_frames * 4 / 3)
                            required_free = max(
                                MIN_RECORDING_FREE_BYTES,
                                estimated_raw_bytes + 512 * 1024 * 1024,
                            )
                            if shutil.disk_usage(session_work_dir).free < required_free:
                                stop_reason = "작업 드라이브의 여유 공간이 부족해 녹음을 자동으로 종료합니다."
                                stop_event.set()
                                break
            if realtime_session is not None:
                # Keep native VAD ownership on this recorder thread through
                # the final padded window and flush.
                realtime_session.finish_input()
            if stop_reason and self._set_state_for_session(
                generation,
                "RECORDING",
                stop_reason,
                required_state="RECORDING",
            ):
                self.stop()
        except Exception as exc:
            log_error("record", exc)
            if realtime_session is not None:
                realtime_session.abort()
            self._set_state_for_session(
                generation,
                "ERROR",
                f"녹음 실패: {exc}",
                traceback.format_exc(),
            )
            stop_event.set()
            cleanup_work_files(session_work_dir)

    def _finish_and_process(
        self,
        generation: int | None = None,
        record_thread: threading.Thread | None = None,
        wav_path: Path | None = None,
        session_work_dir: Path | None = None,
        session_id: str | None = None,
        session_options: tuple[str, str, str, str, str, bool] | None = None,
        realtime_session: RealtimeTranslationSession | None = None,
    ) -> None:
        # Optional arguments keep direct diagnostic/unit-test calls convenient;
        # normal recordings always pass an immutable session snapshot.
        generation = self._session_generation if generation is None else generation
        record_thread = self.record_thread if record_thread is None else record_thread
        wav_path = self.wav_path if wav_path is None else wav_path
        session_work_dir = self.session_work_dir if session_work_dir is None else session_work_dir
        session_id = self.session_id if session_id is None else session_id
        session_options = self.session_options if session_options is None else session_options
        realtime_session = self.realtime_session if realtime_session is None else realtime_session
        if len(session_options) == 5:
            stt_model, precision, preset, script_url, hytrans_url = session_options
            realtime_translation = False
        else:
            stt_model, precision, preset, script_url, hytrans_url, realtime_translation = session_options
        if record_thread:
            record_thread.join(timeout=5)
            if record_thread.is_alive():
                if realtime_session is not None:
                    realtime_session.abort()
                self._reap_unresponsive_recording(
                    generation,
                    record_thread,
                    session_work_dir,
                    realtime_session,
                )
                self._set_state_for_session(generation, "ERROR", "Recording did not stop in time.")
                return
        with self._lock:
            is_current = generation == self._session_generation
            current_state = self.state
        if not is_current:
            if session_work_dir is not None:
                cleanup_work_files(session_work_dir)
            return
        if current_state == "ERROR":
            if session_work_dir is not None:
                cleanup_work_files(session_work_dir)
            return
        final_state = "READY"
        final_status = "완료"
        final_error = ""
        audio = None
        try:
            if realtime_translation:
                if realtime_session is None:
                    raise RuntimeError("실시간 음성 번역 세션을 찾을 수 없습니다.")
                self._set_state_for_session(
                    generation,
                    "PROCESSING",
                    "실시간 음성 번역의 남은 발화를 처리하고 있습니다…",
                )
                summary = realtime_session.wait_for_completion()
                if summary.fatal_error:
                    raise RuntimeError(f"실시간 음성 번역 처리 실패: {summary.fatal_error}")
                if summary.recognized:
                    final_status = (
                        f"완료: 실시간으로 일본어 {summary.recognized}개를 인식하고 번역했습니다."
                    )
                    if summary.translation_failures:
                        final_status += f" 번역 실패/건너뜀 {summary.translation_failures}건."
                    if summary.delivery_failures:
                        final_status += (
                            f" 대본 전달 실패 {summary.delivery_failures}건은 로그를 확인해 주세요."
                        )
                    if summary.dropped_segments:
                        final_status += f" 처리 지연으로 건너뛴 발화 {summary.dropped_segments}건."
                else:
                    final_status = "완료: 실시간으로 인식된 일본어 음성이 없습니다."
                return
            assert wav_path is not None
            processing_dir = session_work_dir or work_dir()
            raw_path = processing_dir / "capture-16k.f32"
            self._set_state_for_session(generation, "PROCESSING", "음성을 16 kHz mono로 변환하고 있습니다…")
            audio = wav_to_mono_16k(wav_path, raw_path)
            if audio.size == 0:
                final_status = "완료: 녹음된 오디오가 없습니다."
                return
            models = self._models_for_processing(
                stt_model,
                precision,
                progress=lambda text: self._set_state_for_session(
                    generation,
                    "DOWNLOADING",
                    text,
                ),
            )
            self._set_state_for_session(generation, "PROCESSING", f"VAD로 음성 구간을 찾고 있습니다 ({preset})…")
            intervals = collect_vad_intervals(audio, models["vad"], preset)
            segments = build_segments(audio, intervals, preset)
            if not segments:
                final_status = "완료: 인식할 음성이 없습니다."
                return
            self._set_state_for_session(generation, "PROCESSING", f"일본어 음성을 인식하고 있습니다 (0/{len(segments)})…")
            recognizer = create_recognizer(models, model_key=stt_model)
            count = 0
            delivery_failures = 0

            def deliver(stage: str, action) -> bool:
                for attempt in range(2):
                    try:
                        action()
                        return True
                    except Exception as exc:
                        log_error(stage, exc)
                        if attempt == 0:
                            time.sleep(0.15)
                return False

            def publish(result) -> None:
                nonlocal count, delivery_failures
                entry_id = f"{session_id}-{result.segment_id}"
                if not deliver(
                    "script_append",
                    lambda: append_script_text(script_url, result, entry_id=entry_id),
                ):
                    delivery_failures += 1
                count += 1
                self._set_state_for_session(generation, "PROCESSING", f"일본어 음성을 인식하고 있습니다 ({count}/{len(segments)})…")

            results = recognize_segments(recognizer, segments, on_result=publish)
            translation_deadline = time.monotonic() + MAX_TRANSLATION_SESSION_SECONDS
            consecutive_translation_failures = 0
            translation_failures = 0
            for index, result in enumerate(results, 1):
                self._set_state_for_session(generation, "TRANSLATING", f"번역하고 있습니다 ({index}/{len(results)})…")
                if time.monotonic() >= translation_deadline:
                    translated = "[번역 건너뜀] 이번 녹음의 전체 번역 제한 시간(30분)을 초과했습니다."
                    translation_failures += 1
                elif consecutive_translation_failures >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    translated = "[번역 건너뜀] HYTrans가 연속으로 응답하지 않아 나머지 요청을 중단했습니다."
                    translation_failures += 1
                else:
                    try:
                        translated = translate_text(
                            hytrans_url,
                            result.text_ja,
                            timeout=AUDIO_TRANSLATION_TIMEOUT_SECONDS,
                        )
                        if not translated:
                            raise RuntimeError("HYTrans가 빈 번역 결과를 반환했습니다.")
                        consecutive_translation_failures = 0
                    except Exception as exc:
                        log_error("translate", exc)
                        translated = f"[번역 실패] {exc}"
                        consecutive_translation_failures += 1
                        translation_failures += 1
                entry_id = f"{session_id}-{result.segment_id}"
                if not deliver(
                    "script_translation",
                    lambda: set_script_translation(
                        script_url,
                        result,
                        translated,
                        entry_id=entry_id,
                    ),
                ):
                    delivery_failures += 1
            if results:
                final_status = f"완료: 일본어 {len(results)}개를 인식하고 번역했습니다."
                if translation_failures:
                    final_status += f" 번역 실패/건너뜀 {translation_failures}건."
                if delivery_failures:
                    final_status += f" 대본 전달 실패 {delivery_failures}건은 로그를 확인해 주세요."
            else:
                final_status = "완료: 인식된 일본어 음성이 없습니다."
        except Exception as exc:
            log_error("process_audio", exc)
            final_state = "ERROR"
            if "invalid unordered_map" in str(exc):
                final_status = "처리 실패: 음성 모델의 토큰 사전이 손상되었거나 모델과 맞지 않습니다. 모델을 다시 받아 주세요."
            else:
                final_status = f"처리 실패: {exc}"
            final_error = traceback.format_exc()
        finally:
            if isinstance(audio, np.memmap):
                try:
                    audio._mmap.close()
                except Exception:
                    pass
            if session_work_dir is not None:
                cleanup_work_files(session_work_dir)
            with self._lock:
                if generation == self._session_generation and self.realtime_session is realtime_session:
                    self.realtime_session = None
            self._set_state_for_session(generation, final_state, final_status, final_error)


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


def make_handler(controller: CaptureController):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == "/health":
                _write_json(self, 200, controller.health())
            else:
                _write_json(self, 404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            try:
                if self.path == "/config":
                    controller.configure(_read_json(self))
                elif self.path == "/start":
                    controller.start()
                elif self.path == "/stop":
                    controller.stop()
                else:
                    _write_json(self, 404, {"ok": False, "error": "not found"})
                    return
                _write_json(self, 200, controller.health())
            except Exception as exc:
                log_error("http_request", exc)
                _write_json(self, 409, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


class CaptureWindow:
    """The intentionally small, local controller for a capture session.

    Unlike the MekiCopy settings window, this dialog does not expose saved
    audio settings.  Its optional live translation switch only applies to the
    next recording and is reset as soon as that recording reaches a terminal
    state.
    """

    def __init__(self, root: tk.Tk, controller: CaptureController) -> None:
        self.root = root
        self.controller = controller
        self._last_state = controller.state
        root.title("MekiAudioCapture")
        root.geometry("480x350")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        configure_window_theme(root)

        body = tk.Frame(root, bg=BG, padx=18, pady=16)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            body,
            text="MekiAudioCapture",
            bg=BG,
            fg=ROSE,
            font=TITLE_FONT,
            anchor=tk.W,
        ).pack(fill=tk.X)
        tk.Label(
            body,
            text="시스템 소리를 녹음해 음성 인식 결과를 MekiScript에 보냅니다.",
            bg=BG,
            fg=MUTED,
            font=DEFAULT_FONT,
            anchor=tk.W,
        ).pack(fill=tk.X, pady=(2, 12))

        status_card = tk.Frame(
            body,
            bg=SURFACE,
            padx=12,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        status_card.pack(fill=tk.X)
        tk.Label(
            status_card,
            text="현재 상태",
            bg=SURFACE,
            fg=MUTED,
            font=DEFAULT_FONT,
            anchor=tk.W,
        ).pack(fill=tk.X)
        self.status = tk.Label(
            status_card,
            text=controller.status,
            wraplength=410,
            justify=tk.LEFT,
            bg=SURFACE,
            fg=INK,
            font=DEFAULT_FONT,
            anchor=tk.W,
        )
        self.status.pack(fill=tk.X, pady=(3, 0))

        options = tk.LabelFrame(
            body,
            text="녹음 옵션",
            bg=SURFACE,
            fg=ROSE,
            font=BUTTON_FONT,
            padx=10,
            pady=8,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        options.pack(fill=tk.X, pady=(12, 0))
        # This variable is intentionally local to this window.  It is neither
        # accepted from MekiCopy nor written to any settings file.
        self.realtime_translation_var = tk.BooleanVar(value=False)
        self.realtime_translation_check = tk.Checkbutton(
            options,
            text="실시간 음성 번역(저사양에서 비권장)",
            variable=self.realtime_translation_var,
            bg=SOFT,
            fg=INK,
            activebackground=SOFT,
            activeforeground=ROSE,
            selectcolor=SURFACE,
            font=DEFAULT_FONT,
            anchor="w",
            command=self._on_realtime_translation_changed,
        )
        self.realtime_translation_check.pack(fill=tk.X, pady=(0, 4))
        tk.Label(
            options,
            text="선택은 저장되지 않으며, 녹음이 끝나면 자동으로 해제됩니다.",
            bg=SURFACE,
            fg=MUTED,
            font=DEFAULT_FONT,
            justify=tk.LEFT,
            anchor=tk.W,
        ).pack(fill=tk.X)

        actions = tk.Frame(body, bg=BG)
        actions.pack(fill=tk.X, pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.start_button = RoundedButton(
            actions,
            text="녹음 시작",
            command=self.start,
            variant="primary",
            width=1,
            height=40,
            radius=18,
        )
        self.start_button.grid(row=0, column=0, sticky=tk.EW, padx=(0, 5))
        self.stop_button = RoundedButton(
            actions,
            text="녹음 종료",
            command=controller.stop,
            width=1,
            height=40,
            radius=18,
        )
        self.stop_button.grid(row=0, column=1, sticky=tk.EW, padx=(5, 0))

        root.after(100, self.poll)

    def _on_realtime_translation_changed(self) -> None:
        if not self.realtime_translation_var.get():
            return
        messagebox.showwarning(
            "MekiAudioCapture",
            "실시간 음성 번역은 음성 인식과 번역을 동시에 처리합니다.\n\n"
            "저사양 컴퓨터에서는 번역이 느리거나 실패할 수 있어 권장하지 않습니다.",
            parent=self.root,
        )

    def start(self) -> None:
        self.controller.start(realtime_translation=bool(self.realtime_translation_var.get()))

    def close(self) -> None:
        if self.controller.state not in {"READY", "ERROR"}:
            messagebox.showwarning(
                "MekiAudioCapture",
                "녹음과 후처리가 끝난 뒤 창을 닫아주세요.",
                parent=self.root,
            )
            return
        self.root.destroy()

    def poll(self) -> None:
        while True:
            try:
                _, text = self.controller.events.get_nowait()
                self.status.configure(text=text)
            except queue.Empty:
                break
        state = self.controller.state
        self.start_button.configure(state=tk.NORMAL if state in {"READY", "ERROR"} else tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL if state == "RECORDING" else tk.DISABLED)
        self.realtime_translation_check.configure(
            state=tk.NORMAL if state in {"READY", "ERROR"} else tk.DISABLED
        )
        self.status.configure(fg=ROSE if state == "ERROR" else INK)
        if state in {"READY", "ERROR"} and self._last_state not in {"READY", "ERROR"}:
            # Choosing it applies to one recording only.  A later session must
            # be explicitly opted into again.
            self.realtime_translation_var.set(False)
        self._last_state = state
        self.root.after(100, self.poll)


def _load_model_test_wav(wav_path: Path) -> np.ndarray:
    """Load a model-provided PCM16 WAV as 16 kHz mono for a native smoke test."""
    with wave.open(str(wav_path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        if source.getcomptype() != "NONE" or sample_width != 2 or channels < 1:
            raise RuntimeError("음성인식 테스트 WAV는 PCM16 형식이어야 합니다.")
        if sample_rate <= 0 or sample_rate % INTERNAL_SAMPLE_RATE:
            raise RuntimeError(f"지원하지 않는 음성인식 테스트 WAV 샘플레이트입니다: {sample_rate}")
        raw = source.readframes(source.getnframes())
    frame_width = sample_width * channels
    if not raw or len(raw) % frame_width:
        raise RuntimeError("음성인식 테스트 WAV 데이터가 올바르지 않습니다.")
    samples = np.frombuffer(raw, dtype="<i2").reshape(-1, channels).astype(np.float32)
    samples *= 1.0 / 32768.0
    ratio = sample_rate // INTERNAL_SAMPLE_RATE
    usable = (len(samples) // ratio) * ratio
    if not usable:
        raise RuntimeError("음성인식 테스트 WAV가 너무 짧습니다.")
    return samples[:usable].mean(axis=1).reshape(-1, ratio).mean(axis=1)


def run_capture_window_self_test() -> None:
    """Exercise the packaged CaptureWindow and its shared MekiCopy theme."""
    root = tk.Tk()
    root.withdraw()
    try:
        controller = CaptureController(
            "int8",
            "BALANCED",
            DEFAULT_SCRIPT_URL,
            DEFAULT_HYTRANS_URL,
            prepare_models_on_start=False,
        )
        window = CaptureWindow(root, controller)
        # Grid-weighted Canvas controls receive their final width only after
        # Tk maps the window at least once.
        root.deiconify()
        root.update_idletasks()
        root.update()
        if root.winfo_width() != 480 or root.winfo_height() != 350:
            raise RuntimeError("MekiAudioCapture 창 레이아웃 크기가 올바르지 않습니다.")
        if window.realtime_translation_var.get():
            raise RuntimeError("실시간 음성 번역 옵션의 기본값은 꺼짐이어야 합니다.")
        if min(window.start_button.winfo_width(), window.stop_button.winfo_width()) < 150:
            raise RuntimeError("MekiAudioCapture 작업 버튼 레이아웃이 올바르지 않습니다.")
    finally:
        root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiAudioCapture")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--stt-model", choices=tuple(STT_MODELS), default=DEFAULT_STT_MODEL)
    parser.add_argument("--precision", choices=("fp32", "int8"), default="int8")
    parser.add_argument("--preset", choices=("FAST", "BALANCED", "LONG"), default="BALANCED")
    parser.add_argument("--script-url", default=DEFAULT_SCRIPT_URL)
    parser.add_argument("--hytrans-url", default=DEFAULT_HYTRANS_URL)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-models", action="store_true")
    parser.add_argument("--self-test-ui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test-server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug-log", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_system_logging("MekiAudioCapture", args.debug_log)
    install_exception_hooks()
    prepare_streams()
    if args.self_test:
        assert DEFAULT_STT_MODEL == "parakeet"
        assert args.stt_model in STT_MODELS
        assert normalize_stt_model(args.stt_model) == args.stt_model
        default_model = STT_MODELS[DEFAULT_STT_MODEL]
        assert default_model.architecture == "nemo_ctc"
        assert default_model.required_files == ("tokens.txt", "model.int8.onnx")
        assert normalize_precision(args.precision) in {"fp32", "int8"}
        if args.stt_model == DEFAULT_STT_MODEL:
            assert effective_stt_precision(args.stt_model, args.precision) == "int8"
            import sherpa_onnx

            if not callable(getattr(sherpa_onnx.OfflineRecognizer, "from_nemo_ctc", None)):
                raise RuntimeError("sherpa-onnx NeMo CTC recognizer factory를 찾을 수 없습니다.")
        assert normalize_preset(args.preset) in {"FAST", "BALANCED", "LONG"}
        work_dir()
        model_roots = model_root_candidates(app_dir(), resource_dir())
        if not model_roots or not any(root.name == "models" for root in model_roots):
            raise RuntimeError("모델 경로가 MekiAudioCapture/models가 아닙니다.")
        return 0
    if args.self_test_models:
        models = resolve_models(
            app_dir(),
            resource_dir(),
            args.stt_model,
            args.precision,
        )
        source_test_wav = models["tokens"].parent / "test.wav"
        collect_vad_intervals(np.zeros(16_000, dtype=np.float32), models["vad"], args.preset)
        recognizer = create_recognizer(models, model_key=args.stt_model, num_threads=1)
        if source_test_wav.is_file():
            samples = _load_model_test_wav(source_test_wav)
            require_text = True
        else:
            # Full carries only runtime model files, not upstream sample WAVs.
            # Still execute one decode to validate the frozen model factory.
            samples = np.zeros(INTERNAL_SAMPLE_RATE, dtype=np.float32)
            require_text = False
        stream = recognizer.create_stream()
        stream.accept_waveform(INTERNAL_SAMPLE_RATE, samples)
        recognizer.decode_stream(stream)
        if require_text and not str(stream.result.text).strip():
            raise RuntimeError(
                f"{get_stt_model(args.stt_model).status_name} 테스트 결과가 비어 있습니다."
            )
        return 0
    if args.self_test_ui:
        run_capture_window_self_test()
        return 0
    controller = CaptureController(
        args.precision,
        args.preset,
        args.script_url,
        args.hytrans_url,
        stt_model=args.stt_model,
        prepare_models_on_start=not args.self_test_server,
    )
    try:
        set_windows_app_id("MekiAudioCapture")
        root = tk.Tk()
        install_tk_exception_hook(root)
        apply_tk_icon(root)
        CaptureWindow(root, controller)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(controller))
        server.daemon_threads = True
        server.block_on_close = False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        root.mainloop()
        server.shutdown()
        server.server_close()
        return 0
    except Exception as exc:
        log_error("main", exc)
        try:
            messagebox.showerror("MekiAudioCapture", str(exc))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
