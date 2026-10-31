"""Review-only reproductions. All data is generated inside docs/review.

PASS means the stated observation was reproduced, not that the product is correct.
No user media, cloud service, persistent port, or source modification is used.
"""
from __future__ import annotations

import ast
import errno
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sys
import threading
from datetime import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent / "reproduction-data" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
BASE.mkdir(parents=True, exist_ok=False)
os.environ["AI_PHOTOS_HOME"] = str(BASE / "default-state")

from PIL import Image
from ai_photos.catalog import PhotoCatalog
from ai_photos.mobile import MobileServer
from ai_photos.sync import SyncManager
from ai_photos.backup import backup_local, backup_s3
from ai_photos.duplicates import find_duplicates

results = []


def case(name):
    def decorate(fn):
        try:
            details = fn()
            results.append({"case": name, "observation_reproduced": True, "details": details})
        except Exception:
            results.append({"case": name, "observation_reproduced": False, "error": traceback.format_exc()})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        return fn
    return decorate


def setup(name):
    base = BASE / name
    if base.exists():
        raise RuntimeError("Use a fresh reproduction-data directory; no data is overwritten")
    cat = PhotoCatalog(base / "state")
    manager = SyncManager(cat, base / "imports")
    device = "review-device-12345"
    manager.pair(device, "Synthetic review device")
    return base, cat, manager, device


