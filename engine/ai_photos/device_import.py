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

from .catalog import PhotoCatalog
from .duplicates import sha256_file

STAGING_DIR = ".wap-importing"


def _item_id(item: dict) -> str:
    return f"{str(item['name']).casefold()}|{int(item['size'])}|{item.get('date') or ''}"


def _month(date: str | None) -> str:
    try:
        return datetime.fromisoformat(str(date)).strftime("%Y-%m")
    except ValueError:
        return datetime.now().strftime("%Y-%m")


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
        """Pick items never imported from this device and not already in the library.

        A library file with the same name and byte size counts as present: iPhone
        originals are multi-megabyte, so an unrelated file matching both is not a
        realistic risk, and it spares re-copying photos the user copied by hand.
        """
        with self._db() as db:
            known = {row[0] for row in db.execute("SELECT item FROM imported WHERE device_id=?", (device_id,))}
        with self.catalog.connection() as db:
            library = {(row["name"].casefold(), row["size"]) for row in db.execute("SELECT name,size FROM photos")}
        selected, skipped_known, skipped_library = [], 0, 0
        for item in items:
            if _item_id(item) in known:
                skipped_known += 1
            elif (str(item["name"]).casefold(), int(item["size"])) in library:
                skipped_library += 1
            else:
                selected.append(item["key"])
        return {"selected": selected, "skipped_known": skipped_known, "skipped_library": skipped_library}

    def commit(self, device_id: str, destination: str, staging: str, items: list[dict], progress=None) -> dict:
        """File copied items into destination/YYYY-MM, dropping exact (SHA-256) duplicates.

        `staging` is destination/.wap-importing/<session>; the app copies each
        DCIM folder (100APPLE, ...) into its own subfolder there. Item `files`
        are paths relative to `staging`.
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
        suffixes: dict[tuple[Path, str], str] = {}
        records, imported, duplicates = [], 0, 0
        now = datetime.now(timezone.utc).isoformat()
        groups = [(item, [self._staged(stage, name) for name in item.get("files", [])]) for item in items]
        # Safety net: a copied file nobody listed (e.g. from an interrupted run)
        # is still filed, dated by its timestamp, rather than left in staging.
        listed = {path for _, files in groups for path in files}
        for path in sorted(stage.rglob("*")):
            if path.is_file() and path.suffix != ".part" and path not in listed:
                groups.append(({"date": datetime.fromtimestamp(path.stat().st_mtime).isoformat()}, [path]))
        for done, (item, files) in enumerate(groups, 1):
            keep = []
            for path in (p for p in files if p.is_file()):
                digest = sha256_file(path)
                if self._in_library(path, digest, by_size, digests):
                    path.unlink()  # Our own fresh copy; the library already has this file.
                    duplicates += 1
                else:
                    keep.append((path, digest))
            placed = self._place(dest / _month(item.get("date")), keep, suffixes)
            for path, digest in placed:
                by_size.setdefault(path.stat().st_size, []).append(str(path))
                digests[str(path)] = digest
            imported += len(placed)
            if files and "name" in item:
                records.append((device_id, _item_id(item), str(placed[0][0]) if placed else None,
                                "imported" if placed else "duplicate", now))
            if progress and done % 10 == 0:
                progress({"stage": "importing", "done": done, "total": len(groups)})
        with self._db() as db:
            db.executemany("INSERT OR REPLACE INTO imported VALUES (?,?,?,?,?)", records)
        self._remove_empty_folders(stage)
        return {"imported": imported, "duplicates": duplicates, "items": len(records),
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
    def _in_library(path: Path, digest: str, by_size: dict[int, list[str]], digests: dict[str, str]) -> bool:
        for candidate in by_size.get(path.stat().st_size, []):
            if candidate not in digests:
                try:
                    digests[candidate] = sha256_file(Path(candidate))
                except OSError:
                    continue  # Offline or moved library file: cannot prove a duplicate.
            if digests[candidate] == digest:
                return True
        return False

    @staticmethod
    def _place(folder: Path, files: list[tuple[Path, str]], suffixes: dict[tuple[Path, str], str]):
        """Move one item's files (still, Live motion, sidecars) without overwriting.

        Files sharing a stem get the same suffix, so a Live Photo pair stays paired
        even when its name is taken by an older photo (iPhone numbers wrap at 9999).
        """
        if not files:
            return []
        folder.mkdir(parents=True, exist_ok=True)
        placed = []
        for path, digest in files:
            stem = path.stem
            suffix = suffixes.get((folder, stem.casefold()), "")
            number = 1
            while (target := folder / f"{stem}{suffix}{path.suffix}").exists():
                number += 1
                suffix = f" ({number})"
            os.rename(path, target)  # Same volume; fails instead of replacing.
            suffixes[(folder, stem.casefold())] = suffix
            placed.append((target, digest))
        return placed

    def _ensure_library_folder(self, dest: Path) -> str:
        roots = [Path(p) for p in self.catalog.folders()]
        covering = next((r for r in roots if r == dest or r in dest.parents), None)
        if covering is None:
            self.catalog.add_folder(str(dest))  # Raises if dest would swallow an existing root.
            covering = dest
        return str(covering)
