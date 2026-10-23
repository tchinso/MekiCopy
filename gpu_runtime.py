"""Install the optional NVIDIA runtime from verified PyPI wheels."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from importlib.metadata import PackageNotFoundError, distribution
from typing import Callable

from runtime_paths import exclusive_file_lock, writable_app_subdir

Progress = Callable[[str], None]


class _CallbackError(Exception):
    def __init__(self, error: Exception) -> None:
        self.error = error


@contextmanager
def _install_lock(cache: Path, progress: Progress | None):
    deadline = time.monotonic() + 3600
    notified = 0.0
    while True:
        lock = exclusive_file_lock(cache / ".install.lock", timeout=0.2)
        try:
            lock.__enter__()
            break
        except TimeoutError:
            now = time.monotonic()
            if now >= deadline:
                raise
            if progress and now - notified >= 1:
                progress("다른 프로세스의 GPU 구성요소 준비를 기다리는 중")
                notified = now
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value or not path.parts:
        raise ValueError(f"Invalid GPU runtime path: {value}")
    return value


def _load_manifest(resource_root: Path) -> tuple[dict, str]:
    raw = (resource_root / "assets" / "gpu_runtime_manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schemaVersion") != 1 or manifest.get("platform") != "win_amd64":
        raise ValueError("Unsupported GPU runtime manifest")
    seen: set[str] = set()
    if not manifest.get("packages"):
        raise ValueError("GPU runtime manifest is empty")
    for package in manifest["packages"]:
        if not package["url"].startswith("https://files.pythonhosted.org/"):
            raise ValueError("GPU runtime wheel must come from PyPI")
        for item in package["files"]:
            _relative(item["member"])
            relative = _relative(item["path"])
            if relative in seen or not relative.startswith(("nvidia/", "licenses/")):
                raise ValueError(f"Invalid or duplicate GPU runtime file: {relative}")
            seen.add(relative)
    return manifest, hashlib.sha256(raw).hexdigest()[:24]


def _valid_file(path: Path, item: dict) -> bool:
    try:
        return (path.is_file() and not path.is_symlink()
                and path.stat().st_size == item["size"] and _sha256(path) == item["sha256"])
    except OSError:
        return False


def _valid_installation(root: Path, manifest: dict, *, bundled: bool = False) -> bool:
    for package in manifest["packages"]:
        for item in package["files"]:
            if bundled and not item["path"].endswith(".dll"):
                continue
            path = root / item["path"]
            if not path.resolve().is_relative_to(root.resolve()) or not _valid_file(path, item):
                return False
    return True


def _dll_directories(root: Path, manifest: dict) -> list[Path]:
    return sorted({(root / item["path"]).parent for package in manifest["packages"]
                   for item in package["files"] if item["path"].endswith(".dll")})


def _download(package: dict, destination: Path, progress: Progress | None) -> None:
    for attempt in range(3):
        if progress:
            progress(f"GPU 구성요소 다운로드: {package['package']} ({attempt + 1}/3)")
        try:
            request = urllib.request.Request(package["url"], headers={"User-Agent": "MekiCopy/1"})
            size = 0
            notified = 0.0
            with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as target:
                if not response.geturl().startswith("https://"):
                    raise ValueError("GPU runtime download redirected away from HTTPS")
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > package["size"]:
                        raise ValueError("GPU runtime download exceeds the expected size")
                    target.write(chunk)
                    now = time.monotonic()
                    if progress and (now - notified >= 1 or size == package["size"]):
                        try:
                            progress(f"GPU {package['package']}: {size / package['size']:.0%} "
                                     f"({size / 1048576:.0f}/{package['size'] / 1048576:.0f} MiB)")
                        except Exception as error:
                            raise _CallbackError(error) from error
                        notified = now
            if not _valid_file(destination, package):
                raise ValueError("GPU runtime wheel checksum mismatch")
            return
        except _CallbackError as error:
            destination.unlink(missing_ok=True)
            raise error.error
        except (OSError, ValueError, http.client.HTTPException):
            destination.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))


def _extract(wheel: Path, root: Path, package: dict, progress: Progress | None = None) -> None:
    notified = 0.0
    with zipfile.ZipFile(wheel) as archive:
        for item in package["files"]:
            member = archive.getinfo(item["member"])
            if member.file_size != item["size"]:
                raise ValueError("GPU runtime wheel member size mismatch")
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    now = time.monotonic()
                    if progress and now - notified >= 1:
                        progress(f"GPU 구성요소 압축 해제: {package['package']}")
                        notified = now
            if not _valid_file(target, item):
                raise ValueError(f"GPU runtime DLL checksum mismatch: {item['path']}")


def _remove_abandoned_staging(cache: Path) -> None:
    """Remove interrupted installs while holding the exclusive install lock."""
    root = cache.resolve()
    for candidate in root.glob(".staging-*"):
        if (candidate.is_symlink() or candidate.is_junction()
                or not candidate.resolve().is_relative_to(root)):
            continue
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
        except OSError:
            # Antivirus scanners can briefly retain a handle after process exit.
            continue


def validate_gpu_runtime(resource_root: Path, runtime_root: Path | None = None) -> bool:
    """Verify all bundled DLLs against the resource manifest without downloading."""
    manifest, _ = _load_manifest(Path(resource_root))
    return _valid_installation(Path(runtime_root or resource_root), manifest, bundled=True)


def prepare_gpu_runtime(resource_root: Path, progress: Progress | None = None) -> list[Path]:
    """Return verified DLL directories, downloading only when no local set exists.

    Call on a worker thread after detecting a usable NVIDIA driver. Download
    and validation errors propagate to the caller so it can select CPU OCR.
    """
    resource_root = Path(resource_root)
    manifest, generation = _load_manifest(resource_root)
    if _valid_installation(resource_root, manifest, bundled=True):
        return _dll_directories(resource_root, manifest)
    if not getattr(sys, "frozen", False):
        try:
            installed_packages = Path(distribution("nvidia-cuda-runtime").locate_file(""))
        except PackageNotFoundError:
            pass
        else:
            if _valid_installation(installed_packages, manifest, bundled=True):
                return _dll_directories(installed_packages, manifest)
    cache = writable_app_subdir("MekiCopy", "gpu-runtime")
    cache.mkdir(parents=True, exist_ok=True)
    # Large wheels may take several minutes on a slow connection.
    with _install_lock(cache, progress):
        _remove_abandoned_staging(cache)
        for installed in sorted(cache.glob(f"{generation}-*"), reverse=True):
            if _valid_installation(installed, manifest):
                return _dll_directories(installed, manifest)
        if progress:
            progress("GPU 구성요소 준비 중 (최초 실행 시 다운로드)")
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=cache) as temporary:
            work = Path(temporary)
            stage = work / "runtime"
            stage.mkdir()
            for package in manifest["packages"]:
                wheel = work / "package.whl"
                _download(package, wheel, progress)
                _extract(wheel, stage, package, progress)
                wheel.unlink()
            installed = cache / f"{generation}-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
            os.replace(stage, installed)
        if progress:
            progress("GPU 구성요소 준비 완료")
        return _dll_directories(installed, manifest)
