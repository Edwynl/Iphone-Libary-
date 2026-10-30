"""SQLite catalog, thumbnail cache and incremental photo scan."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import SKIP_DIRS, SUPPORTED_EXTENSIONS, app_home

LOG = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = 150_000_000


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


class PhotoCatalog:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else app_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / "catalog.db"
        self.thumbs_dir = self.home / "thumbs"
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self):
        db = _connect(self.db_path)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS folders (
                  path TEXT PRIMARY KEY COLLATE NOCASE,
                  added_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS photos (
                  id INTEGER PRIMARY KEY,
                  path TEXT NOT NULL UNIQUE COLLATE NOCASE,
                  folder TEXT NOT NULL,
                  name TEXT NOT NULL,
                  size INTEGER NOT NULL,
                  mtime_ns INTEGER NOT NULL,
                  taken_at TEXT,
                  width INTEGER,
                  height INTEGER,
                  thumb_path TEXT,
                  error TEXT,
                  indexed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(mtime_ns DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder);
                CREATE TABLE IF NOT EXISTS embeddings (
                  photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                  model_id TEXT NOT NULL,
                  dimensions INTEGER NOT NULL,
                  vector BLOB NOT NULL,
                  PRIMARY KEY(photo_id, model_id)
                );
                CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model_id);
            """)

    def add_folder(self, folder: str) -> dict:
        path = Path(folder).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError(f"Not a directory: {path}")
        # The cache contains JPEG previews. Never allow it to become a source.
        cache_root = self.home.resolve()
        if path == cache_root or cache_root in path.parents:
            raise ValueError("The app's internal cache cannot be a photo source")
        # Avoid duplicate indexing when roots overlap. Existing root wins.
        with self.connection() as db:
            for row in db.execute("SELECT path FROM folders"):
                old = Path(row["path"])
                if path == old:
                    return {"path": str(path), "added": False}
                if path in old.parents or old in path.parents:
                    raise ValueError(f"Overlapping photo folder already added: {old}")
            db.execute("INSERT INTO folders (path, added_at) VALUES (?, ?)",
                       (str(path), datetime.now(timezone.utc).isoformat()))
        return {"path": str(path), "added": True}

    def remove_folder(self, folder: str) -> dict:
        """Unregister a source and forget its index, without touching originals.

        Works even if an external drive is disconnected. The thumbnail cache is
        eligible for later maintenance; no source path is ever unlinked here.
        """
        path = str(Path(folder).expanduser().resolve(strict=False))
        with self.connection() as db:
            exists = db.execute("SELECT 1 FROM folders WHERE path=?", (path,)).fetchone()
            if not exists:
                return {"path": path, "removed": False, "photos_forgotten": 0}
            count = db.execute("SELECT COUNT(*) FROM photos WHERE folder=?", (path,)).fetchone()[0]
            db.execute("DELETE FROM photos WHERE folder=?", (path,))
            db.execute("DELETE FROM folders WHERE path=?", (path,))
        return {"path": path, "removed": True, "photos_forgotten": count}

    def folders(self) -> list[str]:
        with self.connection() as db:
            return [r["path"] for r in db.execute("SELECT path FROM folders ORDER BY path")]

    def _walk(self, root: str, on_error=None) -> Iterator[Path]:
        # A user's source root may contain %LOCALAPPDATA%/WindowsAIPhotos.
        # Exclude our own preview cache to prevent recursively indexing output.
        cache_root = self.home.resolve()
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
            dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and
                             not Path(directory, d).is_symlink() and
                             Path(directory, d).resolve(strict=False) != cache_root)
            for name in sorted(files):
                if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS:
                    path = Path(directory, name)
                    if not path.is_symlink():
                        yield path

    def _thumb(self, path: Path, mtime_ns: int) -> tuple[str, int, int, str | None]:
        digest = hashlib.sha256((str(path) + ":" + str(mtime_ns)).encode("utf-8")).hexdigest()
        out = self.thumbs_dir / digest[:2] / (digest + ".jpg")
        out.parent.mkdir(exist_ok=True, parents=True)
        try:
            with Image.open(path) as src:
                image = ImageOps.exif_transpose(src)
                width, height = image.size
                exif = src.getexif()
                dt = str(exif.get(36867) or exif.get(306) or "") or None
                if not out.is_file():
                    image.thumbnail((384, 384), Image.Resampling.LANCZOS)
                    if image.mode not in {"RGB", "L"}:
                        image = image.convert("RGB")
                    temporary = out.with_suffix(".tmp")
                    try:
                        image.save(temporary, "JPEG", quality=78, optimize=True)
                        os.replace(temporary, out)
                    finally:
                        temporary.unlink(missing_ok=True)
                return str(out), width, height, dt
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            # Keep catalog entry so the user sees the filename and can open in Photos.
            return "", 0, 0, str(exc)[:300]

    def _upsert(self, db: sqlite3.Connection, root: str, path: Path) -> tuple[int, bool, bool]:
        try:
            stat = path.stat()
        except (FileNotFoundError, PermissionError):
            return 0, False, False
        p = str(path)
        previous = db.execute("SELECT id,size,mtime_ns,thumb_path,error FROM photos WHERE path=?", (p,)).fetchone()
        changed = previous is None or previous["size"] != stat.st_size or previous["mtime_ns"] != stat.st_mtime_ns
        if changed:
            thumbnail, width, height, taken_or_error = self._thumb(path, stat.st_mtime_ns)
            error = None if thumbnail else taken_or_error
            taken = taken_or_error if thumbnail else None
            db.execute("""
                INSERT INTO photos(path,folder,name,size,mtime_ns,taken_at,width,height,thumb_path,error,indexed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET folder=excluded.folder,name=excluded.name,
                  size=excluded.size,mtime_ns=excluded.mtime_ns,taken_at=excluded.taken_at,
                  width=excluded.width,height=excluded.height,thumb_path=excluded.thumb_path,
                  error=excluded.error,indexed_at=excluded.indexed_at
            """, (p, root, path.name, stat.st_size, stat.st_mtime_ns, taken, width, height,
                  thumbnail, error, datetime.now(timezone.utc).isoformat()))
            photo_id = db.execute("SELECT id FROM photos WHERE path=?", (p,)).fetchone()["id"]
            if previous:
                db.execute("DELETE FROM embeddings WHERE photo_id=?", (photo_id,))
            return photo_id, True, bool(thumbnail)
        return previous["id"], False, bool(previous["thumb_path"])

    def scan(self, encoder=None, progress: Callable[[dict], None] | None = None) -> dict:
        """Incrementally scan registered folders; embed missing vectors when available.

        A scan prunes missing files only after a root's successful traversal.
        Interrupted or inaccessible roots retain their prior library records.
        """
        added = updated = errors = embedded = removed = seen = 0
        t0 = time.monotonic()
        model_id = getattr(encoder, "model_id", None) if encoder is not None else None
        if encoder is not None and hasattr(encoder, "_ensure_loaded"):
            try:
                encoder._ensure_loaded()
            except Exception as exc:
                LOG.warning("AI unavailable; indexing metadata only: %s", exc)
                if progress:
                    progress({"stage": "warning", "message":
                              f"AI unavailable ({exc}); scanning thumbnails and metadata only"})
                encoder = None
        with self.connection() as db:
            for root in self.folders():
                if not Path(root).is_dir():
                    if progress:
                        progress({"stage": "warning", "folder": root, "message": "Folder offline; retaining existing records"})
                    continue
                seen_paths: set[str] = set()
                traversal_errors: list[str] = []
                embed_queue: list[tuple[int, str]] = []

                def flush_embed_queue():
                    nonlocal embedded, errors
                    if not embed_queue or encoder is None:
                        return
                    batch = embed_queue[:]
                    embed_queue.clear()
                    try:
                        vectors = encoder.embed_images([p for _, p in batch])
                        if len(vectors) != len(batch):
                            raise ValueError("AI model returned wrong batch size")
                        for (photo_id, _), vector in zip(batch, vectors):
                            blob = np.asarray(vector, dtype=np.float32).reshape(-1)
                            if not np.isfinite(blob).all() or blob.size == 0:
                                raise ValueError("AI model returned invalid vector")
                            db.execute("""INSERT OR REPLACE INTO embeddings(photo_id,model_id,dimensions,vector)
                                          VALUES(?,?,?,?)""", (photo_id, model_id, len(blob), blob.tobytes()))
                            embedded += 1
                    except Exception as exc:
                        LOG.warning("AI batch failed (%s); retrying images individually", exc)
                        for photo_id, p in batch:
                            try:
                                vec = np.asarray(encoder.embed_images([p])[0], dtype=np.float32).reshape(-1)
                                if not np.isfinite(vec).all() or vec.size == 0:
                                    raise ValueError("invalid embedding")
                                db.execute("""INSERT OR REPLACE INTO embeddings(photo_id,model_id,dimensions,vector)
                                              VALUES(?,?,?,?)""", (photo_id, model_id, vec.size, vec.tobytes()))
                                embedded += 1
                            except Exception as inner:
                                errors += 1
                                LOG.warning("Cannot embed %s: %s", p, inner)
                    db.commit()

                for path in self._walk(root, on_error=lambda e: traversal_errors.append(str(e))):
                    p = str(path)
                    seen_paths.add(p)
                    seen += 1
                    photo_id, was_updated, decodable = self._upsert(db, root, path)
                    if photo_id == 0:
                        errors += 1
                        continue
                    if was_updated:
                        updated += 1
                        if not decodable:
                            errors += 1
                    if encoder is not None and decodable:
                        has_embedding = db.execute(
                            "SELECT 1 FROM embeddings WHERE photo_id=? AND model_id=?",
                            (photo_id, model_id)).fetchone()
                        if not has_embedding:
                            embed_queue.append((photo_id, p))
                            if len(embed_queue) >= 8:
                                flush_embed_queue()
                    if seen % 25 == 0:
                        db.commit()
                        if progress:
                            progress({"stage": "indexing", "seen": seen, "updated": updated,
                                      "embedded": embedded, "errors": errors, "folder": root})
                flush_embed_queue()
                # Never prune when traversal had errors or the root went offline.
                # Only confirmed FileNotFoundError causes catalog-only deletion.
                if not traversal_errors and Path(root).is_dir():
                    for row in db.execute("SELECT id,path FROM photos WHERE folder=?", (root,)).fetchall():
                        if row["path"] not in seen_paths:
                            try:
                                os.stat(row["path"])
                            except FileNotFoundError:
                                db.execute("DELETE FROM photos WHERE id=?", (row["id"],))
                                removed += 1
                            except OSError:
                                # Never delete records on denied or transient I/O access.
                                pass
                elif progress:
                    progress({"stage": "warning", "folder": root,
                              "message": "Incomplete scan; existing photo records retained"})
                db.commit()
        result = {"seen": seen, "updated": updated, "embedded": embedded,
                  "removed": removed, "errors": errors, "elapsed_sec": round(time.monotonic() - t0, 2)}
        if progress:
            progress({"stage": "complete", **result})
        return result

    def browse(self, limit: int = 120, offset: int = 0) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("""
                SELECT id,path,name,thumb_path,mtime_ns,width,height,error FROM photos
                ORDER BY mtime_ns DESC,id DESC LIMIT ? OFFSET ?
            """, (max(1, min(int(limit), 300)), max(0, int(offset)))).fetchall()
            return [dict(row) for row in rows]

    def stats(self, model_id: str | None = None) -> dict:
        with self.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            failures = db.execute("SELECT COUNT(*) FROM photos WHERE error IS NOT NULL").fetchone()[0]
            vectors = db.execute("SELECT COUNT(*) FROM embeddings WHERE model_id=?", (model_id,)).fetchone()[0] if model_id else 0
            return {"photos": count, "folders": len(self.folders()), "errors": failures,
                    "embeddings": vectors, "db_bytes": self.db_path.stat().st_size}

    def search(self, query: str, limit: int = 120, encoder=None) -> dict:
        query = query.strip()
        limit = min(max(1, int(limit)), 200)
        if not query:
            return {"mode": "browse", "photos": self.browse(limit)}
        if encoder is not None:
            try:
                return {"mode": "semantic", "photos": self._semantic(query, limit, encoder)}
            except Exception as exc:
                LOG.exception("Semantic search failed; falling back to filename matching")
                # Report fallback transparently; not silently claim AI results.
                return {"mode": "filename", "warning": str(exc), "photos": self._by_name(query, limit)}
        return {"mode": "filename", "warning": "AI not installed; only filenames/paths searched",
                "photos": self._by_name(query, limit)}

    def _by_name(self, query: str, limit: int) -> list[dict]:
        # ESCAPE prevents '%'/'_' from acting as wildcards in user input.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = "%" + escaped + "%"
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT id,path,name,thumb_path,mtime_ns,width,height,error FROM photos
                WHERE name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'
                ORDER BY mtime_ns DESC,id DESC LIMIT ?
            """, (pattern, pattern, limit))]

    def _semantic(self, query: str, limit: int, encoder) -> list[dict]:
        probe = np.asarray(encoder.embed_text(query), dtype=np.float32).reshape(-1)
        probe /= max(float(np.linalg.norm(probe)), 1e-12)
        metadata: list[dict] = []
        vectors = []
        with self.connection() as db:
            for r in db.execute("""
                SELECT p.id,p.path,p.name,p.thumb_path,p.mtime_ns,p.width,p.height,p.error,
                       e.dimensions,e.vector FROM embeddings e
                JOIN photos p ON p.id=e.photo_id WHERE e.model_id=?
            """, (encoder.model_id,)):
                if r["dimensions"] != probe.size:
                    continue
                v = np.frombuffer(r["vector"], dtype="<f4")
                if v.size != probe.size:
                    continue
                vectors.append(v)
                metadata.append({k: r[k] for k in ("id", "path", "name", "thumb_path", "mtime_ns", "width", "height", "error")})
        if not vectors:
            return []
        matrix = np.stack(vectors).astype(np.float32, copy=False)
        scores = matrix @ probe
        order = np.argsort(-scores, kind="stable")[:limit]
        result = []
        for i in order:
            photo = dict(metadata[int(i)])
            photo["similarity"] = round(float(scores[i]), 4)
            result.append(photo)
        return result
