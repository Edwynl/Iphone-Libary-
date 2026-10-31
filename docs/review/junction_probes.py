"""Review-only, reproducible Windows NTFS junction probes.

Only newly generated photos inside docs/review are modified. No cleanup/delete.
"""
from pathlib import Path
from datetime import datetime
import hashlib
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
sys.dont_write_bytecode = True
from PIL import Image
from ai_photos.catalog import PhotoCatalog
from ai_photos.backup import backup_local

base = Path(__file__).parent / "backup-junction-data" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
base.mkdir(parents=True, exist_ok=False)


def junction(path, target):
    # LiteralPath is not supported by New-Item; single-quoted constant local paths.
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    command = "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path " + quote(path) + " -Target " + quote(target) + " | Out-Null"
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], check=True)


for name, color in [("source-a", "red"), ("source-b", "blue")]:
    folder = base / name
    folder.mkdir()
    Image.new("RGB", (16, 16), color).save(folder / "original.png")
cat = PhotoCatalog(base / "state")
cat.add_folder(str(base / "source-a"))
cat.add_folder(str(base / "source-b"))
cat.scan()
target = base / "backup"
target.mkdir()
root_id = hashlib.sha256(str((base / "source-a").resolve()).encode()).hexdigest()[:16]
junction(target / root_id, base / "source-b")
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
original = base / "source-b/original.png"
before = sha(original)
result = backup_local(cat, target, apply=True)
after = sha(original)
assert before != after and after == sha(base / "source-a/original.png")
backup_result = {"original_b_before_sha256": before, "original_b_after_sha256": after,
                 "original_b_overwritten_with_a": True, "backup_result": result}
(Path(__file__).parent / "backup-junction-result.json").write_text(json.dumps(backup_result, indent=2), encoding="utf-8")

photos = base / "scan-photos"
outside = base / "outside"
photos.mkdir()
outside.mkdir()
Image.new("RGB", (8, 8), "red").save(outside / "private.png")
junction(photos / "redirect", outside)
scan_cat = PhotoCatalog(base / "scan-state")
scan_cat.add_folder(str(photos))
scan = scan_cat.scan()
assert scan["seen"] == 1
scan_result = {"scan": scan, "paths": [p["path"] for p in scan_cat.browse()],
               "junction_is_symlink": (photos / "redirect").is_symlink()}
(Path(__file__).parent / "junction-result.json").write_text(json.dumps(scan_result, indent=2), encoding="utf-8")
print(json.dumps({"backup_overwrite_reproduced": True, "scan_escape_reproduced": True}))
