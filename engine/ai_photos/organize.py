"""Virtual time/folder albums: organize without moving or editing originals."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .catalog import PhotoCatalog


def virtual_albums(catalog: PhotoCatalog) -> dict:
    months = defaultdict(list)
    folders = defaultdict(int)
    formats = defaultdict(int)
    with catalog.connection() as db:
        rows = db.execute("SELECT id,name,folder,taken_at,mtime_ns FROM photos ORDER BY mtime_ns DESC,id DESC")
        for r in rows:
            timestamp = r["taken_at"] or ""
            # EXIF DateTimeOriginal format: YYYY:MM:DD HH:MM:SS
            if len(timestamp) >= 7 and timestamp[:4].isdigit() and timestamp[4] == ':' and timestamp[5:7].isdigit():
                yearmonth = timestamp[:7].replace(':', '-')
            else:
                try:
                    yearmonth = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).strftime('%Y-%m')
                except ValueError:
                    yearmonth = datetime.fromtimestamp(r["mtime_ns"] / 1e9, tz=timezone.utc).strftime('%Y-%m')
            months[yearmonth].append({"id": r["id"], "name": r["name"]})
            folders[r["folder"]] += 1
            formats[Path(r["name"]).suffix.lower()] += 1
    return {
        "timeline": [{"month": month, "count": len(items), "preview": items[:4]}
                     for month, items in sorted(months.items(), reverse=True)],
        "source_folders": [{"folder": key, "count": value}
                           for key, value in sorted(folders.items())],
        "formats": [{"extension": key, "count": value}
                    for key, value in sorted(formats.items())],
        "non_destructive": True,
    }
