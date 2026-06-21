from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import queue
import sys
import threading
import traceback
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
    ensure_ascii_model_paths,
    normalize_precision,
    normalize_preset,
    recognize_segments,
    resolve_models,
    set_script_translation,
    translate_text,
    wav_to_mono_16k,
)
from runtime_paths import prepare_tk_environment
from service_ports import (
    AUDIO_CAPTURE_DEFAULT_PORT,
    HYTRANS_DEFAULT_PORT,
    SCRIPT_DEFAULT_PORT,
)

prepare_tk_environment("MekiCopyRuntime")
import tkinter as tk
from tkinter import messagebox


DEFAULT_PORT = AUDIO_CAPTURE_DEFAULT_PORT
DEFAULT_SCRIPT_URL = f"http://127.0.0.1:{SCRIPT_DEFAULT_PORT}"
DEFAULT_HYTRANS_URL = f"http://127.0.0.1:{HYTRANS_DEFAULT_PORT}"
_WINDOW_STREAM = None


def app_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", app_dir()))


def work_dir() -> Path:
    base = Path(os.environ.get("PROGRAMDATA", str(Path.home() / "AppData" / "Local")))
    path = base / "MekiCopy" / "MekiAudioCapture"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_streams() -> None:
    global _WINDOW_STREAM
    if sys.stderr is None:
        _WINDOW_STREAM = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = _WINDOW_STREAM
    if sys.stdout is None:
        sys.stdout = sys.stderr


