"""One-shot manual-close notices for MekiCopy-owned GUI companions.

The main window is the only process that owns a :class:`subprocess.Popen`
handle, so a child cannot call its watchdog directly.  Each launch therefore
receives a unique, secret-bearing notice path.  A child writes the notice only
after the user confirms its close dialog; the parent consumes it before acting
on watchdog recovery requests.

The notice is intentionally per-launch.  A stale notice can never suppress
recovery for a later process with the same companion name.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from runtime_paths import writable_app_subdir


# Kept distinct from normal application failures and used as a fallback when
# the parent cannot read the one-shot notice (for example, a transient file
# system error during shutdown).
MANUAL_CLOSE_EXIT_CODE = 75
_NOTICE_VERSION = 1


@dataclass(frozen=True)
class ManualCloseSignal:
    """The authenticated, one-use signal assigned to one child launch."""

    path: Path
    token: str


def create_manual_close_signal(
    app_name: str,
    *,
    directory: str | Path | None = None,
) -> ManualCloseSignal:
    """Create a unique signal description without creating a notice file."""

    clean_name = str(app_name).strip()
    if not clean_name:
        raise ValueError("app_name is required")
    root = (
        Path(directory)
        if directory is not None
        else writable_app_subdir("MekiCopy", "watchdog")
    )
    root.mkdir(parents=True, exist_ok=True)
    filename = f"manual-close-{uuid.uuid4().hex}.json"
    return ManualCloseSignal(root / filename, secrets.token_urlsafe(32))


def manual_close_signal_arguments(signal: ManualCloseSignal) -> list[str]:
    """Return hidden companion CLI arguments for a signal assigned by parent."""

    return [
        "--watchdog-manual-close-file",
        str(signal.path),
        "--watchdog-manual-close-token",
        signal.token,
    ]


def publish_manual_close_signal(
    signal_file: str | Path | None,
    signal_token: str | None,
    *,
    app_name: str,
    process_id: int | None = None,
) -> bool:
    """Atomically publish a user-confirmed close intent, if one was assigned.

    Standalone companions do not receive a signal path and still close normally;
    returning ``False`` in that case is expected rather than an error.
    """

    if not signal_file or not signal_token:
        return False
    path = Path(signal_file)
    payload = {
        "version": _NOTICE_VERSION,
        "app": str(app_name),
        "pid": int(os.getpid() if process_id is None else process_id),
        "token": str(signal_token),
    }
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False, separators=(",", ":"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def consume_manual_close_signal(
    signal: ManualCloseSignal,
    *,
    app_name: str,
    process_id: int | None,
) -> bool:
    """Consume and validate a pending notice for the currently owned process."""

    try:
        raw = signal.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return False

    # This path belongs to one launch only.  Remove malformed/stale data too,
    # so a later recovery check cannot keep rereading it.
    try:
        signal.path.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or process_id is None:
        return False
    payload_token = payload.get("token")
    if not isinstance(payload_token, str):
        return False
    try:
        token_matches = secrets.compare_digest(payload_token, signal.token)
    except TypeError:
        return False
    return (
        payload.get("version") == _NOTICE_VERSION
        and payload.get("app") == str(app_name)
        and payload.get("pid") == int(process_id)
        and token_matches
    )


def discard_manual_close_signal(signal: ManualCloseSignal | None) -> None:
    """Best-effort cleanup for an unused one-shot signal."""

    if signal is None:
        return
    try:
        signal.path.unlink(missing_ok=True)
    except OSError:
        pass
