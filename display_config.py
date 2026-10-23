"""Normalization shared by the transcript and translation display windows."""
from __future__ import annotations

from dataclasses import fields
import math
from typing import Any


def normalize_font_name(value: object) -> str:
    return str(value).strip().lstrip("@").strip() or "Malgun Gothic"


def apply_display_config(config: Any, data: dict[str, Any]) -> None:
    """Validate all supplied fields before changing a display configuration."""
    updates = {}
    for field in fields(config):
        key = field.name
        if key not in data:
            continue
        value = data[key]
        if isinstance(getattr(config, key), bool):
            value = bool(value)
        elif key == "opacity":
            value = float(value)
            if not math.isfinite(value):
                raise ValueError("opacity must be finite")
            value = max(0.1, min(1.0, value))
        elif key.endswith("_size"):
            value = max(8, min(96, int(value)))
        elif key.endswith("_font"):
            value = normalize_font_name(value)
        else:
            value = str(value)
        updates[key] = value
    for key, value in updates.items():
        setattr(config, key, value)


def validate_display_colors(root: Any, data: dict[str, Any]) -> None:
    """Resolve Tk colors on the UI thread before applying any settings."""
    for key, value in data.items():
        if key.endswith("_color"):
            root.winfo_rgb(str(value))
