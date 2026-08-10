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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import numpy as np
import soundcard as sc

from app_identity import apply_tk_icon, set_windows_app_id
from audio_capture_core import (
    CAPTURE_SAMPLE_RATE,
    append_script_text,
    build_segments,
    cleanup_work_files,
    collect_vad_intervals,
    create_recognizer,
    ensure_models,
    model_paths_are_valid,
    model_root_candidates,
    normalize_precision,
    normalize_preset,
    recognize_segments,
    resolve_models,
    set_script_translation,
    translate_text,
    wav_to_mono_16k,
)
from runtime_paths import prepare_tk_environment, writable_app_subdir
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


class CaptureController:
    def __init__(
        self,
        precision: str,
        preset: str,
        script_url: str,
        hytrans_url: str,
        *,
        prepare_models_on_start: bool = True,
    ) -> None:
        self.precision = normalize_precision(precision)
        self.preset = normalize_preset(preset)
        self.script_url = script_url.rstrip("/")
        self.hytrans_url = hytrans_url.rstrip("/")
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stop_event = threading.Event()
        self.record_thread: threading.Thread | None = None
        self.process_thread: threading.Thread | None = None
        self.model_thread: threading.Thread | None = None
        self.prepared_models: dict[str, Path] | None = None
        self.prepared_models_precision = ""
        self.wav_path: Path | None = None
        self.session_work_dir: Path | None = None
        self.session_id = ""
        self.session_options = (
            self.precision,
            self.preset,
            self.script_url,
            self.hytrans_url,
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
                "precision": self.precision,
                "preset": self.preset,
                "error": self.error or None,
            }

    def configure(self, payload: dict[str, Any]) -> None:
        with self._lock:
            if self.state not in {"READY", "ERROR"}:
                raise RuntimeError("녹음 또는 처리 중에는 설정을 바꿀 수 없습니다.")
            previous_precision = self.precision
            self.precision = normalize_precision(str(payload.get("precision", self.precision)))
            self.preset = normalize_preset(str(payload.get("preset", self.preset)))
            self.script_url = str(payload.get("scriptUrl", self.script_url)).rstrip("/")
            self.hytrans_url = str(payload.get("hytransUrl", self.hytrans_url)).rstrip("/")
            precision_changed = self.precision != previous_precision
            if precision_changed:
                self.prepared_models = None
                self.prepared_models_precision = ""
        if "debugLog" in payload:
            set_debug_enabled(bool(payload["debugLog"]))
        if precision_changed:
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

    def prepare_models(self) -> None:
        """Prepare models immediately without blocking the Tk event loop."""
        with self._lock:
            if self.model_thread and self.model_thread.is_alive():
                return
            if self.state not in {"READY", "ERROR"}:
                return
            precision = self.precision
            self.state = "DOWNLOADING"
            self.status = "음성인식 모델을 확인하고 있습니다..."
            self.error = ""
            model_thread = threading.Thread(
                target=self._prepare_models,
                args=(precision,),
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

    def _prepare_models(self, precision: str) -> None:
        try:
            models = ensure_models(
                app_dir(),
                resource_dir(),
                precision,
                progress=lambda text: self._set_state("DOWNLOADING", text),
            )
            with self._lock:
                if precision != self.precision or self.state != "DOWNLOADING":
                    return
                self.prepared_models = models
                self.prepared_models_precision = precision
            self._set_state("READY", "녹음 준비")
        except Exception as exc:
            log_error("prepare_models", exc)
            with self._lock:
                current = precision == self.precision and self.state == "DOWNLOADING"
            if current:
                self._set_state(
                    "ERROR",
                    f"음성인식 모델 준비 실패: {exc}",
                    traceback.format_exc(),
                )

    def _models_for_processing(
        self,
        precision: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Path]:
        with self._lock:
            precision = normalize_precision(precision or self.precision)
        with self._lock:
            if self.prepared_models_precision == precision and self.prepared_models:
                if model_paths_are_valid(self.prepared_models):
                    return dict(self.prepared_models)
        models = ensure_models(
            app_dir(),
            resource_dir(),
            precision,
            progress=progress or (lambda text: self._set_state("DOWNLOADING", text)),
        )
        with self._lock:
            self.prepared_models = models
            self.prepared_models_precision = precision
        return models

    def start(self) -> None:
        with self._lock:
            if self.state not in {"READY", "ERROR"}:
                return
            if self.record_thread and self.record_thread.is_alive():
                self.status = "이전 녹음 장치가 아직 종료 중입니다. 잠시 후 다시 시도해 주세요."
                self.events.put(("status", self.status))
                return
            # Reserve the state before allocating paths so concurrent HTTP
            # requests cannot begin a second recording session.
            self.state = "STARTING"
            self._session_generation += 1
            generation = self._session_generation
            session_options = (
                self.precision,
                self.preset,
                self.script_url,
                self.hytrans_url,
            )
        session_work_dir: Path | None = None
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
            record_thread = threading.Thread(
                target=self._record_loop,
                args=(generation, stop_event, wav_path, session_work_dir),
                daemon=True,
            )
            with self._lock:
                if generation != self._session_generation:
                    cleanup_work_files(session_work_dir)
                    return
                self.session_id = session_id
                self.session_work_dir = session_work_dir
                self.wav_path = wav_path
                self.session_options = session_options
                self.stop_event = stop_event
                self.record_thread = record_thread
            record_thread.start()
            self._set_state_for_session(
                generation,
                "RECORDING",
                "컴퓨터 소리를 녹음하고 있습니다…",
                required_state="STARTING",
            )
        except Exception as exc:
            log_error("start_recording", exc)
            if session_work_dir is not None:
                cleanup_work_files(session_work_dir)
            self._set_state_for_session(
                generation,
                "ERROR",
                f"녹음 시작 실패: {exc}",
                traceback.format_exc(),
            )

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
            ),
            daemon=True,
        )
        self.process_thread = process_thread
        process_thread.start()

    def _record_loop(
        self,
        generation: int,
        stop_event: threading.Event,
        wav_path: Path,
        session_work_dir: Path,
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
            if stop_reason and self._set_state_for_session(
                generation,
                "RECORDING",
                stop_reason,
                required_state="RECORDING",
            ):
                self.stop()
        except Exception as exc:
            log_error("record", exc)
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
        session_options: tuple[str, str, str, str] | None = None,
    ) -> None:
        # Optional arguments keep direct diagnostic/unit-test calls convenient;
        # normal recordings always pass an immutable session snapshot.
        generation = self._session_generation if generation is None else generation
        record_thread = self.record_thread if record_thread is None else record_thread
        wav_path = self.wav_path if wav_path is None else wav_path
        session_work_dir = self.session_work_dir if session_work_dir is None else session_work_dir
        session_id = self.session_id if session_id is None else session_id
        session_options = self.session_options if session_options is None else session_options
        precision, preset, script_url, hytrans_url = session_options
        if record_thread:
            record_thread.join(timeout=5)
            if record_thread.is_alive():
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
            assert wav_path is not None
            processing_dir = session_work_dir or work_dir()
            raw_path = processing_dir / "capture-16k.f32"
            self._set_state_for_session(generation, "PROCESSING", "음성을 16 kHz mono로 변환하고 있습니다…")
            audio = wav_to_mono_16k(wav_path, raw_path)
            if audio.size == 0:
                final_status = "완료: 녹음된 오디오가 없습니다."
                return
            models = self._models_for_processing(
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
            recognizer = create_recognizer(models)
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
    def __init__(self, root: tk.Tk, controller: CaptureController) -> None:
        self.root = root
        self.controller = controller
        root.title("MekiAudioCapture")
        root.geometry("430x220")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        body = tk.Frame(root, padx=18, pady=18)
        body.pack(fill=tk.BOTH, expand=True)
        self.status = tk.Label(body, text=controller.status, wraplength=390, justify="center")
        self.status.pack(fill=tk.X, pady=(4, 18))
        self.start_button = tk.Button(body, text="녹음 시작", height=2, command=controller.start)
        self.start_button.pack(fill=tk.X, pady=4)
        self.stop_button = tk.Button(body, text="녹음 종료", height=2, command=controller.stop)
        self.stop_button.pack(fill=tk.X, pady=4)
        root.after(100, self.poll)

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
        self.root.after(100, self.poll)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiAudioCapture")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--precision", choices=("fp32", "int8"), default="fp32")
    parser.add_argument("--preset", choices=("FAST", "BALANCED", "LONG"), default="BALANCED")
    parser.add_argument("--script-url", default=DEFAULT_SCRIPT_URL)
    parser.add_argument("--hytrans-url", default=DEFAULT_HYTRANS_URL)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-models", action="store_true")
    parser.add_argument("--self-test-server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug-log", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_system_logging("MekiAudioCapture", args.debug_log)
    install_exception_hooks()
    prepare_streams()
    if args.self_test:
        assert normalize_precision(args.precision) in {"fp32", "int8"}
        assert normalize_preset(args.preset) in {"FAST", "BALANCED", "LONG"}
        work_dir()
        model_roots = model_root_candidates(app_dir(), resource_dir())
        if not model_roots or not any(root.name == "models" for root in model_roots):
            raise RuntimeError("모델 경로가 MekiAudioCapture/models가 아닙니다.")
        return 0
    if args.self_test_models:
        models = resolve_models(app_dir(), resource_dir(), args.precision)
        source_test_wav = models["tokens"].parent / "test.wav"
        collect_vad_intervals(np.zeros(16_000, dtype=np.float32), models["vad"], args.preset)
        recognizer = create_recognizer(models, num_threads=1)
        if source_test_wav.is_file():
            with wave.open(str(source_test_wav), "rb") as source:
                if source.getframerate() != 16_000 or source.getnchannels() != 1:
                    raise RuntimeError("음성인식 테스트 WAV 형식이 올바르지 않습니다.")
                samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
            stream = recognizer.create_stream()
            stream.accept_waveform(16_000, samples.astype(np.float32) / 32768.0)
            recognizer.decode_stream(stream)
            if not str(stream.result.text).strip():
                raise RuntimeError("ReazonSpeech 테스트 결과가 비어 있습니다.")
        return 0
    controller = CaptureController(
        args.precision,
        args.preset,
        args.script_url,
        args.hytrans_url,
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
