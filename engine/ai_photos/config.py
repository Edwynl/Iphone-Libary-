"""Configuration and paths. No image content leaves the machine by default."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "WindowsAIPhotos"
MODEL_ID = "google/siglip2-base-patch16-224"
MODEL_REVISION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
# RAW (iPhone ProRAW) is cataloged and backed up, previewed through Windows rather than decoded.
RAW_EXTENSIONS = frozenset({".dng"})
SUPPORTED_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif", ".heic", ".heif", ".avif"
}) | RAW_EXTENSIONS
# ".wap-importing" holds in-progress iPhone imports (see device_import.py).
SKIP_DIRS = frozenset({".git", "node_modules", "$recycle.bin", "system volume information", ".wap-importing"})


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
