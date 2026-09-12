"""Headless video-to-SRT pipeline used by MekiCopy's MekiSubtitle tab.

The module deliberately owns only video decoding, subtitle timing, and job
orchestration.  STT/VAD assets come from ``audio_capture_core`` and translation
is supplied by the already-running HYTrans service (or by an injected callable).
That keeps the large model caches single-owned and usable by every companion.
"""

from __future__ import annotations

import inspect
import json
import math
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from meki_subtitle_paths import MediaTools, resolve_media_tools, shared_audio_capture_app_root, shared_stt_model_root
from runtime_paths import writable_app_subdir


SAMPLE_RATE = 16_000
DEFAULT_STT_MODEL = "parakeet"
DEFAULT_VAD_PRESET = "FAST"
DEFAULT_TRANSLATION_TIMEOUT_SECONDS = 600.0

StatusCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]
TranslationCallable = Callable[..., str]


class SubtitleCancelledError(RuntimeError):
    """Raised when a caller-owned cancellation event requests job shutdown."""


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    audio_stream_index: int
    codec_name: str


@dataclass(frozen=True)
class SubtitleEntry:
    start_time: float
    end_time: float
    korean: str
    japanese: str = ""


@dataclass(frozen=True)
class SubtitleProcessSummary:
    output_path: Path
    media_duration: float
    vad_intervals: int
    recognized: int
    translated: int
    translation_failures: int
    elapsed: float


_ASSISTANT_PREFIX = re.compile(r"^assistant\s*[:：]?\s*", re.IGNORECASE)
_SPACE_RE = re.compile(r"[ \t\u3000]+")


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def _thread_count() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def _check_cancel(cancel_event: Any) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SubtitleCancelledError("사용자가 자막 생성을 취소했습니다.")


def _status(callback: StatusCallback | None, value: float, message: str) -> None:
    if callback is not None:
        callback(max(0.0, min(1.0, float(value))), message)


def probe_media(input_path: Path, ffprobe_path: Path) -> MediaInfo:
    """Inspect only the first source audio stream without touching the video."""

    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index,codec_name,duration:format=duration",
        "-of",
        "json",
        str(input_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creation_flags(),
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "FFprobe가 파일을 읽지 못했습니다."
        raise RuntimeError(detail)
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        if not streams:
            raise ValueError("audio stream missing")
        stream = streams[0]
        raw_duration = stream.get("duration") or payload.get("format", {}).get("duration")
        return MediaInfo(
            duration=max(0.0, float(raw_duration or 0.0)),
            audio_stream_index=int(stream.get("index", 0)),
            codec_name=str(stream.get("codec_name") or "unknown"),
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("영상에서 오디오 스트림 정보를 확인할 수 없습니다.") from exc


def extract_original_audio(
    input_path: Path,
    wav_path: Path,
    ffmpeg_path: Path,
    duration: float,
    *,
    progress: StatusCallback | None = None,
    cancel_event: Any = None,
) -> None:
    """Decode the first original audio stream into timeline-preserving PCM.

    Audio is deliberately decoded at 48 kHz because the shared audio pipeline
    owns the tested 48 kHz -> 16 kHz conversion.  ``aresample`` preserves any
    timestamp gaps as silence, so later SRT times remain tied to the source
    video rather than the speech-only segments.
    """

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg_path),
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        "aresample=async=1:first_pts=0",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "-progress",
        "pipe:1",
        "-nostats",
        str(wav_path),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creation_flags(),
    )
    messages: list[str] = []
    assert process.stdout is not None
    try:
        while True:
            _check_cancel(cancel_event)
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.02)
                continue
            line = line.strip()
            if not line:
                continue
            key, separator, value = line.partition("=")
            if separator and key in {"out_time_us", "out_time_ms"}:
                try:
                    # FFmpeg currently reports both values in microseconds.
                    elapsed = int(value) / 1_000_000.0
                    ratio = min(1.0, elapsed / duration) if duration > 0 else 0.0
                    _status(progress, ratio, "원본 오디오를 추출하고 있습니다.")
                except ValueError:
                    pass
            elif key not in {"progress", "bitrate", "speed", "total_size"}:
                messages.append(line)
        return_code = process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        raise
    finally:
        process.stdout.close()

    if return_code != 0 or not wav_path.is_file() or wav_path.stat().st_size <= 44:
        detail = "\n".join(messages[-8:]).strip()
        raise RuntimeError(detail or "FFmpeg 오디오 추출에 실패했습니다.")
    _status(progress, 1.0, "원본 오디오 추출을 완료했습니다.")


