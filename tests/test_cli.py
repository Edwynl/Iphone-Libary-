from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image


def test_local_cli_roundtrip_without_ai_or_cloud(tmp_path):
    library = tmp_path / "photos"
    library.mkdir()
    Image.new("RGB", (44, 34), "orange").save(library / "sunset.jpg")
    env = os.environ.copy()
    env["AI_PHOTOS_HOME"] = str(tmp_path / "catalog-state")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "engine")

    def run(*args):
        done = subprocess.run([sys.executable, "-m", "ai_photos.cli", *args],
                              env=env, text=True, capture_output=True, timeout=20)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    assert run("status")["ai_installed"] is False
    assert run("add-folder", str(library))["added"]
    assert run("scan")["seen"] == 1
    response = run("search", "sunset")
    assert response["mode"] == "filename"
    assert response["photos"][0]["name"] == "sunset.jpg"
