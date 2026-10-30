"""Authenticated, resumable native-iPhone photo resource transfer protocol v1.

Each acknowledged chunk is durable. Source photos are NEVER deleted or modified.
The staging directory is local application data and is not a photo-library root.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import SUPPORTED_EXTENSIONS

VIDEO_EXTENSIONS = frozenset({".mov", ".mp4", ".m4v"})
RAW_EXTENSIONS = frozenset({".dng"})
CHUNK_BYTES = 8 * 1024 * 1024
MAX_RESOURCE_BYTES = 100 * 1024 ** 3
MAX_PENDING_PER_DEVICE = 100
ALLOWED_RESOURCE_TYPES = frozenset({"photo", "video", "pairedVideo", "fullSizePhoto", "fullSizeVideo"})
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{20,90}$")
_SHA_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DEVICE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{8,90}$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SyncFailure(Exception):
    def __init__(self, status: int, message: str, **details):
        super().__init__(message)
        self.status = status
        self.details = details


class SyncManager:
    def __init__(self, catalog, inbox: Path):
        self.catalog = catalog
        self.inbox = Path(inbox).expanduser().resolve(strict=False)
        self.staging = (catalog.home / "sync-staging").resolve()
        self.lock = threading.RLock()
        self._initialize()

    def _initialize(self):
        with self.catalog.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sync_devices (
                    device_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    token_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL, revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sync_transfers (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES sync_devices(device_id),
                    asset_id TEXT NOT NULL, resource_type TEXT NOT NULL,
                    filename TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    offset INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, stored_path TEXT,
                    UNIQUE(device_id, asset_id, resource_type, sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_sync_transfers_device ON sync_transfers(device_id, status);
                CREATE TABLE IF NOT EXISTS sync_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @staticmethod
    def _valid_name(filename: str, resource_type: str) -> str:
        if not isinstance(filename, str) or not 1 <= len(filename) <= 250:
            raise SyncFailure(400, "Invalid filename")
        # Never decode percent-escapes or accept path components from remote clients.
        if filename in {".", ".."} or "/" in filename or "\\" in filename or "\x00" in filename:
            raise SyncFailure(400, "Filename must not contain a path")
        extension = Path(filename).suffix.lower()
        if resource_type in {"video", "pairedVideo", "fullSizeVideo"}:
            allowed = VIDEO_EXTENSIONS
        else:
            allowed = SUPPORTED_EXTENSIONS | RAW_EXTENSIONS
        if extension not in allowed:
            raise SyncFailure(400, "Unsupported resource extension")
        safe = re.sub(r'[<>:"|?*\x00-\x1f]', "_", filename).strip(" .")
        if not safe:
            raise SyncFailure(400, "Invalid filename")
        return safe[:max(1, 180 - len(extension))] + extension if len(safe) > 180 else safe

    def pair(self, device_id: str, name: str) -> dict:
        if not isinstance(device_id, str) or not _DEVICE_PATTERN.fullmatch(device_id):
            raise SyncFailure(400, "Invalid device identifier")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise SyncFailure(400, "Invalid device name")
        token = secrets.token_urlsafe(36)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = utcnow()
        with self.lock, self.catalog.connection() as db:
            db.execute("""INSERT INTO sync_devices(device_id,name,token_sha256,created_at,last_seen_at,revoked_at)
                          VALUES(?,?,?,?,?,NULL) ON CONFLICT(device_id) DO UPDATE SET
                          name=excluded.name,token_sha256=excluded.token_sha256,
                          last_seen_at=excluded.last_seen_at,revoked_at=NULL""",
                       (device_id, name.strip(), digest, now, now))
        return {"device_id": device_id, "device_token": token, "protocol": 1}

    def authorized_device(self, token: str) -> str | None:
        if not isinstance(token, str) or not 30 <= len(token) <= 160:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.catalog.connection() as db:
            row = db.execute("""SELECT device_id FROM sync_devices
                                WHERE token_sha256=? AND revoked_at IS NULL""", (digest,)).fetchone()
        return row["device_id"] if row else None

    def mark_seen(self, device_id: str):
        with self.catalog.connection() as db:
            db.execute("UPDATE sync_devices SET last_seen_at=? WHERE device_id=? AND revoked_at IS NULL",
                       (utcnow(), device_id))

    def _paused(self, db) -> bool:
        row = db.execute("SELECT value FROM sync_settings WHERE key='paused'").fetchone()
        return bool(row and row["value"] == "true")

    def set_paused(self, paused: bool) -> dict:
        with self.lock, self.catalog.connection() as db:
            db.execute("INSERT INTO sync_settings(key,value) VALUES('paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       ("true" if paused else "false",))
        return {"paused": paused}

    def revoke_device(self, device_id: str) -> dict:
        with self.lock, self.catalog.connection() as db:
            result = db.execute("UPDATE sync_devices SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                                (utcnow(), device_id))
            return {"revoked": bool(result.rowcount), "device_id": device_id}

    def dashboard(self) -> dict:
        with self.catalog.connection() as db:
            devices = [dict(row) for row in db.execute("""
                SELECT d.device_id,d.name,d.created_at,d.last_seen_at,d.revoked_at,
                  COUNT(t.id) AS transfers,
                  SUM(CASE WHEN t.status='complete' THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN t.status='pending' THEN 1 ELSE 0 END) AS pending,
                  COALESCE(SUM(CASE WHEN t.status='pending' THEN t.offset ELSE t.size END),0) AS bytes_received,
                  COALESCE(SUM(t.size),0) AS bytes_total
                FROM sync_devices d LEFT JOIN sync_transfers t ON t.device_id=d.device_id
                GROUP BY d.device_id ORDER BY d.last_seen_at DESC
            """)]
            total = db.execute("""SELECT COUNT(*) AS resources,
                COALESCE(SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END),0) AS completed,
                COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0) AS pending,
                COALESCE(SUM(CASE WHEN status='complete' THEN size ELSE offset END),0) AS bytes_received
                FROM sync_transfers""").fetchone()
            recent = [dict(row) for row in db.execute("""SELECT id,device_id,asset_id,resource_type,
                filename,size,offset,status,updated_at FROM sync_transfers
                ORDER BY updated_at DESC LIMIT 30""")]
            return {"paused": self._paused(db), "devices": devices, "totals": dict(total),
                    "recent_transfers": recent, "chunk_bytes": CHUNK_BYTES}

    def _transfer(self, db, transfer_id: str, device_id: str):
        if not _ID_PATTERN.fullmatch(transfer_id):
            raise SyncFailure(404, "Transfer not found")
        row = db.execute("SELECT * FROM sync_transfers WHERE id=? AND device_id=?",
                         (transfer_id, device_id)).fetchone()
        if row is None:
            raise SyncFailure(404, "Transfer not found")
        return row

    def _info(self, row) -> dict:
        return {"transfer_id": row["id"], "status": row["status"], "offset": row["offset"],
                "size": row["size"], "chunk_bytes": CHUNK_BYTES}

    def begin(self, device_id: str, body: dict) -> dict:
        try:
            asset_id = body["asset_id"]
            resource_type = body["resource_type"]
            size = body["size"]
            digest = body["sha256"]
            filename = body["filename"]
        except (TypeError, KeyError) as exc:
            raise SyncFailure(400, "Missing transfer fields") from exc
        if not isinstance(asset_id, str) or not 1 <= len(asset_id) <= 250 or "\x00" in asset_id:
            raise SyncFailure(400, "Invalid asset identifier")
        if not isinstance(resource_type, str) or resource_type not in ALLOWED_RESOURCE_TYPES:
            raise SyncFailure(400, "Unsupported PhotoKit resource type")
        if type(size) is not int or not 0 < size <= MAX_RESOURCE_BYTES:
            raise SyncFailure(400, "Invalid resource size")
        if not isinstance(digest, str) or not _SHA_PATTERN.fullmatch(digest):
            raise SyncFailure(400, "Invalid resource SHA-256")
        filename = self._valid_name(filename, resource_type)
        now = utcnow()
        with self.lock, self.catalog.connection() as db:
            if self._paused(db):
                raise SyncFailure(423, "Sync is paused by Windows owner")
            row = db.execute("""SELECT * FROM sync_transfers WHERE device_id=? AND asset_id=?
                AND resource_type=? AND sha256=?""", (device_id, asset_id, resource_type, digest)).fetchone()
            if row and row["status"] == "pending":
                stage = self.staging / (row["id"] + ".part")
                # Lost or truncated staging data cannot be resumed safely.
                if not stage.exists() or stage.stat().st_size < row["offset"]:
                    db.execute("UPDATE sync_transfers SET status='failed' WHERE id=?", (row["id"],))
                    row = db.execute("SELECT * FROM sync_transfers WHERE id=?", (row["id"],)).fetchone()
            if row and row["status"] != "failed":
                return self._info(row)
            if row:
                db.execute("DELETE FROM sync_transfers WHERE id=?", (row["id"],))
                (self.staging / (row["id"] + ".part")).unlink(missing_ok=True)
            pending = db.execute("""SELECT COUNT(*) FROM sync_transfers WHERE device_id=? AND status='pending'""",
                                 (device_id,)).fetchone()[0]
            if pending >= MAX_PENDING_PER_DEVICE:
                raise SyncFailure(429, "Too many pending transfers; complete or retry existing transfers")
            transfer_id = secrets.token_urlsafe(24)
            db.execute("""INSERT INTO sync_transfers
                (id,device_id,asset_id,resource_type,filename,size,sha256,offset,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,0,'pending',?,?)""",
                (transfer_id, device_id, asset_id, resource_type, filename, size, digest, now, now))
            self.staging.mkdir(parents=True, exist_ok=True)
            # The file is created upfront, allowing precise reconciliation after a crash.
            (self.staging / (transfer_id + ".part")).touch(exist_ok=True)
            return {"transfer_id": transfer_id, "status": "pending", "offset": 0,
                    "size": size, "chunk_bytes": CHUNK_BYTES}

    def status(self, device_id: str, transfer_id: str) -> dict:
        with self.lock, self.catalog.connection() as db:
            return self._info(self._transfer(db, transfer_id, device_id))

    def write_chunk(self, device_id: str, transfer_id: str, offset: int, data: bytes, digest: str) -> dict:
        if not isinstance(offset, int) or offset < 0:
            raise SyncFailure(400, "Invalid upload offset")
        if not isinstance(digest, str) or not _SHA_PATTERN.fullmatch(digest):
            raise SyncFailure(400, "Invalid chunk SHA-256")
        if not 0 < len(data) <= CHUNK_BYTES:
            raise SyncFailure(413, "Chunk must be 1 to 8 MiB")
        if hashlib.sha256(data).hexdigest() != digest:
            raise SyncFailure(422, "Chunk checksum mismatch")
        with self.lock, self.catalog.connection() as db:
            if self._paused(db):
                raise SyncFailure(423, "Sync is paused by Windows owner")
            row = self._transfer(db, transfer_id, device_id)
            if row["status"] == "complete":
                return self._info(row)
            if row["status"] != "pending":
                raise SyncFailure(409, "Transfer is not pending")
            if row["offset"] != offset:
                raise SyncFailure(409, "Offset mismatch; retrieve transfer status and resume", expected_offset=row["offset"])
            if offset + len(data) > row["size"]:
                raise SyncFailure(413, "Chunk exceeds declared resource size")
            target = self.staging / (transfer_id + ".part")
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "r+b" if target.exists() else "w+b") as out:
                # An interrupted process may have written bytes before recording the offset.
                out.truncate(offset)
                out.seek(offset)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            new_offset = offset + len(data)
            db.execute("UPDATE sync_transfers SET offset=?,updated_at=? WHERE id=?",
                       (new_offset, utcnow(), transfer_id))
            return {"transfer_id": transfer_id, "offset": new_offset, "size": row["size"], "status": "pending"}

    def complete(self, device_id: str, transfer_id: str) -> dict:
        with self.lock, self.catalog.connection() as db:
            if self._paused(db):
                raise SyncFailure(423, "Sync is paused by Windows owner")
            row = self._transfer(db, transfer_id, device_id)
            if row["status"] == "complete":
                return {"status": "complete", "transfer_id": transfer_id}
            if row["offset"] != row["size"]:
                raise SyncFailure(409, "Upload incomplete", expected_offset=row["offset"])
            source = self.staging / (transfer_id + ".part")
            if not source.exists():
                raise SyncFailure(409, "Staged resource unavailable; restart transfer")
            hashed = hashlib.sha256()
            with source.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hashed.update(block)
            if hashed.hexdigest() != row["sha256"]:
                source.unlink(missing_ok=True)
                db.execute("UPDATE sync_transfers SET status='failed',updated_at=? WHERE id=?", (utcnow(), transfer_id))
                mismatch = True
            else:
                mismatch = False
            if not mismatch:
                self.inbox.mkdir(parents=True, exist_ok=True)
                extension = Path(row["filename"]).suffix.lower()
                dest_dir = self.inbox / row["sha256"][:2]
                dest_dir.mkdir(parents=True, exist_ok=True)
                # Both Live Photo resources share asset_id in metadata; files stay distinct.
                basename = Path(row["filename"]).stem[:65]
                dest = dest_dir / (basename + "-" + row["sha256"] + extension)
                if dest.exists():
                    existing_hash = hashlib.sha256()
                    with dest.open("rb") as inp:
                        for part in iter(lambda: inp.read(1024 * 1024), b""):
                            existing_hash.update(part)
                    if existing_hash.hexdigest() != row["sha256"]:
                        raise SyncFailure(409, "Existing destination collision; no file was overwritten")
                    source.unlink(missing_ok=True)
                else:
                    os.replace(source, dest)
                db.execute("UPDATE sync_transfers SET status='complete',stored_path=?,updated_at=? WHERE id=?",
                           (str(dest), utcnow(), transfer_id))
        if mismatch:
            raise SyncFailure(422, "Final SHA-256 mismatch, transfer reset")
        # Update catalog best-effort: original HEIC can be indexed without thumbnail decoder;
        # videos remain safely stored, with metadata visible in Sync Management only.
        if extension in SUPPORTED_EXTENSIONS:
            try:
                roots = self.catalog.folders()
                root = next((p for p in roots if Path(p) == self.inbox or Path(p) in dest.parents), None)
                if not root:
                    self.catalog.add_folder(str(self.inbox))
                    root = str(self.inbox)
                with self.catalog.connection() as db:
                    self.catalog._upsert(db, root, dest)
            except (ValueError, OSError) as exc:
                # Import success does not depend on thumbnail decoder / catalog availability.
                import logging
                logging.getLogger(__name__).warning("Imported image pending catalog scan: %s", exc)
        return {"status": "complete", "transfer_id": transfer_id}
