from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import uuid
from pathlib import Path

from runtime_paths import fallback_app_data_dirs, writable_app_data_dir, writable_app_subdir

_models_dir_cache: Path | None = None
_models_dir_lock = threading.RLock()
_assets_dir_cache: Path | None = None
_assets_dir_lock = threading.RLock()
_REQUIRED_RUNTIME_ASSETS = {
    "transformers.min.js",
    "transformers.LICENSE.txt",
    "onnxruntime-web.LICENSE.txt",
    "onnxruntime-web.ThirdPartyNotices.txt",
    "worker.html",
    "worker.js",
    "wasm/ort-wasm-simd-threaded.asyncify.mjs",
    "wasm/ort-wasm-simd-threaded.asyncify.wasm",
    "wasm/ort-wasm-simd-threaded.jsep.mjs",
    "wasm/ort-wasm-simd-threaded.jsep.wasm",
    "wasm/ort-wasm-simd-threaded.jspi.mjs",
    "wasm/ort-wasm-simd-threaded.jspi.wasm",
    "wasm/ort-wasm-simd-threaded.mjs",
    "wasm/ort-wasm-simd-threaded.wasm",
}

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root())).resolve()
    return app_root()


def _valid_runtime_assets(root: Path) -> bool:
    manifest_path = root / "runtime_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        transformers = manifest["transformers"]
        ort_web = manifest["onnxRuntimeWeb"]
        if (
            transformers.get("version") != "4.2.0"
            or transformers.get("buildOverride", {}).get("onnxRuntimeWeb") != "1.27.0"
            or ort_web.get("version") != "1.27.0"
        ):
            return False
        entries = manifest["files"]
        entry_paths = {str(entry["path"]) for entry in entries}
        if (
            len(entries) != len(_REQUIRED_RUNTIME_ASSETS)
            or entry_paths != _REQUIRED_RUNTIME_ASSETS
        ):
            return False
        resolved_root = root.resolve()
        for entry in entries:
            relative = Path(str(entry["path"]))
            if relative.is_absolute() or ".." in relative.parts:
                return False
            path = root / relative
            try:
                if not path.resolve().is_relative_to(resolved_root):
                    return False
            except OSError:
                return False
            if not path.is_file() or path.stat().st_size != int(entry["size"]):
                return False
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != str(entry["sha256"]).lower():
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def assets_dir() -> Path:
    """Prefer a complete external asset set, then the bundled resources."""
    global _assets_dir_cache
    with _assets_dir_lock:
        if _assets_dir_cache is not None:
            return _assets_dir_cache
        candidates = [app_root() / "assets", resource_root() / "assets"]
        for candidate in candidates:
            if _valid_runtime_assets(candidate):
                _assets_dir_cache = candidate
                return candidate
        # Preserve the program-folder-first diagnostic path when both sets are
        # missing or invalid; callers will report the concrete missing file.
        _assets_dir_cache = candidates[0]
        return _assets_dir_cache


def _ensure_writable_directory(path: Path) -> bool:
    probe = path / (
        f".write-test-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}"
    )
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="ascii")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def models_dir() -> Path:
    """Return HYTrans' durable, executable-adjacent model directory.

    A frozen HYTrans therefore always uses ``HYTrans/models`` when that
    directory is writable.  LocalAppData is only a last-resort fallback for
    read-only installations (for example, Program Files).  Most importantly,
    an existing LocalAppData cache must never make a writable executable-
    adjacent directory lose priority.
    """
    global _models_dir_cache
    with _models_dir_lock:
        if _models_dir_cache is not None:
            return _models_dir_cache

        external = app_root() / "models"
        if _ensure_writable_directory(external):
            _models_dir_cache = external
            return external

        # A complete portable model remains useful even when its directory is
        # read-only. Integrity metadata has its own writable fallback, so do
        # not force a multi-gigabyte re-download merely because the files were
        # copied from read-only media or installed under Program Files.
        try:
            from .model_files import active_model_profile, is_complete_model

            profile = active_model_profile()
            portable_model = external.joinpath(*profile.model_id.split("/"))
            if is_complete_model(portable_model, profile):
                _models_dir_cache = external
                return external
        except (OSError, RuntimeError, ValueError):
            pass

        # app_data_dir() normally selects the executable root first. If only
        # its child ``models`` is unusable (for example, a regular file with
        # that name), do not accidentally select the same broken path again.
        fallback_roots = [app_data_dir(), *fallback_app_data_dirs("HYTrans")]
        last_candidate = external
        seen: set[str] = {os.path.normcase(str(external))}
        for root in fallback_roots:
            candidate = root / "models"
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            last_candidate = candidate
            if _ensure_writable_directory(candidate):
                _models_dir_cache = candidate
                return candidate

        _models_dir_cache = last_candidate
        return last_candidate


def app_data_dir() -> Path:
    return writable_app_data_dir("HYTrans")


def chrome_profile_dir() -> Path:
    return writable_app_subdir("HYTrans", "ChromeProfile")


def log_dir(kind: str) -> Path:
    return writable_app_subdir("HYTrans", kind)
