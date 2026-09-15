from __future__ import annotations

import hashlib
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

from runtime_paths import exclusive_file_lock, fallback_app_data_dirs


INTERNAL_SAMPLE_RATE = 16_000
CAPTURE_SAMPLE_RATE = 48_000
PARAKEET_MODEL_DIRECTORY = "sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8"
REAZONSPEECH_MODEL_DIRECTORY = "reazonspeech-ja"
PARAKEET_ARCHIVE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8.tar.bz2"
)
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
PARAKEET_FILES = ("tokens.txt", "model.int8.onnx")


@dataclass(frozen=True)
class SttModel:
    """Description of an offline Japanese STT model shared by companion apps."""

    key: str
    label: str
    status_name: str
    asset_directory: str
    architecture: str
    supports_fp32: bool
    archive_url: str
    archive_filename: str
    required_files: tuple[str, ...]
    test_wav_suffix: str


STT_MODELS: dict[str, SttModel] = {
    "parakeet": SttModel(
        key="parakeet",
        label="Parakeet TDT-CTC 0.6B 일본어 (기본, INT8)",
        status_name="Parakeet TDT-CTC",
        asset_directory=PARAKEET_MODEL_DIRECTORY,
        architecture="nemo_ctc",
        supports_fp32=False,
        archive_url=PARAKEET_ARCHIVE_URL,
        archive_filename="parakeet-tdt-ctc.tar.bz2",
        required_files=PARAKEET_FILES,
        test_wav_suffix="test_wavs/test_ja_1.wav",
    ),
    "reazonspeech": SttModel(
        key="reazonspeech",
        label="ReazonSpeech 일본어 (INT8 / FP32)",
        status_name="ReazonSpeech",
        asset_directory=REAZONSPEECH_MODEL_DIRECTORY,
        architecture="transducer",
        supports_fp32=True,
        archive_url=REAZONSPEECH_ARCHIVE_URL,
        archive_filename="reazonspeech.tar.bz2",
        required_files=REAZONSPEECH_FILES,
        test_wav_suffix="test_wavs/1.wav",
    ),
}
DEFAULT_STT_MODEL = "parakeet"

# File metadata is keyed relative to the shared model root.  `tokens.txt`
# exists in both STT archives, so basename-only integrity entries would let one
# model incorrectly validate the other model's token table.
MODEL_FILE_SIZES = {
    f"{PARAKEET_MODEL_DIRECTORY}/tokens.txt": 28_557,
    f"{PARAKEET_MODEL_DIRECTORY}/model.int8.onnx": 655_542_604,
    f"{REAZONSPEECH_MODEL_DIRECTORY}/tokens.txt": 45_754,
    f"{REAZONSPEECH_MODEL_DIRECTORY}/encoder-epoch-99-avg-1.onnx": 592_347_848,
    f"{REAZONSPEECH_MODEL_DIRECTORY}/decoder-epoch-99-avg-1.onnx": 11_767_836,
    f"{REAZONSPEECH_MODEL_DIRECTORY}/joiner-epoch-99-avg-1.onnx": 10_720_115,
    f"{REAZONSPEECH_MODEL_DIRECTORY}/encoder-epoch-99-avg-1.int8.onnx": 154_670_139,
    f"{REAZONSPEECH_MODEL_DIRECTORY}/joiner-epoch-99-avg-1.int8.onnx": 2_696_970,
    "vad/silero_vad.onnx": 643_854,
}
MODEL_FILE_HASHES = {
    f"{PARAKEET_MODEL_DIRECTORY}/tokens.txt": "732f64c53909f2620c713f4106b487d92e6f54a6915b3cd3d1dbd32f9f4f392a",
    f"{PARAKEET_MODEL_DIRECTORY}/model.int8.onnx": "3addd00ef5bd1742078389e540b77394e4a508bdf2f4c9ad1b4a76d93e76598e",
    f"{REAZONSPEECH_MODEL_DIRECTORY}/tokens.txt": "2c3ac659818a48a0c04010e0593bbc4d7c8a24a054340b01131499c05fd52def",
    f"{REAZONSPEECH_MODEL_DIRECTORY}/encoder-epoch-99-avg-1.onnx": "ecdb0b771e16104aaf8e579cb3c1e32fbd589eb641c5946d82b615bd366c5f96",
    f"{REAZONSPEECH_MODEL_DIRECTORY}/decoder-epoch-99-avg-1.onnx": "58b18211ae06265466bfa17172dab574df94f76c8bcb61a3640c28ba860e4124",
    f"{REAZONSPEECH_MODEL_DIRECTORY}/joiner-epoch-99-avg-1.onnx": "d38a81d1191c9ed6de6a1719503692e07e3e973e2364adde0abae5eaaded1174",
    f"{REAZONSPEECH_MODEL_DIRECTORY}/encoder-epoch-99-avg-1.int8.onnx": "2c7bd08a8a99f9ddd0d9e458456577b1f6279214e51426f114f9eced44c54e1d",
    f"{REAZONSPEECH_MODEL_DIRECTORY}/joiner-epoch-99-avg-1.int8.onnx": "49cc7ea1d3d35a40a27442db5e89996da64bf0e683a903dce76e99e57a12e4de",
    "vad/silero_vad.onnx": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
}

