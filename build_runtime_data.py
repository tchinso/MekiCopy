"""Data required by the build interpreter's standard-library modules."""
from __future__ import annotations

from pathlib import Path
import re
import ssl


def ssl_runtime_data(module_path: str | Path | None = None) -> list[tuple[str, str]]:
    """Preserve the certificate policy required by the Codex Python SSL module.

    This interpreter loads its adjacent policy PEM for every SSLContext. The
    frozen ssl module lives at the bundle root, so its PEM must live there too.
    Standard Python distributions without this file need no additional data.
    """
    policy = Path(module_path or ssl.__file__).resolve().with_name("codex-rejected-cas.pem")
    return [(str(policy), ".")] if policy.is_file() else []


def qt_license_data() -> list[tuple[str, str]]:
    directory = Path(__file__).resolve().parent / "assets" / "licenses" / "Qt"
    return [(str(path), "licenses/Qt") for path in sorted(directory.glob("*.txt"))]


def without_windows_icu(binaries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Let the Windows Qt wheels resolve ICU through System32.

    A private ICU picked up on the build PATH can shadow the OS library while
    exporting incompatible, versioned symbols.
    """
    pattern = re.compile(r"icu(?:dt|in|uc)\d*\.dll", re.IGNORECASE)
    return [
        entry for entry in binaries
        if not pattern.fullmatch(entry[0].replace("\\", "/").rsplit("/", 1)[-1])
    ]
