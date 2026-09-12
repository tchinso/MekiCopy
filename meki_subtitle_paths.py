"""Paths shared by MekiSubtitle and the existing MekiCopy companions.

MekiSubtitle is embedded in MekiCopy, rather than being another standalone
model owner.  In a frozen distribution its process therefore starts from the
``MekiCopy`` directory while the shared speech cache belongs to the sibling
``MekiAudioCapture`` directory.  Keeping that distinction here prevents a
second multi-gigabyte STT download merely because the caller is MekiCopy.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from runtime_paths import app_root


_AUDIO_CAPTURE_DIRECTORY = "MekiAudioCapture"
_REAZON_SUBTITLE_DIRECTORY = "ReazonSubtitle"
_FFMPEG_DIRECTORY = "ffmpeg"


@dataclass(frozen=True)
class MediaTools:
    """Resolved FFmpeg executables used for subtitle source decoding."""

    ffmpeg: Path
    ffprobe: Path


def _path_key(path: Path) -> str:
    try:
        path = path.resolve()
    except OSError:
        pass
    return os.path.normcase(str(path))


def _unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = _path_key(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def shared_audio_capture_app_root() -> Path:
    """Return the app root which owns the shared STT/VAD cache.

    Source runs intentionally use this repository root: that is also where
    ``meki_audio_capture.py`` resolves its own model directory.  Frozen runs
    prefer the sibling distribution folder (``../MekiAudioCapture``).
    """

    current_root = app_root()
    source_root = Path(__file__).resolve().parent
    candidates = [
        current_root
        if current_root.name.casefold() == _AUDIO_CAPTURE_DIRECTORY.casefold()
        else current_root.parent / _AUDIO_CAPTURE_DIRECTORY,
        current_root / _AUDIO_CAPTURE_DIRECTORY,
        source_root
        if source_root.name.casefold() == _AUDIO_CAPTURE_DIRECTORY.casefold()
        else source_root.parent / _AUDIO_CAPTURE_DIRECTORY,
    ]
    for candidate in _unique_paths(candidates):
        if candidate.is_dir():
            return candidate
    return current_root


def shared_stt_model_root() -> Path:
    """Return MekiAudioCapture's portable STT/VAD model root.

    The directory is not created here.  ``audio_capture_core.ensure_models``
    owns validation, locking, and first-run downloads.
    """

    return shared_audio_capture_app_root() / "models"


def _ffmpeg_directory_candidates() -> list[Path]:
    current_root = app_root()
    # In a PyInstaller one-dir build the executable lives beside ``_internal``
    # while bundled data lives below ``sys._MEIPASS``.  Imported modules often
    # happen to report that same directory via ``__file__``, but make the
    # resource root explicit so FFmpeg discovery is not coupled to that
    # implementation detail.
    resource_root = Path(getattr(sys, "_MEIPASS", current_root))
    source_root = Path(__file__).resolve().parent
    configured = os.environ.get("MEKI_SUBTITLE_FFMPEG_DIR", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    for root in _unique_paths(
        [
            current_root,
            resource_root,
            current_root.parent / "MekiSubtitle",
            current_root.parent / _REAZON_SUBTITLE_DIRECTORY,
            source_root,
            source_root.parent / "MekiSubtitle",
            source_root.parent / _REAZON_SUBTITLE_DIRECTORY,
        ]
    ):
        candidates.extend((root / "assets" / _FFMPEG_DIRECTORY, root / _FFMPEG_DIRECTORY))
    return _unique_paths(candidates)


def _tool_names() -> tuple[str, str]:
    suffix = ".exe" if os.name == "nt" else ""
    return f"ffmpeg{suffix}", f"ffprobe{suffix}"


def resolve_media_tools() -> MediaTools:
    """Find a bundled or system FFmpeg/FFprobe pair.

    MekiSubtitle deliberately shares STT, VAD, and translation models, but
    video decoding still requires an FFmpeg pair.  A build can bundle the pair
    at ``assets/ffmpeg``; developers may instead set
    ``MEKI_SUBTITLE_FFMPEG_DIR`` or use a pair already on ``PATH``.
    """

    ffmpeg_name, ffprobe_name = _tool_names()
    searched: list[Path] = []
    for directory in _ffmpeg_directory_candidates():
        searched.append(directory)
        ffmpeg = directory / ffmpeg_name
        ffprobe = directory / ffprobe_name
        if ffmpeg.is_file() and ffprobe.is_file():
            return MediaTools(ffmpeg=ffmpeg, ffprobe=ffprobe)

    ffmpeg_path = shutil.which("ffmpeg") or shutil.which(ffmpeg_name)
    ffprobe_path = shutil.which("ffprobe") or shutil.which(ffprobe_name)
    if ffmpeg_path and ffprobe_path:
        return MediaTools(ffmpeg=Path(ffmpeg_path), ffprobe=Path(ffprobe_path))

    looked = ", ".join(str(path) for path in searched)
    raise FileNotFoundError(
        "영상 자막 생성에 필요한 FFmpeg와 FFprobe를 찾을 수 없습니다. "
        "MekiCopy/assets/ffmpeg에 두 파일을 포함하거나 "
        "MEKI_SUBTITLE_FFMPEG_DIR을 설정해 주세요. "
        f"확인한 위치: {looked or 'PATH'}"
    )
