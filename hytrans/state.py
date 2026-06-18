from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class AppState:
    state: str = "STARTING"
    worker_ready: bool = False
    worker_connected: bool = False
    device: Optional[str] = None
    model: Optional[str] = None
    dtype: Optional[str] = None
    model_mode: Optional[str] = None
    warning: Optional[str] = None
    error: Optional[str] = None
    started_at: float = time.time()

    def as_ready_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ready": self.worker_ready,
            "state": self.state,
            "device": self.device,
            "model": self.model,
            "dtype": self.dtype,
            "modelMode": self.model_mode,
            "error": self.error,
        }
        if self.warning:
            payload["warning"] = self.warning
        return payload

