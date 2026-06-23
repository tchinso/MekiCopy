from __future__ import annotations

import hashlib
import os
import threading
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote

from .logging_setup import debug, error
from .model_files import (
    MODEL_FILE_SPECS,
    MODEL_ID,
    MODEL_REVISION,
    is_complete_model,
    is_verified_model_file,
    record_verified_model_file,
)
from .paths import models_dir


class ModelDownloadManager:
    """Download and publish the pinned model directly into HYTrans/models."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = "IDLE"
        self.current_file = ""
        self.downloaded_bytes = 0
        self.total_bytes = sum(spec.size for spec in MODEL_FILE_SPECS.values())
        self.error_message = ""

    @property
    def model_root(self) -> Path:
        return models_dir().joinpath(*MODEL_ID.split("/"))

    def status(self) -> dict[str, object]:
        with self._lock:
            if self.state != "DOWNLOADING" and is_complete_model(self.model_root):
                self.state = "READY"
                self.downloaded_bytes = self.total_bytes
                self.current_file = ""
                self.error_message = ""
            return {
                "ok": self.state != "ERROR",
                "state": self.state,
                "currentFile": self.current_file or None,
                "downloadedBytes": self.downloaded_bytes,
                "totalBytes": self.total_bytes,
                "error": self.error_message or None,
                "modelPath": str(self.model_root),
            }

    def start(self) -> dict[str, object]:
        with self._lock:
            if is_complete_model(self.model_root):
                self.state = "READY"
                self.downloaded_bytes = self.total_bytes
                self.current_file = ""
                self.error_message = ""
            elif self._thread is None or not self._thread.is_alive():
                self.state = "DOWNLOADING"
                self.error_message = ""
                self._thread = threading.Thread(
                    target=self._download_all,
                    name="HYTransModelDownload",
                    daemon=True,
                )
                self._thread.start()
        return self.status()

    def _set_progress(self, relative_path: str, downloaded: int) -> None:
        completed = 0
        for candidate, spec in MODEL_FILE_SPECS.items():
            if candidate == relative_path:
                break
            if is_verified_model_file(self.model_root, candidate):
                completed += spec.size
        with self._lock:
            self.current_file = relative_path
            self.downloaded_bytes = min(self.total_bytes, completed + downloaded)

    def _download_all(self) -> None:
        try:
            self.model_root.mkdir(parents=True, exist_ok=True)
            for relative_path in MODEL_FILE_SPECS:
                if is_verified_model_file(self.model_root, relative_path):
                    self._set_progress(relative_path, MODEL_FILE_SPECS[relative_path].size)
                    continue
                self._download_file(relative_path)
            if not is_complete_model(self.model_root):
                raise RuntimeError("downloaded model did not pass integrity verification")
            with self._lock:
                self.state = "READY"
                self.current_file = ""
                self.downloaded_bytes = self.total_bytes
            debug("model_download_complete", str(self.model_root))
        except Exception as exc:
            with self._lock:
                self.state = "ERROR"
                self.error_message = str(exc)
            error("model_download", exc)

    def _download_file(self, relative_path: str) -> None:
        spec = MODEL_FILE_SPECS[relative_path]
        target = self.model_root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
        encoded_path = quote(relative_path, safe="/")
        url = (
            f"https://huggingface.co/{MODEL_ID}/resolve/"
            f"{MODEL_REVISION}/{encoded_path}?download=true"
        )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "MekiCopy-HYTrans/1.0"},
        )
        digest = hashlib.sha256()
        received = 0
        debug("model_download_start", f"{relative_path}\nurl: {url}")
        try:
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                while True:
                    chunk = response.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    self._set_progress(relative_path, received)
            if received != spec.size:
                raise RuntimeError(
                    f"incomplete {relative_path}: expected {spec.size} bytes, received {received}"
                )
            actual_digest = digest.hexdigest()
            if actual_digest != spec.sha256:
                raise RuntimeError(f"checksum mismatch for {relative_path}")
            os.replace(temporary, target)
            record_verified_model_file(
                self.model_root,
                relative_path,
                digest=actual_digest,
            )
            debug("model_download_file", f"{relative_path}\nbytes: {received}")
        finally:
            temporary.unlink(missing_ok=True)


model_download_manager = ModelDownloadManager()
