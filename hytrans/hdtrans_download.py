"""Verified, pinned hdTrans bundles shared by portable Lite and Full editions.

Only the reviewed release below is installable. A newer GitHub release is
reported by the update check, but cannot silently change the C API or snapshot
format. Installations are immutable generations; changing active.json never
overwrites a DLL that another HYTrans process has loaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from runtime_paths import exclusive_file_lock, writable_app_subdir
from .paths import app_root

VERSION = "0.2.1"
TAG = "v0.2.1"
SOURCE_COMMIT = "afc752d4ccf2534b2fe206760435214902ce3c68"
DICTIONARY_VERSION = "0.2.1+c9ab89c732ad"
RELEASE_URL = "https://github.com/sokcuri/hdTrans/releases/tag/v0.2.1"
ARCHIVE_URL = "https://github.com/sokcuri/hdTrans/releases/download/v0.2.1/hdTrans-0.2.1-windows.zip"
ARCHIVE_SHA256 = "5825082c11fb96aab7492f1f11dac3119e440d1132f85605b95e838c2cc54fc3"
ARCHIVE_SIZE = 27582747
SOURCE_URL = "https://codeload.github.com/sokcuri/hdTrans/zip/refs/tags/v0.2.1"
SOURCE_SHA256 = "273e6f7d77fa3df75b5b0d9172b1ac0d5698e649645574f40103487c6052e7cd"
SOURCE_SIZE = 24739033
MANIFEST_NAME = "install-manifest.json"
SOURCE_NAME = "hdTrans-v0.2.1-source.zip"
Progress = Callable[[dict], None]

# Hashes of individual files extracted from the release ZIP whose published
# SHA256SUMS/GitHub asset digest is pinned above. These also detect disk damage
# and deliberately reject mixing snapshots or DLLs from different releases.
COMMON_FILES = {
    "hdTrans/dicts.hdsnap": (36085943, "d282a66d43bde0f9508596e25b7fb83dd5232c4720908dba0f816e03ceba1235"),
    "hdTrans/dicts.txt": (19, "5fa13cac88b1f2d4818ce418c9e42ecfa101fdb3a6e753608d5590ac9d7223a6"),
    "LICENSE": (34524, "8d56b405468aad11f87ab5763f901e276e08d9646ff5c8481b1762b6b789e9ed"),
    "THIRD-PARTY-NOTICES.txt": (13556, "54ce84c3871fac6ddccdfcbb7878af752eeff096e3c727b814aec6db6d1fbd51"),
    "hdTrans/user/hdtrans.config.example.yaml": (541, "6cc83803d9f30c675380ac022d0e054ea5365ee5eaebdeb8bd17a300e35f866f"),
    "hdTrans/user/userdict.example.txt": (653, "eafb14d2d554e6f2199dc9e7567543058c14b32043096a189e5202103c6a393c"),
    SOURCE_NAME: (SOURCE_SIZE, SOURCE_SHA256),
}
DLL_FILES = {
    "x64": ("hdTransEngine.dll", 4599296, "f8fcca95c38cc5e538c1a6e6d4c6ec3fce1d04d44e3a5c2c970b245cb6117a15"),
    "x86": ("hdTransEngine_x86.dll", 3820032, "c1d56ecb5b0c92750568321e0535e02b49db4f9a7220945b6c7f66cd2c3444de"),
}
_verified: dict[tuple[str, tuple], bool] = {}
_verify_lock = threading.RLock()
_install_roots: dict[str, Path] = {}


class DownloadCancelled(RuntimeError):
    pass


def architecture() -> str:
    return "x64" if struct.calcsize("P") == 8 else "x86"


def bundle_id() -> str:
    return f"{VERSION}-windows-{architecture()}"


def install_root() -> Path:
    # MekiCopy UI and HYTrans are separate frozen executables. Their shared
    # --api-config/settings location, rather than their different exe parents,
    # owns the writable installation. The packaged Full seed is still resolved
    # separately beside HYTrans.exe below.
    from .hdtrans_settings import hdtrans_settings_path
    configured = hdtrans_settings_path().parent / "hdtrans"
    key = str(configured.resolve())
    with _verify_lock:
        cached = _install_roots.get(key)
        if cached is not None:
            return cached
        try:
            configured.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=configured):
                pass
            selected = configured
        except OSError:
            selected = writable_app_subdir("HYTrans", "hdtrans")
        _install_roots[key] = selected
        return selected


def user_root() -> Path:
    """User files live outside immutable engine/snapshot generations."""
    root = install_root() / "user"
    root.mkdir(parents=True, exist_ok=True)
    return root


def expected_files(arch: str | None = None) -> dict[str, tuple[int, str]]:
    _, size, digest = DLL_FILES[arch or architecture()]
    return {**COMMON_FILES, "hdTransEngine.dll": (size, digest)}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_installation(path: Path, *, rehash: bool = False) -> bool:
    try:
        manifest = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
        if (manifest.get("version"), manifest.get("dictionaryVersion"), manifest.get("architecture"),
                manifest.get("archiveSha256"), manifest.get("sourceCommit")) != (
                VERSION, DICTIONARY_VERSION, architecture(), ARCHIVE_SHA256, SOURCE_COMMIT):
            return False
        metadata = []
        for relative, (size, _) in expected_files().items():
            file = path / relative
            info = file.stat()
            if info.st_size != size or file.is_symlink() or not file.is_file():
                return False
            metadata.append((relative, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        key = (str(path.resolve()), tuple(metadata))
        with _verify_lock:
            if not rehash and _verified.get(key):
                return True
        for relative, (_, digest) in expected_files().items():
            if _hash(path / relative) != digest:
                return False
        if (path / "hdTrans/dicts.txt").read_text(encoding="utf-8").strip() != DICTIONARY_VERSION:
            return False
        with _verify_lock:
            _verified[key] = True
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _selected(root: Path) -> Iterator[Path]:
    try:
        active = json.loads((root / "active.json").read_text(encoding="utf-8"))
        name = str(active["installation"])
        if name and Path(name).name == name and name not in {".", ".."}:
            yield root / "installations" / name
    except (OSError, ValueError, KeyError, TypeError):
        pass
    yield root / "installations" / bundle_id()
    # Failed updates must not hide a previously verified side-by-side install.
    try:
        yield from sorted((root / "installations").glob(f"{bundle_id()}-*"), reverse=True)
    except OSError:
        pass


def resolve_installation(root: Path | None = None) -> Path | None:
    roots = [Path(root)] if root is not None else [install_root(), *_bundled_roots()]
    seen = set()
    for candidate_root in roots:
        for candidate in _selected(candidate_root):
            key = str(candidate)
            if key not in seen and validate_installation(candidate):
                return candidate
            seen.add(key)
    return None


def _bundled_roots() -> list[Path]:
    # The main settings UI runs from MekiCopy/, while the bundled native seed
    # belongs to its HYTrans/ sibling. Either process can win first-install's
    # lock and must be able to prepare Full completely offline.
    executable_root = app_root()
    return [executable_root / "hdtrans", executable_root.parent / "HYTrans" / "hdtrans"]


def _bundled_installation() -> Path | None:
    for root in _bundled_roots():
        selected = resolve_installation(root)
        if selected is not None:
            return selected
    return None


def installation_status() -> dict:
    path = resolve_installation()
    return {
        "installed": path is not None,
        "ready": path is not None,
        "version": VERSION if path else None,
        "supportedVersion": VERSION,
        "dictionaryVersion": DICTIONARY_VERSION if path else None,
        "supportedDictionaryVersion": DICTIONARY_VERSION,
        "architecture": architecture(),
        "installationPath": str(path) if path else None,
        "dllPath": str(path / "hdTransEngine.dll") if path else None,
        "dictionaryPath": str(path / "hdTrans/dicts.hdsnap") if path else None,
        "releaseUrl": RELEASE_URL,
        "restartRequired": False,
    }


def _cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("hdTrans 설치가 취소되었습니다.")


def _report(callback: Progress | None, state: str, phase: str, **fields) -> None:
    if callback:
        callback({"state": state, "phase": phase, "version": VERSION,
                  "dictionaryVersion": DICTIONARY_VERSION, **fields})


@contextmanager
def _install_lock(root: Path, cancel_event: threading.Event | None) -> Iterator[None]:
    # Polling only the lock acquisition keeps cross-process waiting cancellable.
    while True:
        _cancelled(cancel_event)
        lock = exclusive_file_lock(root / ".install.lock", timeout=0.1)
        try:
            lock.__enter__()
            break
        except TimeoutError:
            continue
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _download(url: str, destination: Path, size: int, digest: str,
              cancel_event: threading.Event | None, progress: Progress | None) -> None:
    # Explicit retry belongs to the UI; transient network failures get two
    # short automatic retries and never touch the currently active install.
    for attempt in range(3):
        _cancelled(cancel_event)
        hasher = hashlib.sha256()
        downloaded = 0
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "MekiCopy-hdTrans/1"})
            with urllib.request.urlopen(request, timeout=15) as response, destination.open("wb") as output:
                if not response.geturl().startswith("https://"):
                    raise ValueError("hdTrans download was redirected away from HTTPS")
                while True:
                    _cancelled(cancel_event)
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > size:
                        raise ValueError("hdTrans 다운로드 크기가 공식 릴리스와 다릅니다.")
                    hasher.update(chunk)
                    output.write(chunk)
                    _report(progress, "downloading", destination.name, downloadedBytes=downloaded,
                            totalBytes=size, progress=downloaded / size)
            if downloaded != size or hasher.hexdigest() != digest:
                raise ValueError("hdTrans 다운로드 SHA-256 검증 실패; 설치를 적용하지 않았습니다.")
            return
        except (OSError, ValueError) as exc:
            destination.unlink(missing_ok=True)
            if attempt == 2 or isinstance(exc, ValueError):
                raise
            _report(progress, "downloading", f"네트워크 오류; 재시도 {attempt + 1}/2: {exc}")
            if cancel_event is not None:
                cancel_event.wait(0.5 * (attempt + 1))
            else:
                time.sleep(0.5 * (attempt + 1))


def _write_manifest(path: Path) -> None:
    payload = {
        "schemaVersion": 1, "version": VERSION, "dictionaryVersion": DICTIONARY_VERSION,
        "architecture": architecture(), "archiveSha256": ARCHIVE_SHA256,
        "sourceCommit": SOURCE_COMMIT, "releaseUrl": RELEASE_URL,
        "sourceUrl": SOURCE_URL, "sourceSha256": SOURCE_SHA256,
        "license": "AGPL-3.0-or-later", "files": {
            relative: {"size": size, "sha256": digest}
            for relative, (size, digest) in expected_files().items()
        },
    }
    (path / MANIFEST_NAME).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (path / "SOURCE.txt").write_text(
        f"hdTrans {TAG}\nCorresponding source (included): {SOURCE_NAME}\n"
        f"Upstream source: {SOURCE_URL}\nCommit: {SOURCE_COMMIT}\n"
        f"Release: {RELEASE_URL}\nLicense: AGPL-3.0-or-later (see LICENSE)\n"
        "Rebuild: see docs/build.md and CMakePresets.json in the included source.\n"
        "Windows release DLL statically links the MSVC CRT; no separate CRT DLL is needed.\n"
        "The dictionary snapshot and DLL must come from this same release.\n",
        encoding="utf-8",
    )


def _activate(root: Path, installation: Path) -> None:
    temporary = root / f".active-{uuid.uuid4().hex}.json"
    temporary.write_text(json.dumps({"installation": installation.name}) + "\n", encoding="utf-8")
    os.replace(temporary, root / "active.json")


def ensure_installation(cancel_event: threading.Event | None = None,
                        progress: Progress | None = None, *, force: bool = False,
                        root: Path | None = None, archive_path: Path | None = None,
                        source_path: Path | None = None, bundled_root: Path | None = None) -> Path:
    """Return a complete verified generation, installing on a worker if missing.

    ``force`` stages a new generation; callers restart HYTrans before using it.
    Offline existing installations are returned without contacting GitHub.
    ``root/archive_path/source_path`` also support reproducible Full packaging.
    """
    destination_root = Path(root) if root is not None else install_root()
    destination_root.mkdir(parents=True, exist_ok=True)
    with _install_lock(destination_root, cancel_event):
        _cancelled(cancel_event)
        existing = resolve_installation(destination_root)
        if existing is not None and not force:
            _report(progress, "ready", "검증된 hdTrans 설치 재사용", progress=1.0)
            return existing
        bundled = (resolve_installation(bundled_root) if bundled_root is not None else
                   _bundled_installation() if root is None else None)
        if bundled_root is not None and bundled is None:
            raise ValueError(f"검증된 hdTrans 번들을 찾을 수 없습니다: {bundled_root}")
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=destination_root) as temporary:
            work = Path(temporary)
            stage = work / "bundle"
            stage.mkdir()
            if bundled is not None and not force:
                _report(progress, "installing", "오프라인 Full 엔진을 쓰기 가능한 경로에 준비")
                for relative in (*expected_files(), MANIFEST_NAME, "SOURCE.txt"):
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(bundled / relative, target)
                    _cancelled(cancel_event)
            else:
                archive = Path(archive_path) if archive_path else work / "hdTrans-windows.zip"
                source = Path(source_path) if source_path else work / SOURCE_NAME
                if archive_path is None:
                    _download(ARCHIVE_URL, archive, ARCHIVE_SIZE, ARCHIVE_SHA256, cancel_event, progress)
                if source_path is None:
                    _download(SOURCE_URL, source, SOURCE_SIZE, SOURCE_SHA256, cancel_event, progress)
                _report(progress, "verifying", "공식 릴리스와 소스 SHA-256 확인")
                if archive.stat().st_size != ARCHIVE_SIZE or _hash(archive) != ARCHIVE_SHA256:
                    raise ValueError("hdTrans 공식 릴리스 SHA-256 불일치")
                if source.stat().st_size != SOURCE_SIZE or _hash(source) != SOURCE_SHA256:
                    raise ValueError("hdTrans 대응 소스 SHA-256 불일치")
                _cancelled(cancel_event)
                _report(progress, "extracting", "엔진·동일 버전 기본 사전 준비")
                with zipfile.ZipFile(archive) as package:
                    for relative in expected_files():
                        if relative == SOURCE_NAME:
                            continue
                        # Never extract archive paths. Only this allowlist is
                        # copied, and x86 C API gets the same canonical filename.
                        member = DLL_FILES[architecture()][0] if relative == "hdTransEngine.dll" else relative
                        target = stage / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with package.open(member) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        _cancelled(cancel_event)
                shutil.copyfile(source, stage / SOURCE_NAME)
                _write_manifest(stage)
            if not validate_installation(stage, rehash=True):
                raise ValueError("hdTrans 엔진·사전 버전 또는 파일 무결성 검증 실패")
            _cancelled(cancel_event)
            _report(progress, "installing", "검증된 설치 세대 활성화; 실행 중인 DLL은 유지")
            name = bundle_id()
            if (destination_root / "installations" / name).exists():
                name += f"-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
            installed = destination_root / "installations" / name
            installed.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, installed)
            # The previous active generation and user files are retained even
            # if writing this final pointer fails or the process is interrupted.
            _activate(destination_root, installed)
        _report(progress, "ready", "hdTrans 설치 완료", progress=1.0, restartRequired=force)
        return installed


def check_for_updates() -> dict:
    request = urllib.request.Request("https://api.github.com/repos/sokcuri/hdTrans/releases/latest",
                                     headers={"User-Agent": "MekiCopy-hdTrans/1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        latest = json.load(response)
    tag = str(latest.get("tag_name", ""))
    return {"installedVersion": installation_status()["version"], "supportedVersion": VERSION,
            "latestVersion": tag.removeprefix("v"), "updateAvailable": tag != TAG,
            "supported": tag == TAG, "releaseUrl": latest.get("html_url", RELEASE_URL),
            "message": "지원되는 최신 엔진입니다." if tag == TAG else
            "새 공식 릴리스가 있습니다. C API·사전 호환성을 검증한 MekiCopy 업데이트가 필요합니다."}


class HdTransDownloadManager:
    """UI-friendly background installation with cancellation and retry."""
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict = {"state": "idle", "phase": "", "error": None}

    def _progress(self, payload: dict) -> None:
        with self._lock:
            self._status.update(payload)

    def start(self, *, force: bool = False) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._cancel = threading.Event()
            self._status = {"state": "starting", "phase": "hdTrans 설치 확인", "error": None}
            def run() -> None:
                try:
                    ensure_installation(self._cancel, self._progress, force=force)
                except DownloadCancelled as exc:
                    self._progress({"state": "cancelled", "phase": str(exc)})
                except Exception as exc:
                    self._progress({"state": "error", "phase": "설치 실패; 기존 정상 설치는 보존됩니다.", "error": str(exc)})
            self._thread = threading.Thread(target=run, name="hdTrans-download", daemon=True)
            self._thread.start()
        return self.status()

    def cancel(self) -> dict:
        self._cancel.set()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            return {**installation_status(), **self._status,
                    "busy": self._thread is not None and self._thread.is_alive()}


_manager = HdTransDownloadManager()


def get_download_manager() -> HdTransDownloadManager:
    return _manager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-build", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--from-bundle-root", type=Path)
    args = parser.parse_args()
    previous_phase = [None]
    def progress(value: dict) -> None:
        phase = value.get("phase", "")
        if phase != previous_phase[0]:
            print(phase)
            previous_phase[0] = phase
    path = ensure_installation(root=args.prepare_build, archive_path=args.archive,
                               source_path=args.source_archive, bundled_root=args.from_bundle_root,
                               progress=progress)
    print(f"Verified hdTrans Full payload: {path}")


if __name__ == "__main__":
    main()
