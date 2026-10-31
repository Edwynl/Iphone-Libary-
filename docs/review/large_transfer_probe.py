"""Synthetic >4GiB HTTP upload, interruption/restart, full SHA verification.
No codec/PhotoKit correctness is inferred. Data remains in docs/review.
"""
from pathlib import Path
import hashlib
import http.client
import json
import os
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
sys.dont_write_bytecode = True
from ai_photos.catalog import PhotoCatalog
from ai_photos.mobile import MobileServer
from ai_photos.sync import CHUNK_BYTES

base = Path(__file__).parent / "large-transfer-data" / datetime.now().strftime("%Y%m%d-%H%M%S")
base.mkdir(parents=True, exist_ok=False)
cat = PhotoCatalog(base / "state")
server = MobileServer(cat, inbox=base / "imports", port=0)
server.start()


def request(method, path, body=b"", token="", **extra):
    con = http.client.HTTPConnection("127.0.0.1", server.port, timeout=180)
    headers = {"Authorization": "Bearer " + token, "Content-Length": str(len(body)), **extra}
    con.request(method, path, body, headers)
    response = con.getresponse()
    value = json.loads(response.read())
    con.close()
    assert 200 <= response.status < 300, (response.status, value)
    return value


started = time.monotonic()
try:
    pair = request("POST", "/api/sync/v1/pair", json.dumps({"device_id": "large-review-device", "name": "Synthetic 4GiB test"}).encode(), server.token)
    token = pair["device_token"]
    block = bytes(range(256)) * (CHUNK_BYTES // 256)
    tail = b"review-final-tail"
    sha = hashlib.sha256()
    for _ in range(512):
        sha.update(block)
    sha.update(tail)
    size = 512 * CHUNK_BYTES + len(tail)
    body = json.dumps({"asset_id": "large-synthetic-asset", "resource_type": "video", "filename": "REVIEW.MOV", "size": size, "sha256": sha.hexdigest()}).encode()
    state = request("POST", "/api/sync/v1/transfers", body, token)
    tid = state["transfer_id"]
    offset = 0
    block_sha = hashlib.sha256(block).hexdigest()
    resumed_offset = None
    for number in range(513):
        data = block if number < 512 else tail
        reply = request("PUT", f"/api/sync/v1/transfers/{tid}/chunk", data, token,
                        **{"X-Upload-Offset": str(offset), "X-Chunk-SHA256": block_sha if number < 512 else hashlib.sha256(tail).hexdigest()})
        offset += len(data)
        assert reply["offset"] == offset
        if number == 255:
            server.close()
            server = MobileServer(cat, inbox=base / "imports", port=0)
            server.start()
            recovered = request("POST", "/api/sync/v1/transfers", body, token)
            assert recovered["transfer_id"] == tid and recovered["offset"] == offset
            resumed_offset = offset
            print(json.dumps({"server_restart_resume_offset": offset}), flush=True)
        if number % 128 == 127:
            print(json.dumps({"bytes_acknowledged": offset, "elapsed_sec": round(time.monotonic() - started, 2)}), flush=True)
    assert offset == size
    done = request("POST", f"/api/sync/v1/transfers/{tid}/complete", token=token)
    file = next((base / "imports").rglob("*.mov"))
    verify = hashlib.sha256()
    with file.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            verify.update(part)
    assert verify.hexdigest() == sha.hexdigest() and file.stat().st_size == size
    result = {"passed": True, "synthetic_file_bytes": size, "chunks": 513, "restart_resume_offset": resumed_offset,
              "final_sha256": verify.hexdigest(), "status": done["status"], "elapsed_sec": round(time.monotonic() - started, 2),
              "limits": "Loopback HTTP + synthetic MOV bytes only. Not a real MOV, PhotoKit, Tailscale or OS reboot test."}
    (Path(__file__).parent / "large-transfer-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
finally:
    server.close()
