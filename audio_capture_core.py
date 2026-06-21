from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np


INTERNAL_SAMPLE_RATE = 16_000
CAPTURE_SAMPLE_RATE = 48_000
REAZONSPEECH_ARCHIVE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01.tar.bz2"
)
SILERO_VAD_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "silero_vad.onnx"
)
REAZONSPEECH_FILES = (
    "tokens.txt",
    "encoder-epoch-99-avg-1.onnx",
    "decoder-epoch-99-avg-1.onnx",
    "joiner-epoch-99-avg-1.onnx",
    "encoder-epoch-99-avg-1.int8.onnx",
    "joiner-epoch-99-avg-1.int8.onnx",
)

VAD_PRESETS: dict[str, dict[str, float]] = {
    "FAST": {
        "threshold": 0.50,
        "min_speech_duration": 0.20,
        "min_silence_duration": 0.55,
        "max_segment_duration": 20.0,
        "pre_padding": 0.15,
        "post_padding": 0.35,
        "merge_gap": 0.50,
        "merge_short_under": 0.80,
        "forced_cut_overlap": 0.40,
    },
    "BALANCED": {
        "threshold": 0.50,
        "min_speech_duration": 0.25,
        "min_silence_duration": 0.80,
        "max_segment_duration": 25.0,
        "pre_padding": 0.20,
        "post_padding": 0.45,
        "merge_gap": 0.80,
        "merge_short_under": 1.20,
        "forced_cut_overlap": 0.50,
    },
    "LONG": {
        "threshold": 0.50,
        "min_speech_duration": 0.25,
        "min_silence_duration": 1.00,
        "max_segment_duration": 27.0,
        "pre_padding": 0.25,
        "post_padding": 0.55,
        "merge_gap": 1.00,
        "merge_short_under": 1.50,
        "forced_cut_overlap": 0.60,
    },
}


@dataclass
class SpeechSegment:
    id: int
    start_time: float
    end_time: float
    duration: float
    audio: np.ndarray
    is_forced_cut: bool
    is_short: bool
    previous_overlap: float


@dataclass
class STTResult:
    segment_id: int
    start_time: float
    end_time: float
    duration: float
    text_ja: str
    is_forced_cut: bool
    is_short: bool
    stt_latency: float


def normalize_precision(value: str) -> str:
    return "int8" if str(value).lower() == "int8" else "fp32"


def normalize_preset(value: str) -> str:
    value = str(value).upper()
    return value if value in VAD_PRESETS else "BALANCED"


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def append_script_text(script_url: str, result: STTResult, entry_id: str | None = None) -> None:
    _post_json(
        f"{script_url.rstrip('/')}/append",
        {
            "id": entry_id or str(result.segment_id),
            "text": result.text_ja,
            "startTime": result.start_time,
            "endTime": result.end_time,
        },
    )


def set_script_translation(
    script_url: str,
    result: STTResult,
    text_ko: str,
    entry_id: str | None = None,
) -> None:
    _post_json(
        f"{script_url.rstrip('/')}/translation",
        {"id": entry_id or str(result.segment_id), "text": text_ko},
    )


def translate_text(hytrans_url: str, text: str, timeout: float = 130.0) -> str:
    response = _post_json(
        f"{hytrans_url.rstrip('/')}/translate?format=json",
        {"text": text},
        timeout=timeout,
    )
    return str(response.get("text", "")).strip()


def model_root_candidates(application_dir: Path, resource_dir: Path) -> list[Path]:
    del resource_dir
    # In a frozen build application_dir is the MekiAudioCapture executable
    # folder, so this is always MekiAudioCapture/models. Models deliberately
    # live outside PyInstaller's _internal resource directory.
    return [application_dir / "models"]