def _default_model_key() -> str:
    # Import lazily: MekiCopy can still render its UI before the native STT
    # runtime is loaded, and frozen import errors then remain attributable to
    # the actual action the user requested.
    from audio_capture_core import DEFAULT_STT_MODEL as capture_default

    return str(capture_default or DEFAULT_STT_MODEL)


def prepare_stt_models(
    *,
    stt_model: str | None = None,
    precision: str = "int8",
    model_root: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Path]:
    """Ensure the exact MekiAudioCapture STT/VAD cache is ready.

    ``audio_capture_core`` remains the only downloader and integrity owner.
    Passing the sibling MekiAudioCapture root makes either app's first launch
    populate the same portable model directory, with its canonical shared
    writable fallback when the installation is read-only.
    """

    from audio_capture_core import ensure_models, normalize_stt_model

    owner_root = shared_audio_capture_app_root()
    selected_model = normalize_stt_model(stt_model or _default_model_key())
    return ensure_models(
        owner_root,
        owner_root,
        model_key=selected_model,
        precision=precision,
        progress=progress,
        model_root=model_root or shared_stt_model_root(),
    )


def _remove_overlap(previous: str, current: str, limit: int = 40) -> str:
    previous = previous.strip()
    current = current.strip()
    for length in range(min(limit, len(previous), len(current)), 1, -1):
        if previous[-length:] == current[:length]:
            return current[length:].lstrip()
    return current


def _recognize_segments(
    recognizer: Any,
    segments: Iterable[Any],
    *,
    model_name: str,
    status: StatusCallback | None,
    cancel_event: Any,
) -> list[tuple[float, float, str]]:
    items = list(segments)
    results: list[tuple[float, float, str]] = []
    previous_text = ""
    for index, segment in enumerate(items, 1):
        _check_cancel(cancel_event)
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, segment.audio)
        recognizer.decode_stream(stream)
        text = str(stream.result.text).strip()
        if getattr(segment, "previous_overlap", 0):
            text = _remove_overlap(previous_text, text)
        if text:
            # A forced overlap exists only for ASR context.  Its duplicate
            # time must not become a second overlapping SRT cue.
            start = min(
                float(segment.end_time),
                float(segment.start_time) + float(getattr(segment, "previous_overlap", 0)),
            )
            results.append((start, float(segment.end_time), text))
            previous_text = text
        _status(
            status,
            0.38 + index / max(1, len(items)) * 0.27,
            f"{model_name} 일본어 인식 중 ({index}/{len(items)})",
        )
    return results


def _clean_translation(text: str) -> str:
    result = _ASSISTANT_PREFIX.sub("", str(text).strip()).strip()
    if len(result) >= 2 and result[0] == result[-1] and result[0] in {'"', "'"}:
        result = result[1:-1].strip()
    return result


def _call_translation(
    translator: TranslationCallable,
    text: str,
    *,
    timeout: float,
    cancel_event: Any,
) -> str:
    """Call an injected translator without hiding its own TypeErrors.

    A callback may accept just ``text`` or optional ``timeout`` and
    ``cancel_event`` keyword arguments.  Signature inspection avoids treating
    a real TypeError raised inside the translator as a signature mismatch.
    """

    kwargs: dict[str, Any] = {}
    try:
        signature = inspect.signature(translator)
        parameters = signature.parameters
        accepts_keywords = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if accepts_keywords or "timeout" in parameters:
            kwargs["timeout"] = timeout
        if accepts_keywords or "cancel_event" in parameters:
            kwargs["cancel_event"] = cancel_event
    except (TypeError, ValueError):
        # Callable objects without a discoverable signature follow the simple
        # one-argument contract.
        pass
    return str(translator(text, **kwargs)).strip()


