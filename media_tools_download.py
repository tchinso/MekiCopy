"""Install the verified FFmpeg pair only when subtitle decoding needs it."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile
from typing import Callable
import urllib.request
import uuid
import zipfile

from runtime_paths import exclusive_file_lock, writable_app_subdir

VERSION = "9.0.2"
ARCHIVE_NAME = f"ffmpeg-{VERSION}-essentials_build.zip"
ARCHIVE_URL = f"https://github.com/GyanD/codexffmpeg/releases/download/{VERSION}/{ARCHIVE_NAME}"
ARCHIVE_SIZE = 114768076
ARCHIVE_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
FILES: dict[str, tuple[int, str]] = {
    "ffmpeg.exe": (105423872, "3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec"),
    "ffprobe.exe": (105221120, "f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52"),
    "LICENSE": (35147, "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"),
    "README.txt": (41577, "0342de6bb39dd421cbec2c00d89ad274b640d75ad3f2cab09ffd647ce2d8f949"),
}
Progress = Callable[[float, str], None]


def _check(check_cancel: Callable[[], None] | None) -> None:
    if check_cancel:
        check_cancel()


def _matches(path: Path, size: int, digest: str, check_cancel=None) -> bool:
    try:
        if path.stat().st_size != size:
            return False
        source = path.open("rb")
    except OSError:
        return False
    actual = hashlib.sha256()
    with source:
        while True:
            _check(check_cancel)
            try:
                chunk = source.read(1024 * 1024)
            except OSError:
                return False
            if not chunk:
                break
            actual.update(chunk)
    return actual.hexdigest() == digest


def _valid_installation(directory: Path, check_cancel=None) -> bool:
    return bool(FILES) and all(_matches(directory / name, size, digest, check_cancel)
                               for name, (size, digest) in FILES.items())


def _contained_tree(directory: Path, root: Path, check_cancel=None) -> bool:
    """Reject reparse points before traversing an abandoned install directory."""
    pending = [directory]
    while pending:
        _check(check_cancel)
        current = pending.pop()
        if (current.is_symlink() or current.is_junction()
                or not current.resolve().is_relative_to(root)):
            return False
        if current.is_dir():
            pending.extend(current.iterdir())
    return True


def _remove_abandoned_downloads(root: Path, check_cancel=None) -> None:
    """Clean owned temporary paths while holding the installation lock."""
    root = root.resolve()
    partial_name = re.compile(re.escape(ARCHIVE_NAME) + r"\.[0-9a-f]{32}\.part")
    staging_name = re.compile(r"\.ffmpeg-install-[a-z0-9_]{8}")
    for candidate in root.iterdir():
        _check(check_cancel)
        is_partial = partial_name.fullmatch(candidate.name)
        is_staging = staging_name.fullmatch(candidate.name)
        if not (is_partial or is_staging):
            continue
        try:
            if (candidate.is_symlink() or candidate.is_junction()
                    or candidate.resolve().parent != root):
                continue
            if is_partial and candidate.is_file():
                candidate.unlink()
            elif is_staging and candidate.is_dir() and _contained_tree(candidate, root, check_cancel):
                shutil.rmtree(candidate)
        except InterruptedError:
            raise
        except OSError:
            # A scanner may retain a handle; a subsequent install can retry.
            continue


def _download(destination: Path, *, progress=None, check_cancel=None) -> None:
    partial = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.part")
    try:
        request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "MekiCopy"})
        with urllib.request.urlopen(request, timeout=10) as response, partial.open("wb") as output:
            digest = hashlib.sha256()
            copied = 0
            while True:
                _check(check_cancel)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > ARCHIVE_SIZE:
                    raise RuntimeError("FFmpeg 다운로드 크기가 올바르지 않습니다.")
                output.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(copied / ARCHIVE_SIZE, f"FFmpeg 다운로드 중... {copied * 100 // ARCHIVE_SIZE}%")
            output.flush()
            os.fsync(output.fileno())
        if copied != ARCHIVE_SIZE or digest.hexdigest() != ARCHIVE_SHA256:
            raise RuntimeError("FFmpeg 다운로드 무결성 검증에 실패했습니다.")
        _check(check_cancel)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def _extract(archive: Path, staging: Path, check_cancel=None) -> None:
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        prefix = f"ffmpeg-{VERSION}-essentials_build/"
        for name, (size, digest) in FILES.items():
            member = prefix + (f"bin/{name}" if name.endswith(".exe") else name)
            if names.count(member) != 1:
                raise RuntimeError(f"FFmpeg 압축 파일의 필수 항목이 올바르지 않습니다: {name}")
            info = bundle.getinfo(member)
            if info.file_size != size or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError(f"FFmpeg 압축 파일의 항목이 올바르지 않습니다: {name}")
            with bundle.open(info) as source, (staging / name).open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    _check(check_cancel)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not _matches(staging / name, size, digest, check_cancel):
                raise RuntimeError(f"FFmpeg 파일 무결성 검증에 실패했습니다: {name}")


def ensure_media_tools(*, progress: Progress | None = None,
                       check_cancel: Callable[[], None] | None = None,
                       cache_root: Path | None = None) -> Path:
    """Return an immutable verified installation, retaining the archive offline."""
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("FFmpeg 자동 설치는 Windows x64에서 지원합니다. FFmpeg와 FFprobe를 설치해 주세요.")
    _check(check_cancel)
    root = cache_root or writable_app_subdir("MekiCopy", "media-tools")
    root.mkdir(parents=True, exist_ok=True)
    installation = root / f"ffmpeg-{VERSION}-windows-x64"
    if progress:
        progress(0, "FFmpeg 설치를 확인하고 있습니다.")
    with exclusive_file_lock(root / ".install.lock", timeout=1800, check_cancel=check_cancel):
        _remove_abandoned_downloads(root, check_cancel)
        if _valid_installation(installation, check_cancel):
            return installation
        archive = root / ARCHIVE_NAME
        if not _matches(archive, ARCHIVE_SIZE, ARCHIVE_SHA256, check_cancel):
            _download(archive, progress=progress, check_cancel=check_cancel)
        if progress:
            progress(0.95, "FFmpeg 압축 해제 및 검증 중...")
        with tempfile.TemporaryDirectory(prefix=".ffmpeg-install-", dir=root) as temporary:
            staging = Path(temporary) / "payload"
            staging.mkdir()
            _extract(archive, staging, check_cancel)
            _check(check_cancel)
            # Keep a damaged generation recoverable until its replacement is published.
            backup = root / f".ffmpeg-damaged-{uuid.uuid4().hex}"
            moved = installation.exists()
            if moved:
                os.replace(installation, backup)
            try:
                os.replace(staging, installation)
            except BaseException:
                if moved:
                    os.replace(backup, installation)
                raise
            if moved:
                shutil.rmtree(backup, ignore_errors=True)
        if progress:
            progress(1, "FFmpeg 설치 완료")
        return installation