def _model_paths(root: Path, precision: str) -> dict[str, Path]:
    speech = root / "reazonspeech-ja"
    if normalize_precision(precision) == "int8":
        encoder = speech / "encoder-epoch-99-avg-1.int8.onnx"
        joiner = speech / "joiner-epoch-99-avg-1.int8.onnx"
    else:
        encoder = speech / "encoder-epoch-99-avg-1.onnx"
        joiner = speech / "joiner-epoch-99-avg-1.onnx"
    return {
        "tokens": speech / "tokens.txt",
        "encoder": encoder,
        "decoder": speech / "decoder-epoch-99-avg-1.onnx",
        "joiner": joiner,
        "vad": root / "vad" / "silero_vad.onnx",
    }


def resolve_models(
    application_dir: Path,
    resource_dir: Path,
    precision: str,
) -> dict[str, Path]:
    precision = normalize_precision(precision)
    for root in model_root_candidates(application_dir, resource_dir):
        paths = _model_paths(root, precision)
        if all(path.is_file() and path.stat().st_size > 0 for path in paths.values()):
            return paths
    expected = model_root_candidates(application_dir, resource_dir)[0]
    raise FileNotFoundError(
        "음성인식 모델을 찾을 수 없습니다. "
        f"{expected} 폴더를 확인하세요."
    )


def _download_file(
    url: str,
    destination: Path,
    progress: Callable[[str], None] | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        if partial.exists():
            partial.unlink()
        request = urllib.request.Request(url, headers={"User-Agent": "MekiAudioCapture/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length", "0") or 0)
            copied = 0
            last_report = -1
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                copied += len(chunk)
                if progress and total:
                    percent = int(copied * 100 / total)
                    if percent >= last_report + 5:
                        progress(f"모델 다운로드 중... {percent}%")
                        last_report = percent
            output.flush()
            os.fsync(output.fileno())
        if partial.stat().st_size <= 0:
            raise RuntimeError(f"빈 모델 파일이 다운로드되었습니다: {url}")
        os.replace(partial, destination)
    except Exception:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _extract_reazonspeech_archive(archive: Path, speech_dir: Path) -> None:
    wanted = set(REAZONSPEECH_FILES) | {"test.wav"}
    extracted: set[str] = set()
    speech_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as bundle:
        for member in bundle.getmembers():
            source_name = Path(member.name).name
            if source_name not in wanted or not member.isfile():
                continue
            if source_name == "test.wav" and not member.name.replace("\\", "/").endswith(
                "test_wavs/1.wav"
            ):
                continue
            source = bundle.extractfile(member)
            if source is None:
                continue
            target = speech_dir / source_name
            temporary = target.with_name(target.name + ".part")
            with source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if temporary.stat().st_size <= 0:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"모델 압축 파일의 {member.name} 항목이 비어 있습니다.")
            os.replace(temporary, target)
            extracted.add(source_name)
    missing = set(REAZONSPEECH_FILES) - extracted - {
        path.name for path in speech_dir.iterdir() if path.is_file() and path.stat().st_size > 0
    }
    if missing:
        raise RuntimeError(f"모델 압축 파일에 필요한 파일이 없습니다: {', '.join(sorted(missing))}")


def ensure_models(
    application_dir: Path,
    resource_dir: Path,
    precision: str,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Path]:
    """Use MekiAudioCapture/models first and download missing models only."""
    try:
        return resolve_models(application_dir, resource_dir, precision)
    except FileNotFoundError:
        pass

    root = model_root_candidates(application_dir, resource_dir)[0]
    speech_dir = root / "reazonspeech-ja"
    vad_file = root / "vad" / "silero_vad.onnx"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PermissionError(
            f"모델 폴더를 만들 수 없습니다: {root}\n쓰기 가능한 위치에서 실행해 주세요."
        ) from exc

    required_speech = [speech_dir / name for name in REAZONSPEECH_FILES]
    if not all(path.is_file() and path.stat().st_size > 0 for path in required_speech):
        if progress:
            progress("ReazonSpeech 모델을 다운로드합니다. 최초 실행에는 시간이 걸릴 수 있습니다.")
        with tempfile.TemporaryDirectory(prefix="mekiaudio-model-") as temporary_dir:
            archive = Path(temporary_dir) / "reazonspeech.tar.bz2"
            _download_file(REAZONSPEECH_ARCHIVE_URL, archive, progress)
            _extract_reazonspeech_archive(archive, speech_dir)

    if not vad_file.is_file() or vad_file.stat().st_size <= 0:
        if progress:
            progress("Silero VAD 모델을 다운로드합니다...")
        _download_file(SILERO_VAD_URL, vad_file, progress)

    return resolve_models(application_dir, resource_dir, precision)


