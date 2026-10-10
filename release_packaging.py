"""Build provenance and validation for matching Lite/Full distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


MANIFEST_NAME = "build-manifest.json"
MANIFEST_VERSION = 1
FULL_PAYLOAD_ROOTS = (
    "MekiCopy/_internal/runtime_models/",
    "HYTrans/models/",
    "MekiAudioCapture/models/",
    "MagPie/",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    inputs.add(source_root / "assets" / "runtime_manifest.json")
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


def write_build_manifest(
    release_root: Path, source_root: Path, flavor: str, lite_root: Path | None = None,
    smoke_tested: bool = True,
) -> None:
    reusable = {}
    if lite_root is not None:
        reusable = json.loads((lite_root / MANIFEST_NAME).read_text(encoding="utf-8"))["files"]
    payload_files = release_files(release_root)
    verify_no_saved_api_settings(payload_files)
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
        "files": files,
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
    expected = manifest.get("files", {})
    actual = release_files(release_root)
    verify_no_saved_api_settings(actual)
    if not expected or set(actual) != set(expected):
        raise ValueError("Lite release file list changed since the verified build")
    for relative, path in actual.items():
        if is_full_payload(relative):
            raise ValueError(f"Lite contains a Full-only payload: {relative}")
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
    parser.add_argument("action", choices=("manifest", "verify-lite", "verify-parity"))
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--lite", type=Path)
    parser.add_argument("--flavor", choices=("Lite", "Full"), default="Lite")
    parser.add_argument("--smoke-tested", action="store_true")
    args = parser.parse_args()
    if args.action == "manifest":
        write_build_manifest(args.release, args.source, args.flavor, args.lite, args.smoke_tested)
    elif args.action == "verify-lite":
        verify_lite_build(args.release, args.source)
    else:
        if args.lite is None:
            parser.error("--lite is required for verify-parity")
        verify_flavor_parity(args.lite, args.release)
    print(f"Release validation completed: {args.action}")


if __name__ == "__main__":
    main()
