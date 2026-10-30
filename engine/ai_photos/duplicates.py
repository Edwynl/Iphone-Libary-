"""Non-destructive SHA-256 and conservative perceptual duplicate grouping.

Nothing in this module changes or deletes the original photos.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .catalog import PhotoCatalog
from .file_safety import open_source


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open_source(path) as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dhash_file(path: Path) -> int | None:
    try:
        with Image.open(path) as src:
            image = ImageOps.exif_transpose(src).convert("L").resize(
                (9, 8), Image.Resampling.LANCZOS)
            pixels = list(image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata())
            bits = 0
            for y in range(8):
                for x in range(8):
                    bits = (bits << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
            return bits
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None


def _components(edges: set[tuple[int, int]], keys: set[int]) -> list[list[int]]:
    parent = {k: k for k in keys}

    def root(v):
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    for a, b in edges:
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[rb] = ra
    groups = defaultdict(list)
    for key in keys:
        groups[root(key)].append(key)
    return sorted((sorted(v) for v in groups.values() if len(v) > 1),
                  key=lambda x: (-len(x), x[0]))


def find_duplicates(catalog: PhotoCatalog, similar_distance: int = 4) -> dict:
    """Cache fingerprints keyed by size+mtime; return groups for human review only.

    Similarity is a heuristic, NOT permission to delete files. Only SHA matches
    establish byte-for-byte identity. Scan can be rerun to refresh the index.
    """
    if not 0 <= similar_distance <= 5:
        raise ValueError("similar_distance must be between 0 and 5")
    with catalog.connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS fingerprints (
            photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            dhash TEXT
        )""")
        rows = db.execute("SELECT id,path,name,size,mtime_ns FROM photos ORDER BY id").fetchall()
        photos = {}
        fingerprints = []
        calculated = 0
        for row in rows:
            photo_id = row["id"]
            path = Path(row["path"])
            if not path.is_file() or path.is_symlink():
                continue
            try:
                stat = path.stat()
                # Ignore stale catalog entries, so an unscanned file change is safe.
                if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
                    continue
                cached = db.execute("SELECT * FROM fingerprints WHERE photo_id=?", (photo_id,)).fetchone()
                if cached and cached["size"] == stat.st_size and cached["mtime_ns"] == stat.st_mtime_ns:
                    sha, dhash = cached["sha256"], cached["dhash"]
                else:
                    sha = sha256_file(path)
                    perceptual = dhash_file(path)
                    dhash = f"{perceptual:016x}" if perceptual is not None else None
                    db.execute("""INSERT INTO fingerprints(photo_id,size,mtime_ns,sha256,dhash)
                      VALUES(?,?,?,?,?) ON CONFLICT(photo_id) DO UPDATE SET
                      size=excluded.size,mtime_ns=excluded.mtime_ns,
                      sha256=excluded.sha256,dhash=excluded.dhash""",
                               (photo_id, stat.st_size, stat.st_mtime_ns, sha, dhash))
                    calculated += 1
                photos[photo_id] = {"id": photo_id, "name": row["name"], "path": str(path)}
                fingerprints.append((photo_id, sha, int(dhash, 16) if dhash else None))
            except (OSError, PermissionError):
                continue
        db.commit()

    by_sha = defaultdict(list)
    unique_content = []
    for photo_id, sha, phash in fingerprints:
        if not by_sha[sha]:
            unique_content.append((photo_id, phash))
        by_sha[sha].append(photo_id)
    exact = [sorted(v) for v in by_sha.values() if len(v) > 1]
    exact.sort(key=lambda x: (-len(x), x[0]))

    # At most d differing bits leave one of d+1 disjoint bands unchanged.
    # Four bands cannot guarantee recall for the default distance of four.
    bands = similar_distance + 1
    boundaries = [(64 * i // bands, 64 * (i + 1) // bands) for i in range(bands)]
    buckets = defaultdict(set)
    by_hash = defaultdict(list)
    for photo_id, phash in unique_content:
        if phash is not None:
            by_hash[phash].append(photo_id)
    hashes = sorted(by_hash)
    edges = set()
    for phash in hashes:
        candidates = set()
        for band, (start, end) in enumerate(boundaries):
            key = (band, (phash >> start) & ((1 << (end - start)) - 1))
            candidates.update(buckets[key])
        for other in candidates:
            if (phash ^ other).bit_count() <= similar_distance:
                a, b = by_hash[phash][0], by_hash[other][0]
                edges.add((min(a, b), max(a, b)))
        for band, (start, end) in enumerate(boundaries):
            buckets[(band, (phash >> start) & ((1 << (end - start)) - 1))].add(phash)
    # Different byte content can produce the same perceptual hash.
    for ids in by_hash.values():
        if len(ids) > 1:
            first = ids[0]
            edges.update((min(first, x), max(first, x)) for x in ids[1:])
    groups = _components(edges, {photo_id for photo_id, phash in unique_content if phash is not None})
    return {
        "scanned": len(fingerprints),
        "fingerprints_calculated": calculated,
        "exact_groups": [[photos[x] for x in group] for group in exact],
        "similar_groups": [[photos[x] for x in group] for group in groups],
        "note": "Review groups manually. Neither originals nor indexed files were deleted.",
    }