def ensure_ascii_model_paths(models: dict[str, Path], cache_dir: Path) -> dict[str, Path]:
    """Expose models through an ASCII-only path for native Windows runtimes.

    Some sherpa-onnx token/model readers still use narrow Windows paths. Hard
    links keep this compatibility view from consuming another copy of the large
    fp32 encoder; copying is only a cross-volume fallback.
    """
    if all(str(path).isascii() for path in models.values()):
        return models
    cache_dir.mkdir(parents=True, exist_ok=True)
    output: dict[str, Path] = {}
    for key, source in models.items():
        target = cache_dir / source.name
        if target.is_file() and target.stat().st_size == source.stat().st_size:
            output[key] = target
            continue
        try:
            if target.exists():
                target.unlink()
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        output[key] = target
    return output


def wav_to_mono_16k(wav_path: Path, raw_path: Path) -> np.memmap:
    """Convert a PCM16 48 kHz WAV to a disk-backed 16 kHz mono array."""
    with wave.open(str(wav_path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        frames = source.getnframes()
        if sample_width != 2:
            raise ValueError("PCM16 WAV만 처리할 수 있습니다.")
        if sample_rate % INTERNAL_SAMPLE_RATE:
            raise ValueError(f"지원하지 않는 녹음 샘플레이트입니다: {sample_rate}")
        ratio = sample_rate // INTERNAL_SAMPLE_RATE
        output_frames = frames // ratio
        target = np.memmap(raw_path, dtype=np.float32, mode="w+", shape=(output_frames,))
        write_at = 0
        remainder = np.empty((0, channels), dtype=np.float32)
        while True:
            raw = source.readframes(48_000)
            if not raw:
                break
            block = np.frombuffer(raw, dtype="<i2").reshape(-1, channels).astype(np.float32)
            block *= 1.0 / 32768.0
            if remainder.size:
                block = np.concatenate((remainder, block), axis=0)
            usable = (len(block) // ratio) * ratio
            remainder = block[usable:].copy()
            if not usable:
                continue
            mono = block[:usable].mean(axis=1).reshape(-1, ratio).mean(axis=1)
            count = min(len(mono), output_frames - write_at)
            target[write_at : write_at + count] = mono[:count]
            write_at += count
        target.flush()
    return np.memmap(raw_path, dtype=np.float32, mode="r", shape=(write_at,))


def collect_vad_intervals(
    audio: np.ndarray,
    vad_model: Path,
    preset_name: str,
    num_threads: int = 4,
) -> list[tuple[int, int]]:
    import sherpa_onnx

    preset = VAD_PRESETS[normalize_preset(preset_name)]
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(vad_model)
    config.silero_vad.threshold = preset["threshold"]
    config.silero_vad.min_silence_duration = preset["min_silence_duration"]
    config.silero_vad.min_speech_duration = preset["min_speech_duration"]
    config.silero_vad.window_size = 512
    config.silero_vad.max_speech_duration = preset["max_segment_duration"]
    config.sample_rate = INTERNAL_SAMPLE_RATE
    config.num_threads = num_threads
    config.provider = "cpu"
    vad = sherpa_onnx.VoiceActivityDetector(config, 60.0)
    intervals: list[tuple[int, int]] = []

    def drain() -> None:
        while not vad.empty():
            item = vad.front
            start = int(item.start)
            end = start + len(item.samples)
            intervals.append((start, end))
            vad.pop()

    for start in range(0, len(audio), 512):
        window = np.asarray(audio[start : start + 512], dtype=np.float32)
        if len(window) < 512:
            window = np.pad(window, (0, 512 - len(window)))
        vad.accept_waveform(window)
        drain()
    vad.flush()
    drain()
    return intervals


def build_segments(
    audio: np.ndarray,
    intervals: Iterable[tuple[int, int]],
    preset_name: str,
) -> list[SpeechSegment]:
    preset = VAD_PRESETS[normalize_preset(preset_name)]
    rate = INTERNAL_SAMPLE_RATE
    minimum = int(preset["min_speech_duration"] * rate)
    merge_gap = int(preset["merge_gap"] * rate)
    pre = int(preset["pre_padding"] * rate)
    post = int(preset["post_padding"] * rate)
    maximum = int(preset["max_segment_duration"] * rate)
    overlap = int(preset["forced_cut_overlap"] * rate)
    short_under = preset["merge_short_under"]

    speech = [(max(0, int(a)), min(len(audio), int(b))) for a, b in intervals if b - a >= minimum]
    merged: list[list[int]] = []
    for start, end in speech:
        if merged and start - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    padded: list[list[int]] = []
    for start, end in merged:
        candidate = [max(0, start - pre), min(len(audio), end + post)]
        if padded and candidate[0] <= padded[-1][1]:
            padded[-1][1] = max(padded[-1][1], candidate[1])
        else:
            padded.append(candidate)

    output: list[SpeechSegment] = []
    segment_id = 1
    for start, end in padded:
        cursor = start
        previous_overlap = 0
        while cursor < end:
            cut_end = min(cursor + maximum, end)
            forced = cut_end < end
            duration = (cut_end - cursor) / rate
            output.append(
                SpeechSegment(
                    id=segment_id,
                    start_time=cursor / rate,
                    end_time=cut_end / rate,
                    duration=duration,
                    audio=np.asarray(audio[cursor:cut_end], dtype=np.float32).copy(),
                    is_forced_cut=forced or previous_overlap > 0,
                    is_short=duration <= short_under,
                    previous_overlap=previous_overlap / rate,
                )
            )
            segment_id += 1
            if not forced:
                break
            cursor = max(cursor + 1, cut_end - overlap)
            previous_overlap = overlap
    return output


def create_recognizer(models: dict[str, Path], num_threads: int = 4):
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(models["encoder"]),
        decoder=str(models["decoder"]),
        joiner=str(models["joiner"]),
        tokens=str(models["tokens"]),
        num_threads=num_threads,
        sample_rate=INTERNAL_SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        provider="cpu",
        # Let sherpa-onnx inspect the model metadata.  Explicit "transducer"
        # selects a different token-table path for this ReazonSpeech export.
        model_type="",
    )


def remove_overlap(previous: str, current: str, limit: int = 40) -> str:
    previous = previous.strip()
    current = current.strip()
    for length in range(min(limit, len(previous), len(current)), 1, -1):
        if previous[-length:] == current[:length]:
            return current[length:].lstrip()
    return current


def recognize_segments(
    recognizer,
    segments: Iterable[SpeechSegment],
    on_result: Callable[[STTResult], None] | None = None,
) -> list[STTResult]:
    results: list[STTResult] = []
    previous = ""
    for segment in segments:
        started = time.perf_counter()
        stream = recognizer.create_stream()
        stream.accept_waveform(INTERNAL_SAMPLE_RATE, segment.audio)
        recognizer.decode_stream(stream)
        text = str(stream.result.text).strip()
        if segment.previous_overlap:
            text = remove_overlap(previous, text)
        latency = time.perf_counter() - started
        if not text:
            continue
        result = STTResult(
            segment_id=segment.id,
            start_time=segment.start_time,
            end_time=segment.end_time,
            duration=segment.duration,
            text_ja=text,
            is_forced_cut=segment.is_forced_cut,
            is_short=segment.is_short,
            stt_latency=latency,
        )
        results.append(result)
        previous = text
        if on_result:
            on_result(result)
    return results


def cleanup_work_files(work_dir: Path) -> None:
    for path in work_dir.iterdir() if work_dir.exists() else ():
        if path.is_file() and path.suffix.lower() in {".wav", ".f32"}:
            try:
                path.unlink()
            except OSError:
                pass
