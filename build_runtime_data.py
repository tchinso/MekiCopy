"""Data required by the build interpreter's standard-library modules."""
from __future__ import annotations

from pathlib import Path
import ssl


def ssl_runtime_data(module_path: str | Path | None = None) -> list[tuple[str, str]]:
    """Preserve the certificate policy required by the Codex Python SSL module.

    This interpreter loads its adjacent policy PEM for every SSLContext. The
    frozen ssl module lives at the bundle root, so its PEM must live there too.
    Standard Python distributions without this file need no additional data.
    """
    policy = Path(module_path or ssl.__file__).resolve().with_name("codex-rejected-cas.pem")
    return [(str(policy), ".")] if policy.is_file() else []