def transfer(manager, device, data, name="IMG.HEIC", kind="photo", asset="asset-1"):
    body = {"asset_id": asset, "resource_type": kind, "filename": name,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    info = manager.begin(device, body)
    manager.write_chunk(device, info["transfer_id"], 0, data, body["sha256"])
    manager.complete(device, info["transfer_id"])
    with manager.catalog.connection() as db:
        row = dict(db.execute("SELECT * FROM sync_transfers WHERE id=?", (info["transfer_id"],)).fetchone())
    return body, row


@case("R01_counterevidence_new_device_dashboard_counts_are_zero_not_null")
def dashboard_null():
    _, _, manager, _ = setup("dashboard-null")
    row = manager.dashboard()["devices"][0]
    assert row["completed"] == 0 and row["pending"] == 0
    return {k: row[k] for k in ("transfers", "completed", "pending")}


@case("R02_corrupt_completed_backup_still_reported_complete")
def corrupt_complete():
    _, _, manager, device = setup("corrupt-complete")
    body, row = transfer(manager, device, b"good-original")
    saved = Path(row["stored_path"])
    saved.write_bytes(b"bad!-original")
    retry = manager.begin(device, body)
    assert retry["status"] == "complete"
    assert hashlib.sha256(saved.read_bytes()).hexdigest() != body["sha256"]
    return {"retry_status": retry["status"], "stored_hash_matches": False}


@case("R03_complete_endpoint_acknowledges_missing_backup")
def missing_complete():
    _, _, manager, device = setup("missing-complete")
    body, row = transfer(manager, device, b"missing-backup")
    Path(row["stored_path"]).unlink()  # Generated fixture only.
    done = manager.complete(device, row["id"])
    assert done["status"] == "complete"
    retry = manager.begin(device, body)
    assert retry["status"] == "pending"
    return {"complete_endpoint": done["status"], "begin_endpoint": retry["status"]}


@case("R04_secondary_backups_omit_MOV_DNG_and_Live_Photo_relationships")
def omitted_backup():
    base, cat, manager, device = setup("omitted-backup")
    raw = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(raw, "PNG")
    transfer(manager, device, raw.getvalue(), "IMG.PNG", asset="live-1")
    transfer(manager, device, b"synthetic-paired-video", "IMG.MOV", "pairedVideo", "live-1")
    transfer(manager, device, b"synthetic-proraw", "IMG.DNG", "photo", "raw-1")
    local = backup_local(cat, base / "backup", apply=True)
    cloud = backup_s3(cat, bucket="not-contacted-review-dry-run")
    copied = list((base / "backup").rglob("*"))
    assert manager.dashboard()["totals"]["completed"] == 3
    assert local["files"] == 1 and cloud["objects"] == 1
    assert not any(p.suffix.lower() in {".mov", ".dng", ".db", ".sqlite"} for p in copied)
    return {"completed_resources": 3, "local_backup_files": local["files"],
            "s3_planned_objects": cloud["objects"], "backed_up_extensions": [p.suffix for p in copied if p.is_file()]}


@case("R05_cross_volume_finalize_has_no_copy_fallback")
def cross_volume():
    _, _, manager, device = setup("cross-volume")
    data = b"cross-volume-video"
    digest = hashlib.sha256(data).hexdigest()
    body = {"asset_id": "cross-volume", "resource_type": "video", "filename": "IMG.MOV", "size": len(data), "sha256": digest}
    info = manager.begin(device, body)
    manager.write_chunk(device, info["transfer_id"], 0, data, digest)
    with patch("ai_photos.sync.os.replace", side_effect=OSError(errno.EXDEV, "Injected cross-device link")):
        try:
            manager.complete(device, info["transfer_id"])
        except OSError as exc:
            assert exc.errno == errno.EXDEV
        else:
            raise AssertionError("Expected no cross-volume fallback")
    assert manager.status(device, info["transfer_id"])["status"] == "pending"
    return {"mode": "fault injection, not physical two-volume test", "status": "pending", "received_offset": len(data)}


@case("R06_same_bytes_different_original_filenames_create_duplicate_imports")
def duplicates_import():
    base, _, manager, device = setup("duplicate-imports")
    data = b"identical-original-bytes"
    transfer(manager, device, data, "IMG_A.HEIC", asset="asset-a")
    transfer(manager, device, data, "IMG_B.HEIC", asset="asset-b")
    files = list((base / "imports").rglob("*.heic"))
    assert len(files) == 2 and files[0].read_bytes() == files[1].read_bytes()
    return {"identical_content_physical_copies": 2}


@case("R07_perceptual_distance_four_pair_missed_by_four_band_prefilter")
def perceptual_false_negative():
    base, cat, _, _ = setup("dhash-bands")
    photos = base / "photos"
    photos.mkdir()
    for name, color in [("a.png", "red"), ("b.png", "blue")]:
        Image.new("RGB", (8, 8), color).save(photos / name)
    cat.add_folder(str(photos))
    cat.scan()
    def fake_hash(path):
        return 0 if path.name == "a.png" else sum(1 << (16 * i) for i in range(4))
    with patch("ai_photos.duplicates.dhash_file", side_effect=fake_hash):
        found = find_duplicates(cat, similar_distance=4)
    assert not found["similar_groups"]
    return {"true_hamming_distance": 4, "requested_threshold": 4, "groups_returned": 0,
            "mode": "controlled fingerprints; no real image perceptual-quality claim"}


@case("R08_installing_decoder_does_not_retry_unchanged_HEIC_thumbnail")
def decoder_retry():
    base, cat, _, _ = setup("decoder-retry")
    photos = base / "photos"
    photos.mkdir()
    (photos / "IMG.HEIC").write_bytes(b"synthetic-heic")
    cat.add_folder(str(photos))
    with patch.object(cat, "_thumb", return_value=("", 0, 0, "decoder missing")):
        cat.scan()
    with patch.object(cat, "_thumb", return_value=("now-decodable.jpg", 8, 8, None)) as retry:
        result = cat.scan()
        assert retry.call_count == 0
    assert cat.browse()[0]["error"] == "decoder missing"
    return {"decoder_retry_calls": 0, "updated": result["updated"], "old_error_retained": True}


@case("R09_concurrent_duplicate_chunk_is_serialized_without_corruption")
def concurrent_chunks():
    _, _, manager, device = setup("concurrent-chunks")
    data = b"abcdefgh"
    digest = hashlib.sha256(data).hexdigest()
    info = manager.begin(device, {"asset_id": "a", "resource_type": "photo", "filename": "IMG.HEIC", "size": len(data), "sha256": digest})
    barrier = threading.Barrier(2)
    def write():
        barrier.wait()
        try:
            manager.write_chunk(device, info["transfer_id"], 0, data, digest)
            return 200
        except Exception as exc:
            return getattr(exc, "status", -1)
    with ThreadPoolExecutor(2) as pool:
        statuses = sorted(pool.map(lambda _: write(), range(2)))
    assert statuses == [200, 409]
    manager.complete(device, info["transfer_id"])
    return {"statuses": statuses, "completed": True}


@case("R10_100GiB_rejected_and_reserved_windows_filename_accepted_until_finalize")
def reserved_name():
    _, _, manager, device = setup("reserved-name")
    from ai_photos.sync import SyncFailure, MAX_RESOURCE_BYTES
    data = b"reserved-name-test"
    digest = hashlib.sha256(data).hexdigest()
    body = {"asset_id": "a", "resource_type": "photo", "filename": "CON.HEIC", "size": len(data), "sha256": digest}
    info = manager.begin(device, body)
    manager.write_chunk(device, info["transfer_id"], 0, data, digest)
    # Final filename is suffixed with hash, so CON is NOT a reserved device name at finalization.
    assert manager.complete(device, info["transfer_id"])["status"] == "complete"
    body.update(asset_id="too-big", size=MAX_RESOURCE_BYTES + 1)
    try:
        manager.begin(device, body)
    except SyncFailure as exc:
        assert exc.status == 400
    else:
        raise AssertionError("Expected resource size rejection")
    return {"reserved_stem_is_safely_hash_suffixed": True, "oversize_rejected": True}


@case("R11_Python_AST_and_WinUI_XAML_event_handlers_are_consistent")
def static_contracts():
    import re
    import xml.etree.ElementTree as ET
    files = list((ROOT / "engine").rglob("*.py")) + list((ROOT / "tests").rglob("*.py"))
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    xaml = ET.parse(ROOT / "src/WindowsAIPhotos/MainWindow.xaml")
    handlers = []
    cs = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src/WindowsAIPhotos").glob("*.cs"))
    for element in xaml.iter():
        for attr, value in element.attrib.items():
            if attr in {"Click", "Loaded", "KeyDown", "ItemClick"}:
                assert re.search(r"\b" + re.escape(value) + r"\s*\(", cs), value
                handlers.append(value)
    assert (ROOT / "engine/ai_photos/mobile_web/index.html").exists()
    return {"python_files_parsed": len(files), "xaml_handler_count": len(handlers),
            "note": "Syntax/name consistency only; not a WinUI or Swift build"}


@case("R12_stale_catalog_entries_silently_omitted_from_backup_success")
def stale_backup():
    base, cat, _, _ = setup("stale-backup")
    photos = base / "photos"
    photos.mkdir()
    photo = photos / "original.png"
    Image.new("RGB", (8, 8), "red").save(photo)
    cat.add_folder(str(photos))
    cat.scan()
    Image.new("RGB", (90, 90), "blue").save(photo)
    result = backup_local(cat, base / "backup", apply=True)
    assert result["files"] == 0 and result["copied"] == 0
    assert not any("error" in key or "missing" in key or "stale" in key for key in result)
    return {"catalog_photos": cat.stats()["photos"], "backup_files": result["files"], "reported_stale_count": None}


report = {"note": "Observational review reproductions; product defects are intentionally asserted.",
          "passed_observations": sum(r["observation_reproduced"] for r in results), "total": len(results),
          "results": results}
(Path(__file__).resolve().parent / "reproduction-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
sys.exit(0 if all(r["observation_reproduced"] for r in results) else 1)
