from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


MODEL_ID = "onnx-community/HY-MT1.5-1.8B-ONNX"
MODEL_REVISION = "2f11819b25de08cecd344735cdfa5136ade41a67"
MANIFEST_FILENAME = ".hytrans-model-manifest.json"


@dataclass(frozen=True)
class ModelFileSpec:
    size: int
    sha256: str


# The revision is pinned above, so these values are stable.  Besides avoiding
# partial-file false positives, the digest lets HYTrans distinguish a complete
# cached model from a same-sized damaged file.
MODEL_FILE_SPECS: dict[str, ModelFileSpec] = {
    "config.json": ModelFileSpec(
        1_639,
        "391307b3bab6318ea6987071ccb82e1502aa83bc19f52a93428f7efb9466ecaa",
    ),
    "generation_config.json": ModelFileSpec(
        255,
        "d350fa7971cdf384def814c0f21ee35b89ae9ca8608f7b7a1203e3a6111d4f5c",
    ),
    "tokenizer.json": ModelFileSpec(
        8_672_322,
        "1b119fa76913d752f1003222865691e56f69fbff53b257acc10f8fd183d4f70b",
    ),
    "tokenizer_config.json": ModelFileSpec(
        1_172,
        "05b405b79c53d3616ee39e0387c300d4a631c168ff87c2d690c4334561340074",
    ),
    "onnx/model_q4.onnx": ModelFileSpec(
        448_829,
        "9ffed3b4d2321e42e4c79b970ee73ff80bd10fd4f89208a2b6ee6a5ef59b800c",
    ),
    "onnx/model_q4.onnx_data": ModelFileSpec(
        1_405_788_224,
        "ab975a558df7dbb1ed863d983e8d530e85eeb16de6f7028542b9c0af9a2222eb",
    ),
}

_manifest_lock = threading.Lock()


def required_model_file_sizes() -> dict[str, int]:
    return {relative: spec.size for relative, spec in MODEL_FILE_SPECS.items()}


def model_file_path(model_root: Path, relative_path: str) -> Path:
    return model_root.joinpath(*relative_path.split("/"))


def _manifest_path(model_root: Path) -> Path:
    return model_root / MANIFEST_FILENAME


def _read_manifest(model_root: Path) -> dict[str, object]:
    path = _manifest_path(model_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("revision") != MODEL_REVISION:
            return {"revision": MODEL_REVISION, "files": {}}
        if not isinstance(payload.get("files"), dict):
            return {"revision": MODEL_REVISION, "files": {}}
        return payload
    except (OSError, ValueError, TypeError):
        return {"revision": MODEL_REVISION, "files": {}}


def _write_manifest(model_root: Path, payload: dict[str, object]) -> None:
    model_root.mkdir(parents=True, exist_ok=True)
    target = _manifest_path(model_root)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def record_verified_model_file(
    model_root: Path,
    relative_path: str,
    *,
    digest: str,
) -> None:
    spec = MODEL_FILE_SPECS.get(relative_path)
    path = model_file_path(model_root, relative_path)
    if spec is None or digest != spec.sha256 or not path.is_file():
        return
    stat = path.stat()
    if stat.st_size != spec.size:
        return

    with _manifest_lock:
        manifest = _read_manifest(model_root)
        files = manifest.setdefault("files", {})
        assert isinstance(files, dict)
        files[relative_path] = {
            "size": stat.st_size,
            "mtimeNs": stat.st_mtime_ns,
            "sha256": digest,
        }
        _write_manifest(model_root, manifest)


def is_verified_model_file(model_root: Path, relative_path: str) -> bool:
    spec = MODEL_FILE_SPECS.get(relative_path)
    path = model_file_path(model_root, relative_path)
    if spec is None:
        return path.is_file() and path.stat().st_size > 0
    if not path.is_file():
        return False

    stat = path.stat()
    if stat.st_size != spec.size:
        return False

    with _manifest_lock:
        manifest = _read_manifest(model_root)
        files = manifest.get("files", {})
        entry = files.get(relative_path) if isinstance(files, dict) else None
        if (
            isinstance(entry, dict)
            and entry.get("size") == stat.st_size
            and entry.get("mtimeNs") == stat.st_mtime_ns
            and entry.get("sha256") == spec.sha256
        ):
            return True

    digest = sha256_file(path)
    if digest != spec.sha256:
        return False
    record_verified_model_file(model_root, relative_path, digest=digest)
    return True


def is_present_model_file(model_root: Path, relative_path: str) -> bool:
    spec = MODEL_FILE_SPECS.get(relative_path)
    path = model_file_path(model_root, relative_path)
    if not path.is_file():
        return False
    stat = path.stat()
    if spec is None:
        return stat.st_size > 0
    return stat.st_size == spec.size


def is_present_model(model_root: Path) -> bool:
    return all(
        is_present_model_file(model_root, relative_path)
        for relative_path in MODEL_FILE_SPECS
    )


def is_complete_model(model_root: Path) -> bool:
    return all(
        is_verified_model_file(model_root, relative_path)
        for relative_path in MODEL_FILE_SPECS
    )
