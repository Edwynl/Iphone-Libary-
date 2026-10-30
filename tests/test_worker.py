from __future__ import annotations

import io
import json
import time
from PIL import Image

from ai_photos.catalog import PhotoCatalog
from ai_photos.worker import Worker


class DisabledEncoder:
    model_id = "not-installed"
    installed = False


def test_worker_json_protocol(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    Image.new("RGB", (30, 20), "yellow").save(photos / "sunset.jpg")
    sink = io.StringIO()
    worker = Worker(PhotoCatalog(tmp_path / "state"), DisabledEncoder(), sink)
    worker.handle({"id": 1, "action": "ping"})
    worker.handle({"id": 2, "action": "add_folder", "params": {"path": str(photos)}})
    worker.handle({"id": 3, "action": "scan"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        messages = [json.loads(line) for line in sink.getvalue().splitlines()]
        if any(item.get("id") == 3 for item in messages):
            break
        time.sleep(0.02)
    assert next(item for item in messages if item.get("id") == 1)["data"]["pong"]
    assert next(item for item in messages if item.get("id") == 3)["ok"]
    worker.handle({"id": 4, "action": "search", "params": {"query": "sunset"}})
    messages = [json.loads(line) for line in sink.getvalue().splitlines()]
    found = next(item for item in messages if item.get("id") == 4)
    assert found["data"]["mode"] == "filename"
    assert found["data"]["photos"][0]["name"] == "sunset.jpg"
    assert any(item.get("event") == "progress" for item in messages)


def test_worker_invalid_request(tmp_path):
    sink = io.StringIO()
    worker = Worker(PhotoCatalog(tmp_path / "state"), DisabledEncoder(), sink)
    worker.serve(["{invalid\n"])
    err = json.loads(sink.getvalue().splitlines()[0])
    assert not err["ok"]


def test_worker_remove_folder_does_not_delete_originals(tmp_path):
    root = tmp_path / "pic"
    root.mkdir()
    image = root / "picture.png"
    Image.new("RGB", (14, 18), "red").save(image)
    sink = io.StringIO()
    worker = Worker(PhotoCatalog(tmp_path / "state"), DisabledEncoder(), sink)
    worker.handle({"id": 1, "action": "add_folder", "params": {"path": str(root)}})
    worker.catalog.scan()
    worker.handle({"id": 2, "action": "remove_folder", "params": {"path": str(root)}})
    replies = [json.loads(x) for x in sink.getvalue().splitlines()]
    assert next(x for x in replies if x.get("id") == 2)["data"]["photos_forgotten"] == 1
    assert image.exists()
