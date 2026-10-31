"""SQLite catalog, thumbnail cache and incremental photo scan."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import SKIP_DIRS, SUPPORTED_EXTENSIONS, app_home
from .file_safety import is_reparse, check_path
from .media import VIDEO_EXTENSIONS, probe_video, video_thumbnail

LIVE_STILL_EXTENSIONS = frozenset({".heic", ".heif", ".jpg", ".jpeg"})
# Live Photo motion files are kept (backups, recycle pairing) but shown through their still.
_VISIBLE = "media_kind!='live_motion'"
_MEDIA_FILTERS = {"all": _VISIBLE, "photo": "media_kind='photo'", "video": "media_kind='video'",
                  "screenshot": "media_kind='photo' AND screenshot=1"}
_COLUMNS = "id,path,name,thumb_path,mtime_ns,taken_at,width,height,error,media_kind,companion"
# Local "YYYY-MM-DD HH:MM:SS" to sort by. taken_at is EXIF local time ("2026:09:19 16:47:41") for
# photos and ISO UTC for videos; without it (saved images, screenshots) the file time stands in.
_SORT_TIME = """COALESCE(CASE
    WHEN taken_at GLOB '[0-9][0-9][0-9][0-9]:[0-9][0-9]:[0-9][0-9] *'
        THEN replace(substr(taken_at,1,10),':','-') || substr(taken_at,11,9)
    WHEN taken_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN datetime(taken_at,'localtime') END,
    datetime(mtime_ns/1000000000,'unixepoch','localtime'))"""
_ORDERS = {"newest": "DESC", "oldest": "ASC"}
_FILENAME_QUERY = re.compile(r"[A-Za-z0-9_.\-]*\d[A-Za-z0-9_.\-]*")
_SCREENSHOT_NAME = re.compile(r"screen[ _-]?shot|截图|截屏", re.IGNORECASE)

LOG = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = 150_000_000
try:
    import pillow_heif  # type: ignore[import-not-found]
    pillow_heif.register_heif_opener()
except ImportError:
    # HEIC originals remain cataloged with an explicit decoder error.
    pass


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _is_screenshot(path: Path) -> bool | None:
    """iPhone screenshots are PNGs whose EXIF UserComment says "Screenshot";
    Windows, macOS and Android name them so. None: unreadable now, retry later."""
    if _SCREENSHOT_NAME.search(path.name):
        return True
    if path.suffix.lower() != ".png":
        return False
    try:
        with Image.open(path) as image:
            comment = image.getexif().get_ifd(0x8769).get(0x9286)
    except (OSError, ValueError):
        return None
    if isinstance(comment, bytes):
        comment = comment.decode("utf-8", "ignore")
    return "screenshot" in str(comment or "").casefold()


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
            columns = {r["name"] for r in db.execute("PRAGMA table_info(photos)")}
            for name, declaration in (("media_kind", "TEXT NOT NULL DEFAULT 'photo'"),
                                      ("live_meta", "INTEGER"), ("companion", "TEXT"),
                                      ("screenshot", "INTEGER")):
                if name not in columns:
                    db.execute(f"ALTER TABLE photos ADD COLUMN {name} {declaration}")
            if "media_kind" not in columns:
                db.execute("UPDATE photos SET media_kind='video' WHERE " + " OR ".join(
                    f"lower(path) LIKE '%{suffix}'" for suffix in sorted(VIDEO_EXTENSIONS)))
            db.execute("CREATE INDEX IF NOT EXISTS idx_photos_kind ON photos(media_kind, mtime_ns DESC, id DESC)")

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
                             not is_reparse(Path(directory, d)) and
                             Path(root).resolve() in Path(directory, d).resolve(strict=False).parents and
                             Path(directory, d).resolve(strict=False) != cache_root)
            for name in sorted(files):
                if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS | VIDEO_EXTENSIONS:
                    path = Path(directory, name)
                    if not is_reparse(path):
                        yield path

    def _thumb_path(self, path: Path, mtime_ns: int) -> Path:
        digest = hashlib.sha256((str(path) + ":" + str(mtime_ns)).encode("utf-8")).hexdigest()
        return self.thumbs_dir / digest[:2] / (digest + ".jpg")

    def _thumb(self, path: Path, mtime_ns: int) -> tuple[str, int, int, str | None]:
        out = self._thumb_path(path, mtime_ns)
        out.parent.mkdir(exist_ok=True, parents=True)
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            # A poster frame when Windows has a codec; never an error for videos.
            if out.is_file():
                return str(out), 0, 0, None
            if probe_video(path)[0]:
                return "", 0, 0, None  # Live Photo motion: shown through its still.
            return (str(out) if video_thumbnail(path, out) else ""), 0, 0, None
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

    def prepare(self, path):
        stat = path.stat()
        return stat, self._thumb(path, stat.st_mtime_ns)

    def _upsert(self, db: sqlite3.Connection, root: str, path: Path, prepared=None) -> tuple[int, bool, bool]:
        try:
            stat = prepared[0] if prepared else path.stat()
        except (FileNotFoundError, PermissionError):
            return 0, False, False
        p = str(path)
        previous = db.execute("SELECT id,size,mtime_ns,thumb_path,error,name FROM photos WHERE path=?", (p,)).fetchone()
        changed = previous is None or previous["size"] != stat.st_size or previous["mtime_ns"] != stat.st_mtime_ns
        if changed or previous['error']:
            # An unchanged undecodable file is retried (a decoder may have been
            # installed) but only rewritten and reported as updated once it decodes.
            thumbnail, width, height, taken_or_error = prepared[1] if prepared else self._thumb(path, stat.st_mtime_ns)
            if not changed and not thumbnail:
                return previous["id"], False, False
            error = None if thumbnail else taken_or_error
            taken = taken_or_error if thumbnail else None
            # A changed file is probed again (Live Photo metadata, screenshot) after the walk.
            db.execute("""
                INSERT INTO photos(path,folder,name,size,mtime_ns,taken_at,width,height,thumb_path,error,indexed_at,media_kind,live_meta)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(path) DO UPDATE SET folder=excluded.folder,name=excluded.name,
                  size=excluded.size,mtime_ns=excluded.mtime_ns,taken_at=excluded.taken_at,
                  width=excluded.width,height=excluded.height,thumb_path=excluded.thumb_path,
                  error=excluded.error,indexed_at=excluded.indexed_at,
                  media_kind=excluded.media_kind,live_meta=NULL,screenshot=NULL
            """, (p, root, previous['name'] if previous else path.name, stat.st_size, stat.st_mtime_ns, taken, width, height,
                  thumbnail, error, datetime.now(timezone.utc).isoformat(),
                  'video' if path.suffix.lower() in VIDEO_EXTENSIONS else 'photo'))
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
                        db.commit()  # Never hold a SQLite writer while running model inference.
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
                                db.commit()
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
                    if db.in_transaction:
                        db.commit()  # Release preceding writes before decoding the next image.
                    photo_id, was_updated, decodable = self._upsert(db, root, path)
                    if was_updated:
                        db.commit()  # Unchanged files only read; they need no commit/fsync.
                    if photo_id == 0:
                        errors += 1
                        continue
                    if was_updated:
                        updated += 1
                    if not decodable and path.suffix.lower() not in VIDEO_EXTENSIONS:
                        errors += 1  # Every currently undecodable file, changed or not.
                    if encoder is not None and decodable:
                        has_embedding = db.execute(
                            "SELECT 1 FROM embeddings WHERE photo_id=? AND model_id=?",
                            (photo_id, model_id)).fetchone()
                        if not has_embedding:
                            source = p
                            if path.suffix.lower() in VIDEO_EXTENSIONS:
                                # A video is searchable through its poster frame.
                                source = db.execute("SELECT thumb_path FROM photos WHERE id=?",
                                                    (photo_id,)).fetchone()[0]
                            embed_queue.append((photo_id, source))
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
            self._link_live_photos(db)
            self._mark_screenshots(db)
        result = {"seen": seen, "updated": updated, "embedded": embedded,
                  "removed": removed, "errors": errors, "elapsed_sec": round(time.monotonic() - t0, 2)}
        if progress:
            progress({"stage": "complete", **result})
        return result

    def _link_live_photos(self, db: sqlite3.Connection) -> None:
        """Classify videos: a Live Photo motion file needs Apple's identifier and a same-stem still."""
        pending = db.execute("SELECT id,path FROM photos WHERE media_kind!='photo' AND live_meta IS NULL").fetchall()
        db.commit()  # Probing reads files; hold no writer meanwhile.
        probed = [(row["id"], *probe_video(Path(row["path"]))) for row in pending]
        for photo_id, live, taken in probed:
            if live is not None:  # Unreadable now (offline/denied): retry on a later scan.
                db.execute("UPDATE photos SET live_meta=?,taken_at=COALESCE(?,taken_at) WHERE id=?",
                           (int(live), taken, photo_id))
        stills = {}
        for row in db.execute("SELECT path FROM photos WHERE media_kind='photo'"):
            path = Path(row["path"])
            if path.suffix.lower() in LIVE_STILL_EXTENSIONS:
                stills.setdefault((os.path.normcase(str(path.parent)), path.stem.casefold()), row["path"])
        db.execute("UPDATE photos SET companion=NULL WHERE companion IS NOT NULL")
        for row in db.execute("SELECT id,path,live_meta FROM photos WHERE media_kind!='photo'").fetchall():
            path = Path(row["path"])
            still = stills.get((os.path.normcase(str(path.parent)), path.stem.casefold())) if row["live_meta"] else None
            db.execute("UPDATE photos SET media_kind=?,companion=? WHERE id=?",
                       ("live_motion" if still else "video", still, row["id"]))
            if still:
                db.execute("UPDATE photos SET companion=? WHERE path=?", (row["path"], still))
        db.commit()
        # Visible videos still without a poster: catalogs from older versions,
        # Live clips whose still is gone, or a codec installed since. Cheap to retry.
        missing = db.execute("""SELECT id,path,mtime_ns FROM photos
                                WHERE media_kind='video' AND COALESCE(thumb_path,'')=''""").fetchall()
        db.commit()
        posters = []
        for row in missing:
            out = self._thumb_path(Path(row["path"]), row["mtime_ns"])
            if out.is_file() or video_thumbnail(Path(row["path"]), out):
                posters.append((str(out), row["id"]))
        db.executemany("UPDATE photos SET thumb_path=? WHERE id=?", posters)
        db.commit()

    def _mark_screenshots(self, db: sqlite3.Connection) -> None:
        pending = db.execute("SELECT id,path FROM photos WHERE media_kind='photo' AND screenshot IS NULL").fetchall()
        db.commit()  # Reading files; hold no writer meanwhile.
        marks = [(int(found), row["id"]) for row in pending
                 if (found := _is_screenshot(Path(row["path"]))) is not None]
        db.executemany("UPDATE photos SET screenshot=? WHERE id=?", marks)
        db.commit()

    def browse(self, limit: int = 120, offset: int = 0, media_type: str = "all", order: str = "newest") -> list[dict]:
        if media_type not in _MEDIA_FILTERS:
            raise ValueError("Invalid media type")
        if order not in _ORDERS:
            raise ValueError("Invalid sort order")
        where, direction = _MEDIA_FILTERS[media_type], _ORDERS[order]
        with self.connection() as db:
            rows = db.execute(f"""
                SELECT {_COLUMNS} FROM photos
                WHERE {where} ORDER BY {_SORT_TIME} {direction},id {direction} LIMIT ? OFFSET ?
            """, (max(1, min(int(limit), 300)), max(0, int(offset)))).fetchall()
            return [dict(row) for row in rows]

    def recycle_plan(self, paths: list[str]) -> dict:
        """Resolve candidate Live Photo pairs; never move files here."""
        if not isinstance(paths, list) or not paths or len(paths) > 1000:
            raise ValueError("Select between 1 and 1000 files")
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT path, size, mtime_ns FROM photos")]
            # Pairs confirmed by the Live Photo identifier during scan.
            links = {os.path.normcase(r[0]): os.path.normcase(r[1]) for r in db.execute(
                "SELECT path, companion FROM photos WHERE companion IS NOT NULL")}
            has_sync = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_transfers'").fetchone()
            sync = {os.path.normcase(os.path.abspath(r[0])) for r in db.execute(
                "SELECT stored_path FROM sync_transfers WHERE stored_path IS NOT NULL")} if has_sync else set()
        by_path = {os.path.normcase(os.path.abspath(r['path'])): r for r in rows}
        groups = {}
        for row in rows:
            path = Path(row['path'])
            groups.setdefault((os.path.normcase(str(path.parent)), path.stem.casefold()), []).append(row)
        chosen = {}
        candidates = []
        confirmed = []
        for raw in paths:
            key = os.path.normcase(os.path.abspath(raw))
            if key not in by_path:
                raise ValueError("File is no longer in the catalog: " + raw)
            row = by_path[key]
            chosen[key] = row
            p = Path(row['path'])
            suffix = p.suffix.lower()
            if suffix not in {'.heic', '.heif', '.jpg', '.jpeg', '.mov'}:
                continue
            wanted = {'.mov'} if suffix != '.mov' else {'.heic', '.heif', '.jpg', '.jpeg'}
            matches = [r for r in groups.get((os.path.normcase(str(p.parent)), p.stem.casefold()), [])
                       if Path(r['path']).suffix.lower() in wanted]
            for companion in matches:
                other = os.path.normcase(os.path.abspath(companion['path']))
                if other not in chosen:
                    chosen[other] = companion
                    candidates.append(companion['path'])
                    if links.get(os.path.normcase(row['path'])) == os.path.normcase(companion['path']):
                        confirmed.append(companion['path'])
        for key, row in chosen.items():
            if key in sync:
                raise ValueError("Synced/shared resource protected; deletion unavailable: " + row['path'])
            path = check_path(Path(row['path']))
            info = path.stat()
            if not path.is_file() or info.st_size != row['size'] or info.st_mtime_ns != row['mtime_ns']:
                raise ValueError("File changed since scan; scan again: " + str(path))
        return {"files": list(chosen.values()), "candidate_companions": candidates,
                "confirmed_companions": confirmed}

    def stats(self, model_id: str | None = None) -> dict:
        with self.connection() as db:
            count, videos, live, screenshots = db.execute("""SELECT COALESCE(SUM(media_kind='photo'),0), COALESCE(SUM(media_kind='video'),0),
                COALESCE(SUM(media_kind='photo' AND companion IS NOT NULL),0),
                COALESCE(SUM(media_kind='photo' AND screenshot=1),0) FROM photos""").fetchone()
            failures = db.execute("SELECT COUNT(*) FROM photos WHERE error IS NOT NULL").fetchone()[0]
            vectors = db.execute("SELECT COUNT(*) FROM embeddings WHERE model_id=?", (model_id,)).fetchone()[0] if model_id else 0
            return {"photos": count, "videos": videos, "live_photos": live, "screenshots": screenshots, "folders": len(self.folders()), "errors": failures,
                    "embeddings": vectors, "db_bytes": self.db_path.stat().st_size}

    def search(self, query: str, limit: int = 120, encoder=None) -> dict:
        query = query.strip()
        limit = min(max(1, int(limit)), 200)
        if not query:
            return {"mode": "browse", "photos": self.browse(limit)}
        if encoder is not None:
            try:
                # Typed filenames (e.g. IMG_3164) come first; the model cannot match them.
                named = self._by_name(query, limit, names_only=True)
                if named and _FILENAME_QUERY.fullmatch(query):
                    # IMG_3164-style queries want those files, not look-alike pictures.
                    return {"mode": "semantic", "filename_matches": len(named), "photos": named}
                ids = {p["id"] for p in named}
                ranked = [p for p in self._semantic(query, limit, encoder) if p["id"] not in ids]
                return {"mode": "semantic", "filename_matches": len(named), "photos": (named + ranked)[:limit]}
            except Exception as exc:
                LOG.exception("Semantic search failed; falling back to filename matching")
                # Report fallback transparently; not silently claim AI results.
                return {"mode": "filename", "warning": str(exc), "photos": self._by_name(query, limit)}
        return {"mode": "filename", "warning": "AI not installed; only filenames/paths searched",
                "photos": self._by_name(query, limit)}

    def _by_name(self, query: str, limit: int, names_only: bool = False) -> list[dict]:
        # ESCAPE prevents '%'/'_' from acting as wildcards in user input.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = "%" + escaped + "%"
        # Beside AI results, a folder name such as 2026.09.19 must not match every file.
        where = "name LIKE ? ESCAPE '\\'" if names_only else "(name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')"
        with self.connection() as db:
            return [dict(row) for row in db.execute(f"""
                SELECT {_COLUMNS} FROM photos
                WHERE {where} AND {_VISIBLE}
                ORDER BY mtime_ns DESC,id DESC LIMIT ?
            """, (pattern, limit) if names_only else (pattern, pattern, limit))]

    def _semantic(self, query: str, limit: int, encoder) -> list[dict]:
        probe = np.asarray(encoder.embed_text(query), dtype=np.float32).reshape(-1)
        probe /= max(float(np.linalg.norm(probe)), 1e-12)
        metadata: list[dict] = []
        vectors = []
        with self.connection() as db:
            for r in db.execute("""
                SELECT p.id,p.path,p.name,p.thumb_path,p.mtime_ns,p.taken_at,p.width,p.height,p.error,
                       p.media_kind,p.companion,e.dimensions,e.vector FROM embeddings e
                JOIN photos p ON p.id=e.photo_id WHERE e.model_id=?
            """, (encoder.model_id,)):
                if r["dimensions"] != probe.size:
                    continue
                v = np.frombuffer(r["vector"], dtype="<f4")
                if v.size != probe.size:
                    continue
                vectors.append(v)
                metadata.append({k: r[k] for k in _COLUMNS.split(",")})
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
