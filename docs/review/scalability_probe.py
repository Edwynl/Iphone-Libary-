"""Synthetic 50k/100k SQLite/NumPy workload, not a real photo/AI benchmark."""
from datetime import datetime
from pathlib import Path
import ctypes
from ctypes import wintypes
import json
import sys
import time
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
sys.dont_write_bytecode = True
from ai_photos.catalog import PhotoCatalog

base = Path(__file__).parent / "scalability-data" / datetime.now().strftime("%Y%m%d-%H%M%S")
base.mkdir(parents=True, exist_ok=False)
cat = PhotoCatalog(base / "state")
source = base / "synthetic-source"
source.mkdir()
cat.add_folder(str(source))
dimension = 768
vector = np.ones(dimension, dtype=np.float32)
vector /= np.linalg.norm(vector)
blob = vector.tobytes()


class Encoder:
    model_id = "review-synthetic-768"
    def embed_text(self, query):
        return vector.copy()


class Memory(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def memory():
    counters = Memory()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = ctypes.windll.kernel32.GetCurrentProcess()
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    assert ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb)
    return {"working_set_mib": round(counters.WorkingSetSize / 1024**2, 2),
            "peak_working_set_mib": round(counters.PeakWorkingSetSize / 1024**2, 2)}


measurements = []
previous = 0
for count in [50_000, 100_000]:
    started = time.monotonic()
    with cat.connection() as db:
        for index in range(previous + 1, count + 1):
            name = f"photo-{index:06}.jpg"
            db.execute("INSERT INTO photos(id,path,folder,name,size,mtime_ns,indexed_at) VALUES(?,?,?,?,1,1,'review')",
                       (index, str(source / name), str(source), name))
            db.execute("INSERT INTO embeddings(photo_id,model_id,dimensions,vector) VALUES(?,?,?,?)",
                       (index, Encoder.model_id, dimension, blob))
    insertion = time.monotonic() - started
    item = {"rows": count, "dimensions": dimension, "additional_insert_sec": round(insertion, 3)}
    for label, call in [("browse", lambda: cat.browse(120)),
                        ("filename_search", lambda: cat.search("photo-000", 120)),
                        ("semantic_search", lambda: cat.search("synthetic", 120, Encoder()))]:
        started = time.monotonic()
        value = call()
        item[label + "_sec"] = round(time.monotonic() - started, 3)
        if label == "semantic_search":
            assert value["mode"] == "semantic" and len(value["photos"]) == 120
    item.update(memory())
    item["db_mib"] = round(cat.db_path.stat().st_size / 1024**2, 2)
    measurements.append(item)
    print(json.dumps(item), flush=True)
    previous = count

result = {"limits": "Generated SQLite rows and identical normalized 768-dimensional vectors. No real image files, model inference, disk scan, GPU, retrieval quality or WinUI measurement.", "measurements": measurements}
(Path(__file__).parent / "scalability-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
