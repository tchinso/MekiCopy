from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from runtime_paths import writable_app_subdir


@dataclass(frozen=True)
class ModelFileSpec:
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelProfile:
    """Immutable description of one supported HYTrans model artifact."""

    key: str
    display_label: str
    model_id: str
    revision: str
    dtype: str
    prompt: str
    files: Mapping[str, ModelFileSpec]


def _immutable_files(
    files: dict[str, ModelFileSpec],
) -> Mapping[str, ModelFileSpec]:
    return MappingProxyType(dict(files))


MT2_PROFILE = ModelProfile(
    key="mt2",
    display_label="Hy-MT2 1.8B (q4f16)",
    model_id="tchinso/Hy-MT2-1.8B-onnx-q4f16",
    revision="6b6a4f12235342ed00ac089159c7192ea40bf6e8",
    dtype="q4f16",
    prompt=(
        "Translate the following text into {target}. Note that you should only "
        "output the translated result without any additional explanation:\n\n{text}"
    ),
    files=_immutable_files(
        {
            "chat_template.jinja": ModelFileSpec(
                654,
                "b7491ec0e9c869dfce20f2176758099bf248d979dd05530ede99deb21698acee",
            ),
            "config.json": ModelFileSpec(
                1_518,
                "c0621df26e008b9a9d3a288062aebd150169e25aa707400c58fecd410e20d8f8",
            ),
            "generation_config.json": ModelFileSpec(
                221,
                "0e28667f1cb4c7b880b9223b2d87978f88e79ed7ae037de1021f826c18d4ed6f",
            ),
            "special_tokens_map.json": ModelFileSpec(
                488,
                "bb9f59990034dae326581b9c62471523975417869f78a244b7ae2ce8cbb085eb",
            ),
            "tokenizer.json": ModelFileSpec(
                9_527_287,
                "b475bbef1b0b2fd57dcb865332b546475bd1ede2deb3bb91bafd0c047a8a530a",
            ),
            "tokenizer_config.json": ModelFileSpec(
                166_491,
                "273eea0d246839923aa90d4e376e4bce6ae9ad2ea82ff17c1db76a99d6a50e92",
            ),
            "onnx/model_q4f16.onnx": ModelFileSpec(
                1_373_443_906,
                "c0f5921fe143b05a420334392c59c175389d87dcfd60dc2554ca1c629eebec2a",
            ),
        }
    ),
)


MT15_PROFILE = ModelProfile(
    key="mt1.5",
    display_label="HY-MT1.5 1.8B (q4)",
    model_id="onnx-community/HY-MT1.5-1.8B-ONNX",
    revision="2f11819b25de08cecd344735cdfa5136ade41a67",
    dtype="q4",
    prompt=(
        "Translate the following segment into {target}, without additional "
        "explanation.\n\n{text}"
    ),
    files=_immutable_files(
        {
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
    ),
)


# These are stable user-setting/CLI keys, not Hugging Face repository IDs.
DEFAULT_MODEL_ID = MT2_PROFILE.key
SUPPORTED_MODEL_IDS = (MT2_PROFILE.key, MT15_PROFILE.key)
MODEL_PROFILES: Mapping[str, ModelProfile] = MappingProxyType(
    {
        MT2_PROFILE.key: MT2_PROFILE,
        MT15_PROFILE.key: MT15_PROFILE,
    }
)
MODEL_DISPLAY_LABELS: Mapping[str, str] = MappingProxyType(
    {key: profile.display_label for key, profile in MODEL_PROFILES.items()}
)

_MODEL_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "mt2": MT2_PROFILE.key,
        "hy-mt2": MT2_PROFILE.key,
        "hy-mt2-1.8b": MT2_PROFILE.key,
        MT2_PROFILE.model_id.casefold(): MT2_PROFILE.key,
        "mt1.5": MT15_PROFILE.key,
        "mt1_5": MT15_PROFILE.key,
        "mt15": MT15_PROFILE.key,
        "hy-mt1.5": MT15_PROFILE.key,
        "hy-mt1.5-1.8b": MT15_PROFILE.key,
        MT15_PROFILE.model_id.casefold(): MT15_PROFILE.key,
    }
)

_active_model_key = DEFAULT_MODEL_ID
_active_model_lock = threading.RLock()
MANIFEST_FILENAME = ".hytrans-model-manifest.json"
_manifest_lock = threading.Lock()


def normalize_model_id(model_id: object) -> str:
    """Normalize a setting, CLI key, alias, or repository ID to a stable key."""

    if isinstance(model_id, ModelProfile):
        return model_id.key
    normalized = str(model_id or "").strip().casefold()
    return _MODEL_ALIASES.get(normalized, DEFAULT_MODEL_ID)


def get_model_profile(model_id: object = DEFAULT_MODEL_ID) -> ModelProfile:
    return MODEL_PROFILES[normalize_model_id(model_id)]


def configure_model(model_id: object) -> ModelProfile:
    """Select the process-wide model before the HYTrans server starts."""

    global _active_model_key
    profile = get_model_profile(model_id)
    with _active_model_lock:
        _active_model_key = profile.key
    return profile


def active_model_profile() -> ModelProfile:
    with _active_model_lock:
        return MODEL_PROFILES[_active_model_key]


def _resolve_profile(profile: ModelProfile | str | None) -> ModelProfile:
    if profile is None:
        return active_model_profile()
    if isinstance(profile, ModelProfile):
        return profile
    return get_model_profile(profile)