class CaptureController:
    def __init__(self, precision: str, preset: str, script_url: str, hytrans_url: str) -> None:
        self.precision = normalize_precision(precision)
        self.preset = normalize_preset(preset)
        self.script_url = script_url.rstrip("/")
        self.hytrans_url = hytrans_url.rstrip("/")
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stop_event = threading.Event()
        self.record_thread: threading.Thread | None = None
        self.process_thread: threading.Thread | None = None
        self.wav_path: Path | None = None
        self.session_id = ""
        self.state = "READY"
        self.status = "녹음 준비"
        self.error = ""
        self._lock = threading.Lock()

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
        if self.state not in {"READY", "ERROR"}:
            raise RuntimeError("녹음 또는 처리 중에는 설정을 바꿀 수 없습니다.")
        self.precision = normalize_precision(str(payload.get("precision", self.precision)))
        self.preset = normalize_preset(str(payload.get("preset", self.preset)))
        self.script_url = str(payload.get("scriptUrl", self.script_url)).rstrip("/")
        self.hytrans_url = str(payload.get("hytransUrl", self.hytrans_url)).rstrip("/")

    def _set_state(self, state: str, status: str, error: str = "") -> None:
        with self._lock:
            self.state = state
            self.status = status
            self.error = error
        self.events.put(("status", status))

    def start(self) -> None:
        if self.state not in {"READY", "ERROR"}:
            return
        cleanup_work_files(work_dir())
        now = dt.datetime.now()
        self.session_id = f"{now:%Y%m%d-%H%M%S-%f}"
        self.wav_path = work_dir() / f"capture-{now:%Y%m%d-%H%M%S}.wav"
        self.stop_event.clear()
        self._set_state("RECORDING", "컴퓨터 소리를 녹음하고 있습니다…")
        self.record_thread = threading.Thread(target=self._record_loop, daemon=True)
        self.record_thread.start()

    def stop(self) -> None:
        if self.state != "RECORDING":
            return
        self._set_state("STOPPING", "녹음을 마무리하고 있습니다…")
        self.stop_event.set()
        self.process_thread = threading.Thread(target=self._finish_and_process, daemon=True)
        self.process_thread.start()

    def _record_loop(self) -> None:
        assert self.wav_path is not None
        try:
            speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("기본 출력 장치를 찾을 수 없습니다.")
            loopback = sc.get_microphone(speaker.id, include_loopback=True)
            if loopback is None:
                raise RuntimeError("기본 출력 장치의 WASAPI loopback을 열 수 없습니다.")
            chunk_frames = CAPTURE_SAMPLE_RATE // 10
            with wave.open(str(self.wav_path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(CAPTURE_SAMPLE_RATE)
                with loopback.recorder(
                    samplerate=CAPTURE_SAMPLE_RATE,
                    channels=2,
                    blocksize=chunk_frames,
                ) as recorder:
                    while not self.stop_event.is_set():
                        block = recorder.record(numframes=chunk_frames)
                        pcm = np.clip(block, -1.0, 1.0)
                        output.writeframes((pcm * 32767.0).astype("<i2").tobytes())
        except Exception as exc:
            self._set_state("ERROR", f"녹음 실패: {exc}", traceback.format_exc())
            self.stop_event.set()
            cleanup_work_files(work_dir())

    def _finish_and_process(self) -> None:
        if self.record_thread:
            self.record_thread.join(timeout=5)
        if self.state == "ERROR":
            cleanup_work_files(work_dir())
            return
        final_state = "READY"
        final_status = "완료"
        final_error = ""
        audio = None
        try:
            assert self.wav_path is not None
            raw_path = work_dir() / "capture-16k.f32"
            self._set_state("PROCESSING", "음성을 16 kHz mono로 변환하고 있습니다…")
            audio = wav_to_mono_16k(self.wav_path, raw_path)
            models = ensure_models(
                app_dir(),
                resource_dir(),
                self.precision,
                progress=lambda text: self._set_state("DOWNLOADING", text),
            )
            models = ensure_ascii_model_paths(
                models,
                work_dir() / "models" / self.precision,
            )
            self._set_state("PROCESSING", f"VAD로 음성 구간을 찾고 있습니다 ({self.preset})…")
            intervals = collect_vad_intervals(audio, models["vad"], self.preset)
            segments = build_segments(audio, intervals, self.preset)
            self._set_state("PROCESSING", f"일본어 음성을 인식하고 있습니다 (0/{len(segments)})…")
            recognizer = create_recognizer(models)
            count = 0

            def publish(result) -> None:
                nonlocal count
                entry_id = f"{self.session_id}-{result.segment_id}"
                append_script_text(self.script_url, result, entry_id=entry_id)
                count += 1
                self._set_state("PROCESSING", f"일본어 음성을 인식하고 있습니다 ({count}/{len(segments)})…")

            results = recognize_segments(recognizer, segments, on_result=publish)
            for index, result in enumerate(results, 1):
                self._set_state("TRANSLATING", f"번역하고 있습니다 ({index}/{len(results)})…")
                try:
                    translated = translate_text(self.hytrans_url, result.text_ja)
                except Exception as exc:
                    translated = f"[번역 실패] {exc}"
                entry_id = f"{self.session_id}-{result.segment_id}"
                set_script_translation(self.script_url, result, translated, entry_id=entry_id)
            if results:
                final_status = f"완료: 일본어 {len(results)}개를 인식하고 번역했습니다."
            else:
                final_status = "완료: 인식된 일본어 음성이 없습니다."
        except Exception as exc:
            final_state = "ERROR"
            final_status = f"처리 실패: {exc}"
            final_error = traceback.format_exc()
        finally:
            if isinstance(audio, np.memmap):
                try:
                    audio._mmap.close()
                except Exception:
                    pass
            cleanup_work_files(work_dir())
            self._set_state(final_state, final_status, final_error)


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
    return parser.parse_args()


def main() -> int:
    prepare_streams()
    args = parse_args()
    if args.self_test:
        assert normalize_precision(args.precision) in {"fp32", "int8"}
        assert normalize_preset(args.preset) in {"FAST", "BALANCED", "LONG"}
        work_dir()
        expected_model_root = app_dir() / "models"
        if expected_model_root.parent != app_dir():
            raise RuntimeError("모델 경로가 MekiAudioCapture/models가 아닙니다.")
        return 0
    if args.self_test_models:
        models = resolve_models(app_dir(), resource_dir(), args.precision)
        source_test_wav = models["tokens"].parent / "test.wav"
        models = ensure_ascii_model_paths(
            models,
            work_dir() / "models" / args.precision,
        )
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
    controller = CaptureController(args.precision, args.preset, args.script_url, args.hytrans_url)
    try:
        set_windows_app_id("MekiAudioCapture")
        root = tk.Tk()
        apply_tk_icon(root)
        CaptureWindow(root, controller)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(controller))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        root.mainloop()
        server.shutdown()
        return 0
    except Exception as exc:
        try:
            messagebox.showerror("MekiAudioCapture", str(exc))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