VAD_PRESETS: dict[str, dict[str, float]] = {
    "FAST": {
        # Tuned against ExampleVideos/video.mkv's rapid back-and-forth
        # dialogue.  The short silence, compact padding, and ten-second
        # safety cap prevent several turns from becoming one STT request.
        "threshold": 0.60,
        "min_speech_duration": 0.20,
        "min_silence_duration": 0.10,
        "max_segment_duration": 10.0,
        "pre_padding": 0.05,
        "post_padding": 0.10,
        "merge_gap": 0.05,
        "merge_short_under": 0.60,
        "forced_cut_overlap": 0.25,
    },
    "BALANCED": {
        # Keep this preset exactly halfway between the rapid-dialogue FAST
        # profile and the unchanged LONG profile below.
        "threshold": 0.55,
        "min_speech_duration": 0.225,
        "min_silence_duration": 0.525,
        "max_segment_duration": 18.5,
        "pre_padding": 0.15,
        "post_padding": 0.325,
        "merge_gap": 0.30,
        "merge_short_under": 1.05,
        "forced_cut_overlap": 0.425,
    },
    "LONG": {
        "threshold": 0.50,
        "min_speech_duration": 0.25,
        "min_silence_duration": 0.95,
        "max_segment_duration": 27.0,
        "pre_padding": 0.25,
        "post_padding": 0.55,
        "merge_gap": 0.55,
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


def get_stt_model(model_key: str) -> SttModel:
    """Return a supported STT model or explain the valid model keys."""
    try:
        return STT_MODELS[str(model_key).lower()]
    except KeyError as exc:
        choices = ", ".join(STT_MODELS)
        raise ValueError(
            f"알 수 없는 음성인식 모델입니다: {model_key} (선택 가능: {choices})"
        ) from exc


def normalize_stt_model(value: str) -> str:
    """Normalize persisted/UI input, falling back to the default STT model."""
    key = str(value).lower()
    return key if key in STT_MODELS else DEFAULT_STT_MODEL


def effective_stt_precision(model_key: str, precision: str) -> str:
    """Return the precision the selected model can actually run with."""
    model = get_stt_model(normalize_stt_model(model_key))
    return normalize_precision(precision) if model.supports_fp32 else "int8"


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


def translate_text(
    hytrans_url: str,
    text: str,
    timeout: float = 650.0,
    *,
    realtime: bool = False,
) -> str:
    """Translate text through HYTrans, optionally using its live-tuning path."""

    payload = {"text": text}
    if realtime:
        # HYTrans uses this hint to keep a short spoken utterance from being
        # given the same 2,048-token generation budget as a bulk request.
        payload["realtime"] = True
    response = _post_json(
        f"{hytrans_url.rstrip('/')}/translate?format=json",
        payload,
        timeout=timeout,
    )
    return str(response.get("text", "")).strip()


def default_model_root(application_dir: Path) -> Path:
    """Return the portable model directory used by MekiAudioCapture."""
    return Path(application_dir) / "models"


def _shared_model_cache_candidates() -> list[Path]:
    """Return durable, caller-independent MekiAudioCapture model caches."""
    return [root / "models" for root in fallback_app_data_dirs("MekiAudioCapture")]


def model_root_candidates(
    application_dir: Path,
    resource_dir: Path,
    *,
    model_root: Path | None = None,
) -> list[Path]:
    """List portable then shared-cache model roots.

    ``model_root`` is intentionally authoritative for portable models.  It
    lets MekiSubtitle point at MekiAudioCapture's sibling ``models`` directory
    instead of creating a second copy beside its own executable.
    """
    del resource_dir
    # Keep portable/prepared models beside the executable first, but never
    # require write access there. Program Files and protected folders use a
    # durable MekiAudioCapture cache that is independent of the calling app.
    candidates = [Path(model_root) if model_root is not None else default_model_root(application_dir)]
    candidates.extend(_shared_model_cache_candidates())
    seen: set[str] = set()
    result: list[Path] = []
    for root in candidates:
        key = os.path.normcase(str(root))
        if key not in seen:
            seen.add(key)
            result.append(root)
    return result


def _select_writable_model_root(candidates: list[Path]) -> Path:
    failures: list[str] = []
    for root in candidates:
        probe = root / f".write-test-{os.getpid()}"
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="ascii")
            probe.unlink(missing_ok=True)
            return root
        except OSError as exc:
            failures.append(f"{root}: {exc}")
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
    detail = "\n".join(failures)
    raise PermissionError(f"No writable speech-model directory is available.\n{detail}")


def _model_paths(root: Path, model_key: str, precision: str) -> dict[str, Path]:
    model = get_stt_model(normalize_stt_model(model_key))
    speech = root / model.asset_directory
    if model.architecture == "nemo_ctc":
        return {
            "tokens": speech / "tokens.txt",
            "model": speech / "model.int8.onnx",
            "vad": root / "vad" / "silero_vad.onnx",
        }
    if model.architecture == "transducer":
        if effective_stt_precision(model.key, precision) == "int8":
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
    raise ValueError(f"지원하지 않는 음성인식 모델 형식입니다: {model.architecture}")


def _model_root_for_paths(models: dict[str, Path]) -> Path | None:
    if not models:
        return None
    try:
        roots = {Path(path).parent.parent for path in models.values()}
    except TypeError:
        return None
    return roots.pop() if len(roots) == 1 else None


def _model_file_key(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _model_file_is_valid(path: Path, root: Path) -> bool:
    key = _model_file_key(path, root)
    expected_size = MODEL_FILE_SIZES.get(key or "")
    try:
        return path.is_file() and expected_size is not None and path.stat().st_size == expected_size
    except OSError:
        return False


def model_paths_are_valid(models: dict[str, Path]) -> bool:
    root = _model_root_for_paths(models)
    if root is None or not all(_model_file_is_valid(path, root) for path in models.values()):
        return False

    root_key = hashlib.sha256(
        os.path.normcase(str(root.resolve())).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    cache_paths = [root / ".model-integrity.json"]
    cache_paths.extend(
        cache_root.parent / "model-integrity" / f"{root_key}.json"
        for cache_root in _shared_model_cache_candidates()
    )
    unique_cache_paths: list[Path] = []
    seen_cache_paths: set[str] = set()
    for cache_path in cache_paths:
        cache_key = os.path.normcase(str(cache_path))
        if cache_key not in seen_cache_paths:
            seen_cache_paths.add(cache_key)
            unique_cache_paths.append(cache_path)
    cache: dict = {}
    cache_sources: list[dict] = []
    for cache_path in unique_cache_paths:
        try:
            candidate = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                # Merge program-side and writable-fallback records. A valid but
                # partial read-only manifest must not hide the more complete
                # fallback written after the last expensive verification.
                cache.update(candidate)
                cache_sources.append(candidate)
        except (OSError, ValueError, TypeError):
            continue

    changed = False
    for path in models.values():
        file_key = _model_file_key(path, root)
        expected_hash = MODEL_FILE_HASHES.get(file_key or "")
        if expected_hash is None:
            return False
        try:
            stat = path.stat()
        except OSError:
            return False
        matching_record = next(
            (
                record
                for source in cache_sources
                if isinstance((record := source.get(file_key)), dict)
                and record.get("size") == stat.st_size
                and record.get("mtimeNs") == stat.st_mtime_ns
                and record.get("sha256") == expected_hash
            ),
            None,
        )
        if matching_record is None:
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                return False
            if digest.hexdigest() != expected_hash:
                return False
            cache[file_key] = {
                "size": stat.st_size,
                "mtimeNs": stat.st_mtime_ns,
                "sha256": expected_hash,
            }
            changed = True

    if changed:
        payload = json.dumps(cache, ensure_ascii=False, indent=2) + "\n"
        for cache_path in unique_cache_paths:
            temporary = cache_path.with_name(
                f"{cache_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, cache_path)
                break
            except OSError:
                continue
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return True


def resolve_models(
    application_dir: Path,
    resource_dir: Path,
    model_key: str = DEFAULT_STT_MODEL,
    precision: str = "int8",
    *,
    model_root: Path | None = None,
) -> dict[str, Path]:
    model_key = normalize_stt_model(model_key)
    precision = effective_stt_precision(model_key, precision)
    for root in model_root_candidates(application_dir, resource_dir, model_root=model_root):
        paths = _model_paths(root, model_key, precision)
        if model_paths_are_valid(paths):
            return paths
    expected = model_root_candidates(application_dir, resource_dir, model_root=model_root)[0]
    model = get_stt_model(model_key)
    raise FileNotFoundError(
        f"{model.status_name} 모델을 찾을 수 없습니다. "
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


def _extract_stt_archive(archive: Path, speech_dir: Path, model: SttModel) -> None:
    wanted = set(model.required_files)
    speech_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as bundle:
        for member in bundle.getmembers():
            source_name = Path(member.name).name
            member_name = member.name.replace("\\", "/").lstrip("./")
            is_test_wav = member_name == model.test_wav_suffix or member_name.endswith(
                f"/{model.test_wav_suffix}"
            )
            if (source_name not in wanted and not is_test_wav) or not member.isfile():
                continue
            source = bundle.extractfile(member)
            if source is None:
                continue
            target = speech_dir / ("test.wav" if is_test_wav else source_name)
            temporary = target.with_name(target.name + ".part")
            with source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if temporary.stat().st_size <= 0:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"모델 압축 파일의 {member.name} 항목이 비어 있습니다.")
            os.replace(temporary, target)
    missing = {
        name
        for name in model.required_files
        if not _model_file_is_valid(speech_dir / name, speech_dir.parent)
    }
    if missing:
        raise RuntimeError(f"모델 압축 파일에 필요한 파일이 없습니다: {', '.join(sorted(missing))}")


def ensure_models(
    application_dir: Path,
    resource_dir: Path,
    model_key: str = DEFAULT_STT_MODEL,
    precision: str = "int8",
    progress: Callable[[str], None] | None = None,
    *,
    model_root: Path | None = None,
) -> dict[str, Path]:
    """Resolve or download one STT model plus the shared Silero VAD model."""
    model_key = normalize_stt_model(model_key)
    model = get_stt_model(model_key)
    precision = effective_stt_precision(model_key, precision)
    try:
        return resolve_models(
            application_dir,
            resource_dir,
            model_key,
            precision,
            model_root=model_root,
        )
    except FileNotFoundError:
        pass

    root = _select_writable_model_root(
        model_root_candidates(application_dir, resource_dir, model_root=model_root)
    )
    speech_dir = root / model.asset_directory
    vad_file = root / "vad" / "silero_vad.onnx"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PermissionError(
            f"모델 폴더를 만들 수 없습니다: {root}\n쓰기 가능한 위치에서 실행해 주세요."
        ) from exc

    # A first launch can race with another companion process. Serialize the
    # complete model set so no process observes a half-extracted archive or a
    # shared .part file while it is still being written.
    with exclusive_file_lock(root / ".model-download.lock", timeout=3_600):
        required_speech = [speech_dir / name for name in model.required_files]
        speech_models = {path.name: path for path in required_speech}
        if not model_paths_are_valid(speech_models):
            if progress:
                progress(
                    f"{model.status_name} 모델을 다운로드합니다. 최초 실행에는 시간이 걸릴 수 있습니다."
                )
            with tempfile.TemporaryDirectory(
                prefix=".mekiaudio-model-",
                dir=root,
            ) as temporary_dir:
                archive = Path(temporary_dir) / model.archive_filename
                _download_file(model.archive_url, archive, progress)
                _extract_stt_archive(archive, speech_dir, model)
            if not model_paths_are_valid(speech_models):
                raise RuntimeError(f"다운로드한 {model.status_name} 모델의 무결성 검증에 실패했습니다.")

        vad_models = {"vad": vad_file}
        if not model_paths_are_valid(vad_models):
            if progress:
                progress("Silero VAD 모델을 다운로드합니다...")
            _download_file(SILERO_VAD_URL, vad_file, progress)
            if not model_paths_are_valid(vad_models):
                raise RuntimeError("다운로드한 Silero VAD 모델의 무결성 검증에 실패했습니다.")

    return resolve_models(
        application_dir,
        resource_dir,
        model_key,
        precision,
        model_root=model_root,
    )


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
    vad = create_voice_activity_detector(vad_model, preset_name, num_threads=num_threads)
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


def create_voice_activity_detector(
    vad_model: Path,
    preset_name: str,
    num_threads: int = 4,
):
    """Create one CPU Silero VAD instance for batch or live capture.

    Callers that feed a live stream must keep the returned native object on a
    single thread.  ``VoiceActivityDetector.front`` is only valid until the
    next detector operation, so live callers also need to copy its samples
    before calling ``pop``.
    """
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
    return sherpa_onnx.VoiceActivityDetector(config, 60.0)


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
                    # The source memmap remains open for the whole recognition
                    # pass, so a contiguous view avoids duplicating long
                    # recordings in RAM for every segment.
                    audio=np.asarray(audio[cursor:cut_end], dtype=np.float32),
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


def validate_tokens_file(tokens_path: Path) -> int:
    """Validate the token IDs before handing the table to the native runtime."""
    seen: set[int] = set()
    try:
        lines = tokens_path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"음성 모델 토큰 사전을 읽을 수 없습니다: {tokens_path}") from exc

    for line_number, line in enumerate(lines, 1):
        fields = line.rsplit(maxsplit=1)
        if len(fields) != 2:
            raise ValueError(f"음성 모델 토큰 사전 {line_number}행의 형식이 잘못되었습니다.")
        try:
            token_id = int(fields[1])
        except ValueError as exc:
            raise ValueError(f"음성 모델 토큰 사전 {line_number}행의 ID가 잘못되었습니다.") from exc
        if token_id < 0 or token_id in seen:
            raise ValueError(f"음성 모델 토큰 사전 {line_number}행의 ID가 중복되었거나 음수입니다.")
        seen.add(token_id)

    if not seen:
        raise ValueError("음성 모델 토큰 사전이 비어 있습니다.")
    expected = set(range(max(seen) + 1))
    if seen != expected:
        missing = min(expected - seen)
        raise ValueError(f"음성 모델 토큰 사전에 ID {missing}이(가) 없습니다.")
    return len(seen)


def create_recognizer(
    models: dict[str, Path],
    model_key: str = DEFAULT_STT_MODEL,
    num_threads: int = 4,
    *,
    precision: str | None = None,
):
    """Create the appropriate CPU offline recognizer for an STT model."""
    import sherpa_onnx

    model = get_stt_model(normalize_stt_model(model_key))
    # Precision selects the transducer files during model resolution. Keeping
    # it in this reusable factory's public signature makes callers explicit
    # while preventing an unsupported FP32 request from changing Parakeet's
    # fixed INT8 runtime path.
    if precision is not None:
        effective_stt_precision(model.key, precision)
    validate_tokens_file(models["tokens"])
    if model.architecture == "nemo_ctc":
        # Parakeet is a NeMo CTC export, not a ReazonSpeech transducer. Audio
        # reaches this recognizer as 16 kHz mono from ``wav_to_mono_16k``;
        # retain the model's 80-bin features, greedy CPU decoding, and the
        # dedicated NeMo factory instead of treating model.int8.onnx as an
        # encoder/joiner bundle.
        return sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=str(models["model"]),
            tokens=str(models["tokens"]),
            num_threads=num_threads,
            sample_rate=INTERNAL_SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            provider="cpu",
        )
    if model.architecture == "transducer":
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
            # Let sherpa-onnx inspect the model metadata. Explicit
            # "transducer" selects a different token-table path for this
            # ReazonSpeech export.
            model_type="",
        )
    raise ValueError(f"지원하지 않는 음성인식 모델 형식입니다: {model.architecture}")


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
    try:
        children = list(work_dir.iterdir()) if work_dir.exists() else []
    except OSError:
        children = []
    for path in children:
        try:
            removable = path.is_file() and path.suffix.lower() in {".wav", ".f32"}
        except OSError:
            removable = False
        if removable:
            try:
                path.unlink()
            except OSError:
                pass
    try:
        work_dir.rmdir()
    except OSError:
        pass
