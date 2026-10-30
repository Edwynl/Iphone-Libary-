"""Reproduce scan/SQLite contention using generated photos and a blocked fake encoder."""
from datetime import datetime
from pathlib import Path
import hashlib
import http.client
import json
import sys
import threading
import time
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
sys.dont_write_bytecode = True
from PIL import Image
from ai_photos.catalog import PhotoCatalog
from ai_photos.mobile import MobileServer

base = Path(__file__).parent / "concurrency-data" / datetime.now().strftime("%Y%m%d-%H%M%S")
base.mkdir(parents=True, exist_ok=False)
cat = PhotoCatalog(base / "state")
server = MobileServer(cat, inbox=base / "imports", port=0)
server.start()


def request(method, path, token, body=b"", **headers):
    con = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    try:
        con.request(method, path, body, {"Authorization": "Bearer " + token, **headers})
        response = con.getresponse()
        return response.status, json.loads(response.read())
    finally:
        con.close()


released = threading.Event()
entered = threading.Event()
try:
    _, pair = request("POST", "/api/sync/v1/pair", server.token,
                      json.dumps({"device_id": "review-lock-device", "name": "Lock contention"}).encode())
    token = pair["device_token"]
    data = b"chunk-bytes-before-db-commit"
    digest = hashlib.sha256(data).hexdigest()
    _, info = request("POST", "/api/sync/v1/transfers", token,
                      json.dumps({"asset_id": "lock", "filename": "IMG.HEIC", "resource_type": "photo", "size": len(data), "sha256": digest}).encode())
    folder = base / "photos"
    folder.mkdir()
    Image.new("RGB", (8, 8), "red").save(folder / "photo.png")
    cat.add_folder(str(folder))

    class Encoder:
        model_id = "review-fake-blocking"
        def embed_images(self, paths):
            entered.set()
            assert released.wait(60)
            return [np.array([1., 0., 0.], dtype=np.float32) for _ in paths]

    thread = threading.Thread(target=lambda: cat.scan(Encoder()), daemon=True)
    thread.start()
    assert entered.wait(10)
    started = time.monotonic()
    try:
        status, reply = request("PUT", f"/api/sync/v1/transfers/{info['transfer_id']}/chunk", token, data,
                                 **{"X-Upload-Offset": "0", "X-Chunk-SHA256": digest})
        observation = {"http_status": status, "reply": reply}
    except http.client.RemoteDisconnected:
        observation = {"response": "RemoteDisconnected; no JSON error body"}
    finally:
        released.set()
        thread.join(10)
    observation["elapsed_sec"] = round(time.monotonic() - started, 2)
    _, state = request("GET", f"/api/sync/v1/transfers/{info['transfer_id']}", token)
    observation["db_offset"] = state["offset"]
    observation["stage_bytes"] = (server.sync_manager.staging / (info["transfer_id"] + ".part")).stat().st_size
    assert "response" in observation and state["offset"] == 0
    observation["reproduced"] = True
    (Path(__file__).parent / "concurrency-result.json").write_text(json.dumps(observation, indent=2), encoding="utf-8")
    print(json.dumps(observation), flush=True)
finally:
    released.set()
    server.close()
