"""Configuration and paths. No image content leaves the machine by default."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "WindowsAIPhotos"
MODEL_ID = "google/siglip2-base-patch16-224"
SUPPORTED_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif", ".heic", ".heif"
})
SKIP_DIRS = frozenset({".git", "node_modules", "$recycle.bin", "system volume information"})


def app_home() -> Path:
    override = os.environ.get("AI_PHOTOS_HOME")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def model_path() -> Path:
    return app_home() / "models" / "siglip2-base-patch16-224"
