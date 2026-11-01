"""Import new photos and videos from a USB-connected iPhone without duplicates.

The app copies camera-roll files from the iPhone (through the Windows Shell)
into a staging folder inside the chosen destination. This module decides which
files to copy and files what arrived. The iPhone is only read; nothing on it
is changed.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .catalog import LIVE_STILL_EXTENSIONS, PhotoCatalog
from .duplicates import sha256_file
from .media import is_video, probe_video

STAGING_DIR = ".wap-importing"


def _item_id(item: dict) -> str:
    # The DCIM folder keeps IMG_0001 in 100APPLE and in 101APPLE apart.
    folder = str(item.get("folder") or "").replace("\\", "/").casefold()
    return f"{folder}|{str(item['name']).casefold()}|{int(item['size'])}|{item.get('date') or ''}"


def _legacy_item_id(item: dict) -> str:
    # The first version's journal key, without the folder. Its entries carry no hash.
    return f"{str(item['name']).casefold()}|{int(item['size'])}|{item.get('date') or ''}"


def _month(date: str | None) -> str:
    try:
        return datetime.fromisoformat(str(date)).strftime("%Y-%m")
    except ValueError:
        return datetime.now().strftime("%Y-%m")


def _capture_month(path: Path) -> str | None:
    """YYYY-MM the file was taken in, from its own metadata: EXIF for photos, the QuickTime
    header (UTC, shown in local time) for videos. None when it has no capture time."""
    try:
        if is_video(path):
            taken = probe_video(path)[1]
            return datetime.fromisoformat(taken).astimezone().strftime("%Y-%m") if taken else None
        with Image.open(path) as image:
            taken = image.getexif().get_ifd(0x8769).get(0x9003)  # DateTimeOriginal, local time
        return datetime.strptime(str(taken).strip(), "%Y:%m:%d %H:%M:%S").strftime("%Y-%m") if taken else None
    except (OSError, ValueError, SyntaxError):
        return None


class DeviceImporter:
    def __init__(self, catalog: PhotoCatalog):
        self.catalog = catalog
        self.db_path = catalog.home / "device_imports.db"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imported (
                    device_id TEXT NOT NULL, item TEXT NOT NULL, path TEXT,
                    outcome TEXT NOT NULL, imported_at TEXT NOT NULL,
                    PRIMARY KEY (device_id, item));
            """)
            # The SHA-256 of the verified copy, and the modification time it was last hashed at.
            columns = {row[1] for row in db.execute("PRAGMA table_info(imported)")}
            for name, declaration in (("sha256", "TEXT"), ("mtime_ns", "INTEGER")):
                if name not in columns:
                    db.execute(f"ALTER TABLE imported ADD COLUMN {name} {declaration}")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def settings(self, update: dict | None = None) -> dict:
        if update and update.get("destination"):
            # Refuse now rather than after copying: commit adds it as a library root.
            dest = Path(update["destination"]).resolve(strict=True)
            for root in map(Path, self.catalog.folders()):
                if dest in root.parents:
                    raise ValueError(f"The import folder contains a library folder already added: {root}. "
                                     "Choose that folder, a folder inside it, or a separate folder.")
        with self._db() as db:
            for key in ("destination", "auto"):
                if update and key in update:
                    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(update[key])))
            values = dict(db.execute("SELECT key,value FROM settings").fetchall())
        return {"destination": values.get("destination", ""), "auto": values.get("auto") == "True"}

    def plan(self, device_id: str, items: list[dict]) -> dict:
        """Pick every item without a verified copy on this computer.

        An item is skipped only while the file its journal entry points to (its import, or
        the library file it was found byte-identical to) still has the SHA-256 recorded when
        it was filed; the file is hashed again whenever its size or modification time changed.
        Everything else is copied and compared by SHA-256 in commit: a file is never taken
        as present from its name and size alone. Entries from versions that stored no hash
        are counted in `reverify`: those items are copied again once and checked.
        """
        selected, skipped_known, reverify = [], 0, 0
        with self._db() as db:
            known = {row[0]: row[1:] for row in db.execute(
                "SELECT item,path,sha256,mtime_ns FROM imported WHERE device_id=?", (device_id,))}
            for item in items:
                entry = known.get(_item_id(item)) or known.get(_legacy_item_id(item))
                if entry and entry[1] and self._unchanged(db, *entry, int(item["size"])):
                    skipped_known += 1
                    continue
                if entry and not entry[1]:
                    reverify += 1
                selected.append(item["key"])
        return {"selected": selected, "skipped_known": skipped_known, "reverify": reverify}

    @staticmethod
    def _unchanged(db: sqlite3.Connection, path: str, sha256: str, mtime_ns: int | None, size: int) -> bool:
        """Whether path still holds the bytes recorded for it."""
        try:
            stat = os.stat(path)
            if stat.st_size != size:
                return False
            if stat.st_mtime_ns == mtime_ns:
                return True
            if sha256_file(Path(path)) != sha256:
                return False
        except OSError:
            return False
        db.execute("UPDATE imported SET mtime_ns=? WHERE path=? AND sha256=?", (stat.st_mtime_ns, path, sha256))
        return True

    def commit(self, device_id: str, destination: str, staging: str, items: list[dict], progress=None) -> dict:
        """File copied items into destination/YYYY-MM, dropping exact (SHA-256) duplicates.

        `staging` is destination/.wap-importing/<session>; the app copies each
        DCIM folder (100APPLE, ...) into its own subfolder there. Item `files`
        are paths relative to `staging`; the first is the item itself and must have
        the item's `size`, or the copy is incomplete: it is discarded and not journaled,
        so the next import copies it again. Files go under the month they were taken.
        """
        dest = Path(destination).resolve(strict=True)
        stage = Path(staging).resolve(strict=True)
        if stage.parent != dest / STAGING_DIR:
            raise ValueError("Staging folder is not inside the import destination")
        by_size: dict[int, list[str]] = {}
        with self.catalog.connection() as db:
            for row in db.execute("SELECT path,size FROM photos"):
                by_size.setdefault(row["size"], []).append(row["path"])
        digests: dict[str, str] = {}
        names = ({}, set())  # Name suffix per destination folder and stem group; every name reserved.
        records, imported, duplicates, incomplete = [], 0, 0, 0
        now = datetime.now(timezone.utc).isoformat()
        groups = [(item, [self._staged(stage, name) for name in item.get("files", [])]) for item in items]
        # Safety net: a copied file nobody listed (e.g. from an interrupted run)
        # is still filed, dated by its timestamp, rather than left in staging.
        listed = {path for _, files in groups for path in files}
        for path in sorted(stage.rglob("*")):
            if path.is_file() and path.suffix != ".part" and path not in listed:
                groups.append(({"date": datetime.fromtimestamp(path.stat().st_mtime).isoformat()}, [path]))
        # The extensions of each stem in each copied folder: a Live Photo's still and motion
        # arrive as separate items and must share one name.
        companions: dict[tuple[Path, str], set[str]] = {}
        for _, files in groups:
            for path in files:
                companions.setdefault((path.parent, path.stem.casefold()), set()).add(path.suffix)
        # A Live Photo's motion is filed under its still's month, so the pair shares a folder.
        still_months = {}
        for _, files in groups:
            for path in files:
                if path.suffix.lower() in LIVE_STILL_EXTENSIONS and path.is_file():
                    if month := _capture_month(path):
                        still_months[(path.parent, path.stem.casefold())] = month
        for done, (item, files) in enumerate(groups, 1):
            files = [p for p in files if p.is_file()]
            if files and "name" in item and files[0].stat().st_size != int(item["size"]):
                for path in files:
                    path.unlink()  # Our own short copy; the phone's original is untouched.
                incomplete += 1
                continue
            keep, matches = [], []
            for path in files:
                digest = sha256_file(path)
                if match := self._library_copy(path, digest, by_size, digests):
                    path.unlink()  # Our own fresh copy; the library already has this file.
                    matches.append((match, digest))
                    duplicates += 1
                else:
                    keep.append((path, digest))
            month = None
            if files:
                month = still_months.get((files[0].parent, files[0].stem.casefold())) or _capture_month(files[0])
            placed = self._place(dest / (month or _month(item.get("date"))), keep, companions, names)
            for path, digest in placed:
                by_size.setdefault(path.stat().st_size, []).append(str(path))
                digests[str(path)] = digest
            imported += len(placed)
            if files and "name" in item:
                # The verified copy the next plan checks for: this import, or the identical library file.
                path, digest = (str(placed[0][0]), placed[0][1]) if placed else matches[0]
                records.append((device_id, _item_id(item), path, digest, os.stat(path).st_mtime_ns,
                                "imported" if placed else "duplicate", now))
            if progress and done % 10 == 0:
                progress({"stage": "importing", "done": done, "total": len(groups)})
        with self._db() as db:
            db.executemany("""INSERT OR REPLACE INTO imported (device_id,item,path,sha256,mtime_ns,outcome,imported_at)
                              VALUES (?,?,?,?,?,?,?)""", records)
        self._remove_empty_folders(stage)
        return {"imported": imported, "duplicates": duplicates, "incomplete": incomplete, "items": len(records),
                "library_folder": self._ensure_library_folder(dest)}

    @staticmethod
    def _remove_empty_folders(stage: Path) -> None:
        subfolders = sorted((d for d in stage.rglob("*") if d.is_dir()), reverse=True)  # Deepest first.
        for folder in [*subfolders, stage, stage.parent]:
            try:
                folder.rmdir()
            except OSError:
                pass  # Not empty: files that could not be filed stay for inspection.

    @staticmethod
    def _staged(stage: Path, name: str) -> Path:
        path = (stage / name).resolve()
        if stage not in path.parents:
            raise ValueError(f"Imported file is outside the staging folder: {name}")
        return path

    @staticmethod
    def _library_copy(path: Path, digest: str, by_size: dict[int, list[str]], digests: dict[str, str]) -> str | None:
        """A library file byte-identical to path, if any."""
        for candidate in by_size.get(path.stat().st_size, []):
            if candidate not in digests:
                try:
                    digests[candidate] = sha256_file(Path(candidate))
                except OSError:
                    continue  # Offline or moved library file: cannot prove a duplicate.
            if digests[candidate] == digest:
                return candidate
        return None

    @staticmethod
    def _place(folder: Path, files: list[tuple[Path, str]], companions: dict[tuple[Path, str], set[str]],
               names: tuple[dict, set]):
        """Move one item's files (still, Live motion, sidecars) without overwriting.

        Files sharing a stem in one copied folder get one suffix, chosen free for all of
        their extensions before the first moves, so a Live Photo pair stays paired even
        when its name is taken by an older photo (iPhone numbers wrap at 9999).
        """
        if not files:
            return []
        folder.mkdir(parents=True, exist_ok=True)
        suffixes, reserved = names
        placed = []
        for path, digest in files:
            stem, group = path.stem, (path.parent, path.stem.casefold())
            if (folder, group) not in suffixes:
                extensions = companions.get(group, {path.suffix})
                suffix, number = "", 1
                while any((folder / f"{stem}{suffix}{ext}").exists() or
                          (folder, f"{stem}{suffix}{ext}".casefold()) in reserved for ext in extensions):
                    number += 1
                    suffix = f" ({number})"
                suffixes[(folder, group)] = suffix
                reserved.update((folder, f"{stem}{suffix}{ext}".casefold()) for ext in extensions)
            suffix, number = suffixes[(folder, group)], 1
            while (target := folder / f"{stem}{suffix}{path.suffix}").exists():
                number += 1  # Only a file that appeared after the name was chosen.
                suffix = f" ({number})"
            os.rename(path, target)  # Same volume; fails instead of replacing.
            placed.append((target, digest))
        return placed

    def _ensure_library_folder(self, dest: Path) -> str:
        roots = [Path(p) for p in self.catalog.folders()]
        covering = next((r for r in roots if r == dest or r in dest.parents), None)
        if covering is None:
            self.catalog.add_folder(str(dest))  # Raises if dest would swallow an existing root.
            covering = dest
        return str(covering)
