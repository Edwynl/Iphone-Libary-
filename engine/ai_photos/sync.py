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
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import SUPPORTED_EXTENSIONS
from .file_safety import SafeDirectory, check_path, ChecksumMismatch
from .duplicates import sha256_file

VIDEO_EXTENSIONS = frozenset({".mov", ".mp4", ".m4v"})
RAW_EXTENSIONS = frozenset({".dng"})
CHUNK_BYTES = 8 * 1024 * 1024
MAX_RESOURCE_BYTES = 100 * 1024 ** 3
MAX_PENDING_PER_DEVICE = 100
ALLOWED_RESOURCE_TYPES = frozenset({"photo", "alternatePhoto", "video", "pairedVideo", "fullSizePhoto", "fullSizeVideo"})
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
        # Guards short DB/staging mutations only. Whole-file hashing and the
        # final copy run outside it so one large resource never stalls others.
        self.lock = threading.RLock()
        self._finalizing = set()
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
            columns = {row['name'] for row in db.execute('PRAGMA table_info(sync_transfers)')}
            for column in ('asset_created_at', 'asset_modified_at', 'resource_key'):
                if column not in columns:
                    db.execute(f'ALTER TABLE sync_transfers ADD COLUMN {column} TEXT')
            db.execute("INSERT OR IGNORE INTO sync_settings(key,value) VALUES('receiver_id',?)",
                       (secrets.token_urlsafe(24),))

    @property
    def receiver_id(self):
        with self.catalog.connection() as db:
            return db.execute("SELECT value FROM sync_settings WHERE key='receiver_id'").fetchone()[0]

    def _stored_valid(self, row):
        try:
            path = check_path(Path(row['stored_path'] or ''))
            return path.is_file() and path.stat().st_size == row['size'] and sha256_file(path) == row['sha256']
        except (OSError, ValueError):
            return False

    def _mark_failed_if_unchanged(self, row):
        # Only fail the exact completed version that was hashed without the lock.
        with self.lock, self.catalog.connection() as db:
            db.execute('''UPDATE sync_transfers SET status='failed',updated_at=?
                WHERE id=? AND status='complete' AND stored_path IS ?''',
                (utcnow(), row['id'], row['stored_path']))

    def _ensure_device(self, db, device_id):
        active = db.execute('SELECT 1 FROM sync_devices WHERE device_id=? AND revoked_at IS NULL',
                            (device_id,)).fetchone()
        if not active:
            raise SyncFailure(401, 'Device has been revoked; pair again')

    def verify(self, device_id, body):
        with self.catalog.connection() as db:
            self._ensure_device(db, device_id)
            row = db.execute('''SELECT * FROM sync_transfers WHERE device_id=?
                AND asset_id=? AND resource_type=? AND sha256=?''',
                (device_id, body.get('asset_id'), body.get('resource_type'), body.get('sha256'))).fetchone()
        valid = bool(row and row['status'] == 'complete' and self._stored_valid(row))
        if row and row['status'] == 'complete' and not valid:
            self._mark_failed_if_unchanged(row)
        return {'complete': valid, 'receiver_id': self.receiver_id}

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
        return {"device_id": device_id, "device_token": token, "protocol": 1, "receiver_id": self.receiver_id}

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

    def _transfer(self, db, transfer_id: str, device_id: str, *, require_active=True):
        if require_active:
            self._ensure_device(db, device_id)
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
        metadata = []
        for key in ('asset_created_at', 'asset_modified_at', 'resource_key'):
            value = body.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 1024):
                raise SyncFailure(400, 'Invalid resource metadata')
            if value and key != 'resource_key':
                try:
                    datetime.fromisoformat(value.replace('Z', '+00:00'))
                except ValueError as exc:
                    raise SyncFailure(400, 'Invalid asset timestamp') from exc
            metadata.append(value)
        now = utcnow()
        lookup = ("""SELECT * FROM sync_transfers WHERE device_id=? AND asset_id=?
            AND resource_type=? AND sha256=?""", (device_id, asset_id, resource_type, digest))
        # Hash an existing completed import before taking the lock.
        with self.catalog.connection() as db:
            found = db.execute(*lookup).fetchone()
        checked = {}
        if found and found["status"] == "complete":
            checked[(found["id"], found["stored_path"])] = self._stored_valid(found)
        with self.lock, self.catalog.connection() as db:
            self._ensure_device(db, device_id)
            if self._paused(db):
                raise SyncFailure(423, "Sync is paused by Windows owner")
            row = db.execute(*lookup).fetchone()
            if row and row["status"] == "complete":
                valid = checked.get((row["id"], row["stored_path"]))
                if valid is None:  # Changed concurrently; rare, so recheck here.
                    valid = self._stored_valid(row)
                if not valid:
                    db.execute("UPDATE sync_transfers SET status='failed' WHERE id=?", (row["id"],))
                    row = db.execute("SELECT * FROM sync_transfers WHERE id=?", (row["id"],)).fetchone()
            if row and row["status"] == "pending":
                stage = self.staging / (row["id"] + ".part")
                # Lost or truncated staging data cannot be resumed safely.
                if not stage.exists() or stage.stat().st_size < row["offset"]:
                    db.execute("UPDATE sync_transfers SET status='failed' WHERE id=?", (row["id"],))
                    row = db.execute("SELECT * FROM sync_transfers WHERE id=?", (row["id"],)).fetchone()
            if row and row["status"] != "failed":
                db.execute('''UPDATE sync_transfers SET asset_created_at=COALESCE(?,asset_created_at),
                    asset_modified_at=COALESCE(?,asset_modified_at),resource_key=COALESCE(?,resource_key) WHERE id=?''',
                    (*metadata, row['id']))
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
            db.execute('UPDATE sync_transfers SET asset_created_at=?,asset_modified_at=?,resource_key=? WHERE id=?',
                       (*metadata, transfer_id))
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
            if shutil.disk_usage(target.parent).free < len(data) + 1024**3:
                raise SyncFailure(507, 'Insufficient staging storage; at least 1 GiB must remain free')
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

    def cancel(self, device_id: str, transfer_id: str, *, owner=False) -> dict:
        # The Windows owner may discard staging of a revoked device; a device
        # itself is already rejected by authentication once revoked.
        with self.lock:
            with self.catalog.connection() as db:
                row = self._transfer(db, transfer_id, device_id, require_active=not owner)
                if transfer_id in self._finalizing:
                    raise SyncFailure(409, 'Transfer is being finalized and cannot be cancelled')
                if row['status'] == 'complete':
                    raise SyncFailure(409, 'Completed originals cannot be cancelled or deleted')
                db.execute("UPDATE sync_transfers SET status='failed',offset=0,updated_at=? WHERE id=?",
                           (utcnow(), transfer_id))
            (self.staging / (transfer_id + '.part')).unlink(missing_ok=True)
        return {'cancelled': True}

    def _finalize(self, row) -> Path:
        """Copy and verify a fully staged resource; runs without the global lock."""
        transfer_id = row['id']
        source = check_path(self.staging / (transfer_id + '.part'))
        if not source.is_file():
            raise SyncFailure(409, 'Staged resource unavailable; restart transfer')
        extension = Path(row['filename']).suffix.lower()
        dest_dir = self.inbox / row['sha256'][:2]
        # Content-addressed bytes; logical filenames and Live Photo links stay in DB.
        dest = dest_dir / (row['sha256'] + extension)
        with self.catalog.connection() as db:
            candidates = db.execute("SELECT * FROM sync_transfers WHERE sha256=? AND size=? AND status='complete'",
                                    (row['sha256'], row['size'])).fetchall()
        for candidate in candidates:
            if self._stored_valid(candidate):
                candidate_path = Path(candidate['stored_path'])
                if candidate_path.parent == dest_dir and candidate_path.suffix == extension:
                    dest = candidate_path
                    break
        # Verified bytes wait under a hidden, non-photo name in the destination
        # folder: scans index only photo extensions, and publishing is then
        # an atomic same-directory rename (never cross-directory/volume).
        private_name = '.' + transfer_id + '.verified'
        try:
            with SafeDirectory(dest_dir) as directory:
                try:
                    existing = directory.digest(dest.name)
                except FileNotFoundError:
                    existing = None
            if existing is not None and existing != row['sha256']:
                # Preserve a possibly user-edited/corrupt import. Recover to
                # a new path instead of overwriting it or blocking forever.
                dest = dest_dir / (row['sha256'] + '-recovered-' + secrets.token_hex(8) + extension)
                existing = None
            if existing is None:
                with SafeDirectory(dest_dir) as directory:
                    # The copy briefly needs the resource twice; fail before
                    # writing rather than with ENOSPC part-way. Staging is kept.
                    if shutil.disk_usage(directory.path).free < row['size'] + 1024**3:
                        raise SyncFailure(507, 'Insufficient import storage; free space and complete again',
                                          expected_offset=row['offset'])
                    directory.copy(source, private_name, expected=row['sha256'])
        except ChecksumMismatch:
            with self.lock, self.catalog.connection() as db:
                db.execute("UPDATE sync_transfers SET status='failed',updated_at=? WHERE id=?",
                           (utcnow(), transfer_id))
            source.unlink(missing_ok=True)
            raise SyncFailure(422, 'Final SHA-256 mismatch, transfer reset')
        private_written = existing is None
        publish = private_written
        try:
            while True:
                with self.lock, self.catalog.connection() as db:
                    # Revoke/pause may have happened during the unlocked copy. On
                    # refusal nothing becomes visible in the import folder and the
                    # staged bytes are kept for a later retry.
                    self._ensure_device(db, row['device_id'])
                    if self._paused(db):
                        raise SyncFailure(423, 'Sync is paused by Windows owner')
                    current = db.execute('SELECT status,offset FROM sync_transfers WHERE id=?', (transfer_id,)).fetchone()
                    if current is None or current['status'] != 'pending' or current['offset'] != row['size']:
                        raise SyncFailure(409, 'Transfer changed while finalizing; retrieve its status')
                    # Publish before the commit: after a crash in between, the
                    # retry finds these verified bytes and reuses them.
                    if not publish or self._publish(private_name, dest):
                        db.execute("UPDATE sync_transfers SET status='complete',stored_path=?,updated_at=? WHERE id=?",
                                   (str(dest), utcnow(), transfer_id))
                        break
                # Another transfer published this name meanwhile. Hash it
                # without the lock, then recheck everything above again.
                with SafeDirectory(dest_dir) as directory:
                    same = directory.digest(dest.name) == row['sha256']
                if same:
                    publish = False
                else:
                    dest = dest_dir / (row['sha256'] + '-recovered-' + secrets.token_hex(8) + extension)
        finally:
            if private_written:
                with SafeDirectory(dest_dir) as directory:
                    try:
                        directory._unlink(private_name)
                    except FileNotFoundError:
                        pass
        # Keep staged bytes until the durable DB commit; a crash can safely retry.
        source.unlink(missing_ok=True)
        return dest

    @staticmethod
    def _publish(private_name, dest) -> bool:
        """Atomic same-directory rename; False (no hashing here) if dest exists."""
        with SafeDirectory(dest.parent) as directory:
            try:
                directory._commit(private_name, dest.name, False)  # Never replaces.
                return True
            except FileExistsError:
                return False

    def complete(self, device_id: str, transfer_id: str) -> dict:
        with self.lock:
            with self.catalog.connection() as db:
                if self._paused(db):
                    raise SyncFailure(423, 'Sync is paused by Windows owner')
                row = dict(self._transfer(db, transfer_id, device_id))
            if transfer_id in self._finalizing:
                raise SyncFailure(409, 'Transfer is still being finalized; retry shortly',
                                  expected_offset=row['offset'])
            finalize = row['status'] == 'pending' and row['offset'] == row['size']
            if finalize:
                # Reserve the staged file: chunks and cancel are refused meanwhile.
                self._finalizing.add(transfer_id)
        if row['status'] == 'complete':
            if self._stored_valid(row):
                return {'status': 'complete', 'transfer_id': transfer_id}
            self._mark_failed_if_unchanged(row)
            raise SyncFailure(409, 'Completed file is missing or corrupt; begin the transfer again')
        if not finalize:
            raise SyncFailure(409, 'Upload incomplete', expected_offset=row['offset'])
        try:
            dest = self._finalize(row)
        finally:
            with self.lock:
                self._finalizing.discard(transfer_id)
        extension = Path(row['filename']).suffix.lower()
        if extension in SUPPORTED_EXTENSIONS:
            try:
                roots = self.catalog.folders()
                root = next((p for p in roots if Path(p) == self.inbox or Path(p) in dest.parents), None)
                if not root:
                    self.catalog.add_folder(str(self.inbox))
                    root = str(self.inbox)
                prepared = self.catalog.prepare(dest)
                with self.catalog.connection() as db:
                    self.catalog._upsert(db, root, dest, prepared=prepared)
                    db.execute('UPDATE photos SET name=? WHERE path=?', (row['filename'], str(dest)))
                    if row.get('asset_created_at'):
                        db.execute('UPDATE photos SET taken_at=COALESCE(taken_at,?) WHERE path=?',
                                   (row['asset_created_at'], str(dest)))
            except (ValueError, OSError):
                import logging
                logging.getLogger(__name__).exception('Imported image pending catalog scan')
        return {'status': 'complete', 'transfer_id': transfer_id}