def translate_via_hytrans(
    hytrans_url: str,
    text: str,
    *,
    timeout: float = DEFAULT_TRANSLATION_TIMEOUT_SECONDS,
    cancel_event: Any = None,
) -> str:
    """Translate one segment through the existing local HYTrans service."""

    _check_cancel(cancel_event)
    endpoint = f"{hytrans_url.rstrip('/')}/translate?format=json"
    data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HYTrans 번역 요청이 실패했습니다 (HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("HYTrans에 연결할 수 없습니다. HYTrans를 먼저 실행해 주세요.") from exc
    _check_cancel(cancel_event)
    if payload.get("ok") is not True:
        raise RuntimeError(f"HYTrans 번역이 실패했습니다: {payload}")
    translated = str(payload.get("text") or "").strip()
    if not translated:
        raise RuntimeError("HYTrans가 빈 번역 결과를 반환했습니다.")
    return translated


def _translation_callable(
    *,
    translate: TranslationCallable | None,
    translation_url: str | None,
) -> TranslationCallable | None:
    if translate is not None:
        return translate
    if translation_url:
        return lambda text, timeout=DEFAULT_TRANSLATION_TIMEOUT_SECONDS, cancel_event=None: translate_via_hytrans(
            translation_url,
            text,
            timeout=timeout,
            cancel_event=cancel_event,
        )
    return None


def _srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(math.floor(float(seconds) * 1000.0 + 0.5)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{milliseconds:03d}"


def _clean_subtitle_text(text: str) -> str:
    lines = []
    for line in str(text).replace("\r", "\n").split("\n"):
        cleaned = _SPACE_RE.sub(" ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def write_srt_atomic(entries: Iterable[SubtitleEntry], destination: Path) -> int:
    """Publish UTF-8-BOM SRT only after the complete job succeeds."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    blocks: list[str] = []
    for entry in entries:
        text = _clean_subtitle_text(entry.korean)
        if not text:
            continue
        index = len(blocks) + 1
        start = _srt_timestamp(entry.start_time)
        end = _srt_timestamp(max(entry.start_time + 0.001, entry.end_time))
        blocks.append(f"{index}\n{start} --> {end}\n{text}\n")
    temporary.write_text("\n".join(blocks), encoding="utf-8-sig", newline="\n")
    os.replace(temporary, destination)
    return len(blocks)


def _model_label(model_key: str) -> str:
    from audio_capture_core import STT_MODELS

    selected = STT_MODELS.get(model_key)
    return str(getattr(selected, "status_name", None) or getattr(selected, "label", None) or model_key)


def process_video(
    input_path: str | Path,
    output_path: str | Path,
    *,
    stt_model: str | None = None,
    precision: str = "int8",
    vad_preset: str = DEFAULT_VAD_PRESET,
    translate: TranslationCallable | None = None,
    translation_url: str | None = None,
    translation_timeout: float = DEFAULT_TRANSLATION_TIMEOUT_SECONDS,
    media_tools: MediaTools | None = None,
    stt_model_root: Path | None = None,
    status: StatusCallback | None = None,
    log: LogCallback | None = None,
    cancel_event: Any = None,
) -> SubtitleProcessSummary:
    """Create Korean SRT from the first audio stream of a video.

    ``translate`` takes precedence over ``translation_url``.  Supplying the
    URL is the normal embedded-MekiCopy route and therefore reuses the already
    selected HYTrans model/service rather than creating a subtitle-only copy.
    Silent videos do not require either translation input and yield an empty
    SRT normally.
    """

    from audio_capture_core import (
        build_segments,
        collect_vad_intervals,
        create_recognizer,
        normalize_stt_model,
        wav_to_mono_16k,
    )

    started = time.perf_counter()
    source = Path(input_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"영상 파일이 없습니다: {source}")
    if source == destination:
        raise ValueError("입력 영상과 출력 SRT 경로가 같습니다.")

    selected_model = normalize_stt_model(stt_model or _default_model_key())
    model_name = _model_label(selected_model)
    tools = media_tools or resolve_media_tools()
    _check_cancel(cancel_event)
    _status(status, 0.0, "MekiAudioCapture 공용 음성 모델을 확인하고 있습니다.")

    def report_stt_download(message: str) -> None:
        # ``ensure_models`` reports archive progress from its download loop.
        # Check here as well as at stage boundaries so a first 650 MB Parakeet
        # download can be cancelled promptly when MekiCopy is closing.
        _check_cancel(cancel_event)
        _status(status, 0.02, message)

    models = prepare_stt_models(
        stt_model=selected_model,
        precision=precision,
        model_root=stt_model_root,
        progress=report_stt_download,
    )
    _check_cancel(cancel_event)
    _status(status, 0.03, "영상의 원본 오디오 스트림을 확인하고 있습니다.")
    media = probe_media(source, tools.ffprobe)
    if log:
        log(
            f"오디오 스트림 #{media.audio_stream_index} ({media.codec_name}), "
            f"영상 길이 {media.duration:.2f}초"
        )
        log(f"일본어 STT: {model_name}")

    audio: np.memmap | None = None
    work_root = writable_app_subdir("MekiSubtitle", "work")
    with tempfile.TemporaryDirectory(prefix="subtitle-", dir=work_root) as temporary:
        temporary_dir = Path(temporary)
        wav_path = temporary_dir / "source-audio-48k.wav"
        raw_path = temporary_dir / "source-audio-16k.f32"
        try:
            extract_original_audio(
                source,
                wav_path,
                tools.ffmpeg,
                media.duration,
                progress=lambda ratio, message: _status(status, 0.04 + ratio * 0.13, message),
                cancel_event=cancel_event,
            )
            _check_cancel(cancel_event)
            _status(status, 0.18, "추출한 오디오를 정밀 타임라인으로 변환하고 있습니다.")
            audio = wav_to_mono_16k(wav_path, raw_path)
            _check_cancel(cancel_event)
            _status(status, 0.20, "FAST VAD로 음성과 타임스탬프를 추적하고 있습니다.")
            intervals = collect_vad_intervals(
                audio,
                models["vad"],
                vad_preset,
                num_threads=_thread_count(),
            )
            _check_cancel(cancel_event)
            segments = build_segments(audio, intervals, vad_preset)
            if log:
                log(f"{vad_preset.upper()} VAD: 원시 구간 {len(intervals)}개, 인식 구간 {len(segments)}개")
            if not segments:
                count = write_srt_atomic([], destination)
                _status(status, 1.0, "음성이 없어 빈 SRT를 저장했습니다.")
                return SubtitleProcessSummary(
                    output_path=destination,
                    media_duration=media.duration,
                    vad_intervals=len(intervals),
                    recognized=0,
                    translated=count,
                    translation_failures=0,
                    elapsed=time.perf_counter() - started,
                )

            _status(status, 0.36, f"{model_name} 일본어 인식 모델을 불러오고 있습니다.")
            recognizer = create_recognizer(
                models,
                model_key=selected_model,
                precision=precision,
                num_threads=_thread_count(),
            )
            try:
                results = _recognize_segments(
                    recognizer,
                    segments,
                    model_name=model_name,
                    status=status,
                    cancel_event=cancel_event,
                )
            finally:
                del recognizer
            if log:
                for start_time, end_time, japanese in results:
                    log(f"[{start_time:.3f}–{end_time:.3f}] JA  {japanese}")
            if not results:
                count = write_srt_atomic([], destination)
                _status(status, 1.0, "인식된 일본어가 없어 빈 SRT를 저장했습니다.")
                return SubtitleProcessSummary(
                    output_path=destination,
                    media_duration=media.duration,
                    vad_intervals=len(intervals),
                    recognized=0,
                    translated=count,
                    translation_failures=0,
                    elapsed=time.perf_counter() - started,
                )

            translator = _translation_callable(translate=translate, translation_url=translation_url)
            if translator is None:
                raise RuntimeError("HYTrans가 준비되지 않았습니다. HYTrans를 먼저 실행해 주세요.")
            _status(status, 0.66, "HYTrans로 한국어 번역을 시작합니다.")
            entries: list[SubtitleEntry] = []
            failures = 0
            for index, (start_time, end_time, japanese) in enumerate(results, 1):
                _check_cancel(cancel_event)
                _status(
                    status,
                    0.70 + (index - 1) / len(results) * 0.28,
                    f"한국어 번역 중 ({index}/{len(results)})",
                )
                translated = ""
                last_error: Exception | None = None
                for attempt in range(2):
                    try:
                        translated = _clean_translation(
                            _call_translation(
                                translator,
                                japanese,
                                timeout=translation_timeout,
                                cancel_event=cancel_event,
                            )
                        )
                        if not translated:
                            raise RuntimeError("빈 번역 결과")
                        break
                    except SubtitleCancelledError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        if attempt == 0:
                            time.sleep(0.2)
                if not translated:
                    failures += 1
                    translated = f"[번역 실패] {japanese}"
                    if log:
                        log(f"번역 실패: {last_error}")
                elif log:
                    log(f"KO  {translated}")
                entries.append(
                    SubtitleEntry(
                        start_time=start_time,
                        end_time=end_time,
                        korean=translated,
                        japanese=japanese,
                    )
                )

            _check_cancel(cancel_event)
            count = write_srt_atomic(entries, destination)
            _status(status, 1.0, f"완료: 한국어 자막 {count}개를 저장했습니다.")
            return SubtitleProcessSummary(
                output_path=destination,
                media_duration=media.duration,
                vad_intervals=len(intervals),
                recognized=len(results),
                translated=count - failures,
                translation_failures=failures,
                elapsed=time.perf_counter() - started,
            )
        finally:
            if isinstance(audio, np.memmap):
                try:
                    audio._mmap.close()
                except Exception:
                    pass
