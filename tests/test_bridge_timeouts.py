"""The desktop bridge must not give up on engine jobs that legitimately run for hours."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _long_actions():
    bridge = (ROOT / 'src/WindowsAIPhotos/Services/PythonEngineBridge.cs').read_text(encoding='utf-8')
    clause = re.search(r'var timeout = action is (.+?)\?\s*TimeSpan\.FromHours', bridge, re.DOTALL)
    return set(re.findall(r'"(\w+)"', clause.group(1)))


def _background_actions():
    worker = (ROOT / 'engine/ai_photos/worker.py').read_text(encoding='utf-8')
    clause = re.search(r'if action in \((.+?)\):\s*self\._background', worker, re.DOTALL)
    return set(re.findall(r'"(\w+)"', clause.group(1)))


def test_every_background_job_gets_the_long_timeout():
    background = _background_actions()
    assert 'device_import_commit' in background
    assert background <= _long_actions()