def required_model_file_sizes(
    profile: ModelProfile | str | None = None,
) -> dict[str, int]:
    selected = _resolve_profile(profile)
    return {relative: spec.size for relative, spec in selected.files.items()}


def model_file_path(model_root: Path, relative_path: str) -> Path:
    return model_root.joinpath(*relative_path.split("/"))


def _manifest_paths(model_root: Path) -> list[Path]:
    try:
        identity = os.path.normcase(str(model_root.resolve()))
    except OSError:
        identity = os.path.normcase(os.path.abspath(str(model_root)))
    cache_key = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
    candidates = [
        model_root / MANIFEST_FILENAME,
        writable_app_subdir("HYTrans", "model-integrity") / f"{cache_key}.json",
    ]
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def _empty_manifest(profile: ModelProfile) -> dict[str, object]:
    return {
        "modelId": profile.model_id,
        "revision": profile.revision,
        "files": {},
    }


def _read_manifest(model_root: Path, profile: ModelProfile) -> dict[str, object]:
    merged = _empty_manifest(profile)
    merged_files = merged["files"]
    assert isinstance(merged_files, dict)
    entry_is_current: dict[str, bool] = {}
    for path in _manifest_paths(model_root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("revision") != profile.revision:
                continue
            manifest_model_id = payload.get("modelId")
            if manifest_model_id not in {None, profile.model_id}:
                continue
            if not isinstance(payload.get("files"), dict):
                continue
            for relative_path, entry in payload["files"].items():
                current = False
                spec = profile.files.get(relative_path)
                if isinstance(entry, dict) and spec is not None:
                    try:
                        stat = model_file_path(model_root, relative_path).stat()
                        current = (
                            entry.get("size") == stat.st_size == spec.size
                            and entry.get("mtimeNs") == stat.st_mtime_ns
                            and entry.get("sha256") == spec.sha256
                        )
                    except OSError:
                        current = False
                # Never let a stale cache entry shadow a current one from a
                # different writable/read-only manifest location.
                if relative_path not in merged_files or (
                    current and not entry_is_current.get(relative_path, False)
                ):
                    merged_files[relative_path] = entry
                    entry_is_current[relative_path] = current
        except (OSError, ValueError, TypeError):
            continue
    return merged


def _write_manifest(model_root: Path, payload: dict[str, object]) -> bool:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    for target in _manifest_paths(model_root):
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, target)
            return True
        except OSError:
            continue
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return False


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
    profile: ModelProfile | str | None = None,
) -> None:
    selected = _resolve_profile(profile)
    spec = selected.files.get(relative_path)
    path = model_file_path(model_root, relative_path)
    if spec is None or digest != spec.sha256 or not path.is_file():
        return
    try:
        stat = path.stat()
    except OSError:
        return
    if stat.st_size != spec.size:
        return

    with _manifest_lock:
        manifest = _read_manifest(model_root, selected)
        files = manifest.setdefault("files", {})
        assert isinstance(files, dict)
        files[relative_path] = {
            "size": stat.st_size,
            "mtimeNs": stat.st_mtime_ns,
            "sha256": digest,
        }
        _write_manifest(model_root, manifest)


def is_verified_model_file(
    model_root: Path,
    relative_path: str,
    profile: ModelProfile | str | None = None,
) -> bool:
    selected = _resolve_profile(profile)
    spec = selected.files.get(relative_path)
    if spec is None:
        return False
    path = model_file_path(model_root, relative_path)
    if not path.is_file():
        return False

    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size != spec.size:
        return False

    with _manifest_lock:
        manifest = _read_manifest(model_root, selected)
        files = manifest.get("files", {})
        entry = files.get(relative_path) if isinstance(files, dict) else None
        if (
            isinstance(entry, dict)
            and entry.get("size") == stat.st_size
            and entry.get("mtimeNs") == stat.st_mtime_ns
            and entry.get("sha256") == spec.sha256
        ):
            return True

    try:
        digest = sha256_file(path)
    except OSError:
        return False
    if digest != spec.sha256:
        return False
    record_verified_model_file(
        model_root,
        relative_path,
        digest=digest,
        profile=selected,
    )
    return True


def is_present_model_file(
    model_root: Path,
    relative_path: str,
    profile: ModelProfile | str | None = None,
) -> bool:
    selected = _resolve_profile(profile)
    spec = selected.files.get(relative_path)
    if spec is None:
        return False
    path = model_file_path(model_root, relative_path)
    try:
        return path.is_file() and path.stat().st_size == spec.size
    except OSError:
        return False


def is_present_model(
    model_root: Path,
    profile: ModelProfile | str | None = None,
) -> bool:
    selected = _resolve_profile(profile)
    return all(
        is_present_model_file(model_root, relative_path, selected)
        for relative_path in selected.files
    )


def is_complete_model(
    model_root: Path,
    profile: ModelProfile | str | None = None,
) -> bool:
    selected = _resolve_profile(profile)
    return all(
        is_verified_model_file(model_root, relative_path, selected)
        for relative_path in selected.files
    )


# Backward-compatible aliases describe the default profile only. Runtime code
# must call active_model_profile() rather than importing these static values.
MODEL_ID = MT2_PROFILE.model_id
MODEL_REVISION = MT2_PROFILE.revision
MODEL_FILE_SPECS = MT2_PROFILE.files
