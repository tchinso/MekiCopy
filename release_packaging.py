"""Build provenance and validation for matching Lite/Full distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from importlib.metadata import distributions


MANIFEST_NAME = "build-manifest.json"
MANIFEST_VERSION = 2
FULL_PAYLOAD_ROOTS = (
    "MekiCopy/_internal/runtime_models/",
    "MekiCopy/_internal/nvidia/",
    "MekiCopy/_internal/assets/ffmpeg/",
    "HYTrans/models/",
    "HYTrans/hdtrans/",
    "MekiAudioCapture/models/",
    "MagPie/",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



def verify_runtime_assets(assets_root: Path) -> None:
    """Verify browser modules and the WASM ABI they were built against."""
    manifest = json.loads((assets_root / "runtime_manifest.json").read_text(encoding="utf-8"))
    if manifest["transformers"]["buildOverride"]["onnxRuntimeWeb"] != manifest["onnxRuntimeWeb"]["version"]:
        raise ValueError("Transformers.js and ONNX Runtime Web versions disagree")
    required = {
        "transformers.min.js", "transformers.LICENSE.txt", "onnxruntime-web.LICENSE.txt",
        "onnxruntime-web.ThirdPartyNotices.txt", "worker.html", "worker.js",
        "translator_api_worker.html", "translator_api_worker.js",
    }
    required.update(
        f"wasm/ort-wasm-simd-threaded{variant}.{extension}"
        for variant in ("", ".asyncify", ".jsep", ".jspi") for extension in ("mjs", "wasm")
    )
    entries = manifest["files"]
    names = [entry["path"] for entry in entries]
    if len(set(names)) != len(names) or set(names) != required:
        raise ValueError("Browser runtime manifest has missing or duplicate assets")
    root = assets_root.resolve()
    for entry in entries:
        path = root / entry["path"]
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError(f"Browser runtime asset escapes its root: {entry['path']}")
        if path.stat().st_size != entry["size"] or sha256_file(path) != entry["sha256"]:
            raise ValueError(f"Browser runtime asset hash mismatch: {entry['path']}")


def browser_runtime_data(assets_root: Path) -> list[tuple[str, str]]:
    """Select verified browser files without collecting developer asset caches."""
    verify_runtime_assets(assets_root)
    manifest = json.loads((assets_root / "runtime_manifest.json").read_text(encoding="utf-8"))
    names = [entry["path"] for entry in manifest["files"]] + ["runtime_manifest.json"]
    return [(str(assets_root / name), (Path("assets") / Path(name).parent).as_posix())
            for name in names]


def verify_lite_optional_payloads(files: dict[str, Path]) -> None:
    """Reject optional native payloads in every companion, including flat DLLs."""
    manifest_path = Path(__file__).resolve().parent / "assets" / "gpu_runtime_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gpu_names = {Path(item["path"]).name.casefold()
                 for package in manifest["packages"] for item in package["files"]
                 if item["path"].endswith(".dll")}
    excluded_names = gpu_names | {"ffmpeg.exe", "ffprobe.exe"}
    for relative, path in files.items():
        parts = {part.casefold() for part in Path(relative).parts}
        if "nvidia" in parts or path.name.casefold() in excluded_names or is_full_payload(relative):
            raise ValueError(f"Lite contains a Full-only payload: {relative}")


def installed_dependencies() -> dict[str, str]:
    """Record resolved versions, including native and transitive dependencies."""
    return dict(sorted(
        (distribution.metadata["Name"].lower().replace("_", "-"), distribution.version)
        for distribution in distributions() if distribution.metadata["Name"]
    ))


def verify_media_tools(directory: Path) -> None:
    """Require the pinned FFmpeg pair together with its redistribution notices."""
    from media_tools_download import _valid_installation
    if not _valid_installation(directory):
        raise ValueError(f"FFmpeg tools or license files do not match the pinned release: {directory}")


def stage_optional_runtime(release_root: Path, ffmpeg_root: Path) -> None:
    """Attach verified native GPU and media payloads to a Full application."""
    from gpu_runtime import validate_gpu_runtime
    from media_tools_download import FILES as MEDIA_FILES

    resources = release_root / "MekiCopy" / "_internal"
    for distribution in distributions():
        name = (distribution.metadata["Name"] or "").lower().replace("_", "-")
        if not name.startswith("nvidia-"):
            continue
        for entry in distribution.files or ():
            relative = Path(str(entry))
            if relative.parts[0] == "nvidia" and relative.suffix.lower() == ".dll":
                destination = resources / relative
            elif "license" in relative.name.casefold():
                destination = resources / "nvidia" / "licenses" / name / relative.name
            else:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(distribution.locate_file(entry), destination)
    if not validate_gpu_runtime(resources):
        raise ValueError("Full GPU libraries do not match the pinned runtime manifest")
    media_target = resources / "assets" / "ffmpeg"
    media_target.mkdir(parents=True, exist_ok=True)
    for name, (size, digest) in MEDIA_FILES.items():
        source = ffmpeg_root / name
        if source.stat().st_size != size or sha256_file(source) != digest:
            raise ValueError(f"Full media tool does not match its pinned release: {name}")
        shutil.copy2(source, media_target / name)


def source_fingerprint(source_root: Path) -> str:
    """Cover runtime source, build definitions, and verified browser resources."""
    source_root = source_root.resolve()
    inputs = set(source_root.glob("*.py")) | set(source_root.glob("*.spec"))
    inputs.update((source_root / "hytrans").rglob("*.py"))
    inputs.update(
        source_root / filename
        for filename in ("build_mekicopy.ps1", "requirements-build.txt", "MekiCopy.ico")
    )
    # The browser runtime manifest already includes hashes of the large WASM
    # payloads. The build validates it against those files before packaging.
    inputs.update((source_root / "assets").glob("*.html"))
    inputs.update((source_root / "assets").glob("*.js"))
    inputs.update((source_root / "assets").glob("*.json"))
    digest = hashlib.sha256()
    for path in sorted(inputs, key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(sha256_file(path).encode("ascii") + b"\n")
    return digest.hexdigest()


def release_files(release_root: Path) -> dict[str, Path]:
    return {
        path.relative_to(release_root).as_posix(): path
        for path in release_root.rglob("*")
        if path.is_file() and path.relative_to(release_root).as_posix() != MANIFEST_NAME
    }


def is_full_payload(relative_path: str) -> bool:
    return any(relative_path.startswith(prefix) for prefix in FULL_PAYLOAD_ROOTS)


def verify_no_saved_api_settings(files: dict[str, Path]) -> None:
    for relative, path in files.items():
        if path.name.casefold().startswith("translation_api.json"):
            raise ValueError(f"Saved API settings must not be distributed: {relative}")
        if path.name.casefold() in {"hdtrans_settings.json", "realtime_ocr_state.json"}:
            raise ValueError(f"Saved user settings must not be distributed: {relative}")


def verify_hdtrans_payload(release_root: Path, flavor: str) -> dict | None:
    from hytrans.hdtrans_download import (
        ARCHIVE_SHA256, DICTIONARY_VERSION, SOURCE_COMMIT, VERSION,
        resolve_installation, validate_installation,
    )
    root = release_root / "HYTrans" / "hdtrans"
    if flavor == "Lite":
        if root.exists() and any(root.rglob("hdTransEngine*.dll")):
            raise ValueError("Lite must not contain a bundled hdTrans engine")
        return None
    installed = resolve_installation(root)
    if installed is None or not validate_installation(installed, rehash=True):
        raise ValueError("Full requires a verified, compatible hdTrans DLL/dictionary/source bundle")
    # Only official examples are packaged. Saved user files/configs belong in
    # the writable runtime store, never in a distributed Full engine generation.
    for user_file in installed.joinpath("hdTrans", "user").iterdir():
        if user_file.name not in {"hdtrans.config.example.yaml", "userdict.example.txt"}:
            raise ValueError(f"Saved hdTrans user settings must not be distributed: {user_file.name}")
    return {"version": VERSION, "dictionaryVersion": DICTIONARY_VERSION,
            "archiveSha256": ARCHIVE_SHA256, "sourceCommit": SOURCE_COMMIT,
            "license": "AGPL-3.0-or-later"}


def write_build_manifest(
    release_root: Path, source_root: Path, flavor: str, lite_root: Path | None = None,
    smoke_tested: bool = True,
) -> None:
    reusable = {}
    if lite_root is not None:
        reusable = json.loads((lite_root / MANIFEST_NAME).read_text(encoding="utf-8"))["files"]
    payload_files = release_files(release_root)
    verify_no_saved_api_settings(payload_files)
    if flavor == "Lite":
        verify_lite_optional_payloads(payload_files)
    hdtrans = verify_hdtrans_payload(release_root, flavor)
    files = {}
    for relative, path in sorted(payload_files.items()):
        info = path.stat()
        previous = reusable.get(relative)
        if previous and os.path.samefile(path, lite_root / relative):
            content_hash = previous["sha256"]
        else:
            content_hash = sha256_file(path)
        files[relative] = {
            "size": info.st_size,
            "mtimeNs": info.st_mtime_ns,
            "sha256": content_hash,
        }
    manifest = {
        "version": MANIFEST_VERSION,
        "flavor": flavor,
        "smokeTested": smoke_tested,
        "sourceFingerprint": source_fingerprint(source_root),
        "pythonVersion": list(sys.version_info[:3]),
        "dependencies": installed_dependencies(),
        "files": files,
        "hdTrans": hdtrans,
    }
    # A reused release may be hard-linked to Lite. Atomic replacement ensures
    # writing Full provenance never changes the original Lite manifest.
    target = release_root / MANIFEST_NAME
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def verify_lite_build(release_root: Path, source_root: Path) -> None:
    manifest = json.loads((release_root / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("version") != MANIFEST_VERSION or manifest.get("flavor") != "Lite":
        raise ValueError("Reuse requires a Lite release created by this build script")
    if manifest.get("smokeTested") is not True:
        raise ValueError("Lite executable smoke tests were skipped; build and verify Lite before reusing it")
    if manifest.get("sourceFingerprint") != source_fingerprint(source_root):
        raise ValueError("Lite runtime source changed; build Lite again before reusing it")
    if manifest.get("pythonVersion") != list(sys.version_info[:3]):
        raise ValueError("Lite was built with a different Python runtime")
    if manifest.get("dependencies") != installed_dependencies():
        raise ValueError("Lite build dependencies changed; build Lite again before reusing it")
    expected = manifest.get("files", {})
    actual = release_files(release_root)
    verify_no_saved_api_settings(actual)
    verify_lite_optional_payloads(actual)
    if not expected or set(actual) != set(expected):
        raise ValueError("Lite release file list changed since the verified build")
    for relative, path in actual.items():
        recorded = expected[relative]
        info = path.stat()
        if info.st_size != recorded["size"]:
            raise ValueError(f"Lite release file changed: {relative}")
        # Reuse the content hash from the completed build while immutable file
        # metadata matches. Rehash changed metadata instead of every CUDA DLL.
        if info.st_mtime_ns != recorded["mtimeNs"] and sha256_file(path) != recorded["sha256"]:
            raise ValueError(f"Lite release file changed: {relative}")


def verify_flavor_parity(lite_root: Path, full_root: Path) -> None:
    lite = release_files(lite_root)
    full = release_files(full_root)
    verify_no_saved_api_settings(lite)
    verify_no_saved_api_settings(full)
    verify_lite_optional_payloads(lite)
    missing = set(lite) - set(full)
    unexpected = {relative for relative in set(full) - set(lite) if not is_full_payload(relative)}
    if missing or unexpected:
        raise ValueError(f"Lite/Full runtime file lists differ: missing={sorted(missing)}, extra={sorted(unexpected)}")
    for relative, lite_path in lite.items():
        full_path = full[relative]
        if os.path.samefile(lite_path, full_path):
            continue
        if lite_path.stat().st_size != full_path.stat().st_size or sha256_file(lite_path) != sha256_file(full_path):
            raise ValueError(f"Lite/Full runtime contents differ: {relative}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("manifest", "verify-lite", "verify-parity", "verify-runtime", "verify-media", "stage-optional"))
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--lite", type=Path)
    parser.add_argument("--flavor", choices=("Lite", "Full"), default="Lite")
    parser.add_argument("--smoke-tested", action="store_true")
    parser.add_argument("--ffmpeg", type=Path)
    args = parser.parse_args()
    if args.action == "manifest":
        write_build_manifest(args.release, args.source, args.flavor, args.lite, args.smoke_tested)
    elif args.action == "verify-lite":
        verify_lite_build(args.release, args.source)
    elif args.action == "verify-runtime":
        verify_runtime_assets(args.release)
    elif args.action == "verify-media":
        try:
            verify_media_tools(args.release)
        except ValueError:
            raise SystemExit(1)
    elif args.action == "stage-optional":
        if args.ffmpeg is None:
            parser.error("--ffmpeg is required for stage-optional")
        stage_optional_runtime(args.release, args.ffmpeg)
    else:
        if args.lite is None:
            parser.error("--lite is required for verify-parity")
        verify_flavor_parity(args.lite, args.release)
    print(f"Release validation completed: {args.action}")


if __name__ == "__main__":
    main()
