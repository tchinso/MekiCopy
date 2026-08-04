from __future__ import annotations

import os
import re
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from runtime_paths import exclusive_file_lock

from .logging_setup import debug, error
from .model_files import (
    ModelFileSpec,
    ModelProfile,
    active_model_profile,
    is_complete_model,
    is_verified_model_file,
    record_verified_model_file,
    sha256_file,
)
from .paths import models_dir

_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


class ModelDownloadManager:
    """Download and atomically publish the selected pinned model."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._profile_key = active_model_profile().key
        self.state = "IDLE"
        self.current_file = ""
        self.downloaded_bytes = 0
        self.error_message = ""

    @staticmethod
    def _root_for(profile: ModelProfile) -> Path:
        return models_dir().joinpath(*profile.model_id.split("/"))

    @staticmethod
    def _total_for(profile: ModelProfile) -> int:
        return sum(spec.size for spec in profile.files.values())

    @property
    def profile(self) -> ModelProfile:
        return active_model_profile()

    @property
    def model_root(self) -> Path:
        return self._root_for(self.profile)

    @property
    def total_bytes(self) -> int:
        return self._total_for(self.profile)

    def _activate_profile_locked(self, profile: ModelProfile) -> None:
        if self._profile_key == profile.key:
            return
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("cannot change the HYTrans model during a download")
        self._profile_key = profile.key
        self._thread = None
        self.state = "IDLE"
        self.current_file = ""
        self.downloaded_bytes = 0
        self.error_message = ""

    def status(self) -> dict[str, object]:
        profile = self.profile
        model_root = self._root_for(profile)
        total_bytes = self._total_for(profile)

        with self._lock:
            self._activate_profile_locked(profile)
            should_verify = self.state != "DOWNLOADING"

        complete = (
            is_complete_model(model_root, profile)
            if should_verify
            else False
        )

        with self._lock:
            self._activate_profile_locked(profile)
            if self.state != "DOWNLOADING" and complete:
                self.state = "READY"
                self.downloaded_bytes = total_bytes
                self.current_file = ""
                self.error_message = ""
            return {
                "ok": self.state != "ERROR",
                "state": self.state,
                "currentFile": self.current_file or None,
                "downloadedBytes": self.downloaded_bytes,
                "totalBytes": total_bytes,
                "error": self.error_message or None,
                "modelKey": profile.key,
                "modelId": profile.model_id,
                "modelPath": str(model_root),
            }

    def start(self) -> dict[str, object]:
        profile = self.profile
        model_root = self._root_for(profile)
        total_bytes = self._total_for(profile)
        complete = is_complete_model(model_root, profile)

        with self._lock:
            self._activate_profile_locked(profile)
            if complete:
                self.state = "READY"
                self.downloaded_bytes = total_bytes
                self.current_file = ""
                self.error_message = ""
            elif self._thread is None or not self._thread.is_alive():
                self.state = "DOWNLOADING"
                self.current_file = ""
                self.downloaded_bytes = 0
                self.error_message = ""
                self._thread = threading.Thread(
                    target=self._download_all,
                    args=(profile,),
                    name=f"HYTransModelDownload-{profile.key}",
                    daemon=True,
                )
                self._thread.start()

        # Deliberately evaluate status after releasing _lock. The previous
        # implementation recursively acquired a non-reentrant Lock here and
        # deadlocked every first-run /model/prepare request.
        return self.status()

    def _set_progress(
        self,
        profile: ModelProfile,
        model_root: Path,
        relative_path: str,
        downloaded: int,
    ) -> None:
        completed = 0
        for candidate, spec in profile.files.items():
            if candidate == relative_path:
                break
            if is_verified_model_file(model_root, candidate, profile):
                completed += spec.size
        with self._lock:
            if self._profile_key != profile.key:
                return
            self.current_file = relative_path
            self.downloaded_bytes = min(
                self._total_for(profile),
                completed + max(0, downloaded),
            )

    def _download_all(self, profile: ModelProfile) -> None:
        model_root = self._root_for(profile)
        try:
            model_root.mkdir(parents=True, exist_ok=True)
            # A deterministic .part file is shared by retries. Serialize full
            # model preparation so two HYTrans instances cannot append to it.
            with exclusive_file_lock(
                model_root / ".hytrans-model-download.lock",
                timeout=6 * 60 * 60,
            ):
                self._ensure_download_space(profile, model_root)
                for relative_path, spec in profile.files.items():
                    if is_verified_model_file(model_root, relative_path, profile):
                        self._set_progress(
                            profile,
                            model_root,
                            relative_path,
                            spec.size,
                        )
                        continue
                    self._download_file(
                        profile,
                        model_root,
                        relative_path,
                        spec,
                    )
                if not is_complete_model(model_root, profile):
                    raise RuntimeError(
                        "downloaded model did not pass integrity verification"
                    )

            with self._lock:
                if self._profile_key == profile.key:
                    self.state = "READY"
                    self.current_file = ""
                    self.downloaded_bytes = self._total_for(profile)
                    self.error_message = ""
            debug("model_download_complete", str(model_root))
        except Exception as exc:
            with self._lock:
                if self._profile_key == profile.key:
                    self.state = "ERROR"
                    self.error_message = str(exc)
            error("model_download", exc)

    @staticmethod
    def _part_path(target: Path) -> Path:
        return target.with_name(f"{target.name}.part")

    @staticmethod
    def _remove_invalid_part(part: Path) -> None:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass

    @classmethod
    def _required_download_bytes(
        cls,
        profile: ModelProfile,
        model_root: Path,
    ) -> int:
        """Return the additional bytes needed for unverified model files."""
        required = 0
        for relative_path, spec in profile.files.items():
            if is_verified_model_file(model_root, relative_path, profile):
                continue
            part = cls._part_path(
                model_root.joinpath(*relative_path.split("/"))
            )
            try:
                part_size = part.stat().st_size
            except OSError:
                part_size = 0
            if part_size < 0 or part_size > spec.size:
                part_size = 0
            required += spec.size - part_size
        return required

    @classmethod
    def _ensure_download_space(
        cls,
        profile: ModelProfile,
        model_root: Path,
    ) -> None:
        required = cls._required_download_bytes(profile, model_root)
        if required <= 0:
            return
        free = shutil.disk_usage(model_root).free
        # Leave room for filesystem metadata, the manifest, and normal app
        # operation. This turns a late ENOSPC crash into a clear early error.
        reserve = max(128 * 1024 * 1024, required // 20)
        if free < required + reserve:
            needed_gib = (required + reserve) / (1024 ** 3)
            free_gib = free / (1024 ** 3)
            raise RuntimeError(
                "not enough free disk space for the HYTrans model: "
                f"need {needed_gib:.2f} GiB, available {free_gib:.2f} GiB"
            )

    def _publish_complete_part(
        self,
        profile: ModelProfile,
        model_root: Path,
        relative_path: str,
        spec: ModelFileSpec,
        target: Path,
        part: Path,
    ) -> bool:
        try:
            part_size = part.stat().st_size
        except FileNotFoundError:
            return False
        if part_size != spec.size:
            return False

        digest = sha256_file(part)
        if digest != spec.sha256:
            self._remove_invalid_part(part)
            return False
        os.replace(part, target)
        record_verified_model_file(
            model_root,
            relative_path,
            digest=digest,
            profile=profile,
        )
        return True

    @staticmethod
    def _valid_content_range(
        value: str,
        *,
        expected_start: int,
        expected_size: int,
    ) -> bool:
        match = _CONTENT_RANGE_RE.match(value.strip())
        if not match:
            return False
        start, end = int(match.group(1)), int(match.group(2))
        total_text = match.group(3)
        if start != expected_start or end < start:
            return False
        return total_text == "*" or int(total_text) == expected_size

    def _download_file(
        self,
        profile: ModelProfile,
        model_root: Path,
        relative_path: str,
        spec: ModelFileSpec,
    ) -> None:
        target = model_root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        part = self._part_path(target)

        try:
            part_size = part.stat().st_size
        except FileNotFoundError:
            part_size = 0
        if part_size > spec.size:
            self._remove_invalid_part(part)
            part_size = 0
        elif part_size == spec.size:
            if self._publish_complete_part(
                profile,
                model_root,
                relative_path,
                spec,
                target,
                part,
            ):
                self._set_progress(
                    profile,
                    model_root,
                    relative_path,
                    spec.size,
                )
                return
            part_size = 0

        encoded_path = quote(relative_path, safe="/")
        url = (
            f"https://huggingface.co/{profile.model_id}/resolve/"
            f"{profile.revision}/{encoded_path}?download=true"
        )

        # A 416 response or malformed Content-Range invalidates the resume
        # point. Retry once from byte zero; ordinary network failures retain the
        # deterministic .part so the next start can continue with Range.
        for attempt in range(2):
            try:
                resume_from = part.stat().st_size
            except FileNotFoundError:
                resume_from = 0
            if resume_from > spec.size:
                self._remove_invalid_part(part)
                resume_from = 0

            headers = {
                "Accept-Encoding": "identity",
                "User-Agent": "MekiCopy-HYTrans/1.0",
            }
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"
            request = urllib.request.Request(url, headers=headers)
            debug(
                "model_download_start",
                f"{relative_path}\nurl: {url}\nresume_from: {resume_from}",
            )
            self._set_progress(
                profile,
                model_root,
                relative_path,
                resume_from,
            )

            try:
                response_context = urllib.request.urlopen(request, timeout=120)
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and resume_from and attempt == 0:
                    self._remove_invalid_part(part)
                    continue
                raise

            restart_from_zero = False
            with response_context as response:
                status = int(getattr(response, "status", response.getcode()))
                append = resume_from > 0 and status == 206
                if status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not self._valid_content_range(
                        content_range,
                        expected_start=resume_from,
                        expected_size=spec.size,
                    ):
                        restart_from_zero = True
                elif status == 200:
                    # The host ignored Range. Truncate and accept a clean full
                    # response rather than appending duplicate bytes.
                    append = False
                    resume_from = 0
                else:
                    raise RuntimeError(
                        f"unexpected HTTP {status} for {relative_path}"
                    )

                if not restart_from_zero:
                    received = resume_from
                    oversized = False
                    with part.open("ab" if append else "wb") as output:
                        while True:
                            chunk = response.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            received += len(chunk)
                            if received > spec.size:
                                oversized = True
                                break
                            self._set_progress(
                                profile,
                                model_root,
                                relative_path,
                                received,
                            )
                    if oversized:
                        self._remove_invalid_part(part)
                        raise RuntimeError(
                            f"oversized {relative_path}: expected "
                            f"{spec.size} bytes, received more than expected"
                        )

            if restart_from_zero:
                self._remove_invalid_part(part)
                if attempt == 0:
                    continue
                raise RuntimeError(
                    f"invalid HTTP Content-Range while downloading {relative_path}"
                )

            try:
                received = part.stat().st_size
            except FileNotFoundError as exc:
                raise RuntimeError(f"download produced no file for {relative_path}") from exc
            if received != spec.size:
                # Preserve an incomplete but valid prefix for a later Range
                # retry, including when the remote end closed early.
                raise RuntimeError(
                    f"incomplete {relative_path}: expected {spec.size} bytes, "
                    f"received {received}"
                )

            digest = sha256_file(part)
            if digest != spec.sha256:
                self._remove_invalid_part(part)
                raise RuntimeError(f"checksum mismatch for {relative_path}")
            os.replace(part, target)
            record_verified_model_file(
                model_root,
                relative_path,
                digest=digest,
                profile=profile,
            )
            debug("model_download_file", f"{relative_path}\nbytes: {received}")
            return

        raise RuntimeError(f"failed to download {relative_path}")


model_download_manager = ModelDownloadManager()
