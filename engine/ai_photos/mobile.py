"""Opt-in, loopback-only iPhone web upload and photo browsing service.

For remote access, proxy localhost through Tailscale Serve HTTPS. Never
expose this development server directly to the public internet.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .catalog import PhotoCatalog
from .config import SUPPORTED_EXTENSIONS
from .sync import CHUNK_BYTES, SyncFailure, SyncManager
from .file_safety import SafeDirectory
from .media import VIDEO_EXTENSIONS
from .duplicates import sha256_file

LOG = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
PAGE_SIZE = 60
MEDIA_LINK_HOURS = 12
# What a signed media link can open: the catalogued original, its Live Photo motion, its thumbnail.
_MEDIA_COLUMNS = {"original": "path", "motion": "companion", "thumb": "thumb_path"}
_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".bmp": "image/bmp",
    ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff", ".gif": "image/gif",
    ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif", ".dng": "image/x-adobe-dng",
    ".mov": "video/quicktime", ".mp4": "video/mp4", ".m4v": "video/x-m4v",
}


class BoundedHTTPServer(ThreadingHTTPServer):
    def __init__(self, *args, max_connections=16, socket_timeout=15, **kwargs):
        if not 1 <= max_connections <= 128 or not 0 < socket_timeout <= 120:
            raise ValueError('Invalid receiver connection limits')
        self._slots = threading.BoundedSemaphore(max_connections)
        self.socket_timeout = socket_timeout
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.socket_timeout)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            body = b'{"error":"Receiver busy; retry later","retryable":true}'
            try:
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: '
                                + str(len(body)).encode() + b'\r\n\r\n' + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def default_inbox() -> Path:
    override = os.environ.get('AI_PHOTOS_INBOX')
    return Path(override).expanduser() if override else Path.home() / "Pictures" / "Windows AI Photos Imports"


def _safe_name(value: str) -> str:
    value = unquote(value)
    name = re.split(r"[/\\]", value)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    stem = Path(name).stem.strip(" .")[:70] or "photo"
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS | VIDEO_EXTENSIONS:
        raise ValueError("Unsupported media extension")
    return stem + suffix


class MobileServer:
    def __init__(self, catalog: PhotoCatalog, *, inbox: Path | None = None,
                 token: str | None = None, host: str = "127.0.0.1", port: int = 8787,
                 sync_manager: SyncManager | None = None, max_connections=16, socket_timeout=15):
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Mobile service must bind to loopback; use Tailscale Serve for remote access")
        if port in {8765, 8766}:
            raise ValueError('Protected ports cannot host a photo receiver')
        self.catalog = catalog
        self.inbox = (inbox or default_inbox()).expanduser().resolve(strict=False)
        if catalog.home.resolve() == self.inbox or catalog.home.resolve() in self.inbox.parents:
            raise ValueError("Import folder must not live inside the internal app data directory")
        self.sync_manager = sync_manager or SyncManager(catalog, self.inbox)
        self.token = token or secrets.token_urlsafe(32)
        if len(self.token) < 32:
            raise ValueError("Use a strong 32+ character pairing token")
        self.httpd = BoundedHTTPServer((host, port), self._handler_class(),
                                      max_connections=max_connections, socket_timeout=socket_timeout)
        # Explicit Stop drains admitted writes before the receiver process exits.
        self.httpd.daemon_threads = False
        self.httpd.mobile = self  # type: ignore[attr-defined]
        self.thread = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="photo-mobile")
        self.thread.start()

    def close(self):
        if self.thread:
            self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=5)

    def media_signature(self, photo_id: int, part: str, expires: int) -> str:
        return hmac.new(self.token.encode(), f"{photo_id}:{part}:{expires}".encode(), hashlib.sha256).hexdigest()

    def media_url(self, photo_id: int, part: str) -> str:
        # <img> and <video> cannot send the Authorization header, so each link carries its own
        # signature for one file. It expires after 12-13 hours, rounded to the hour so a link
        # stays the same (and cacheable) for an hour.
        expires = (int(time.time()) // 3600 + MEDIA_LINK_HOURS + 1) * 3600
        signature = self.media_signature(photo_id, part, expires)
        return f"/api/media?id={photo_id}&part={part}&exp={expires}&sig={signature}"

    def media_file(self, photo_id: int, part: str) -> Path | None:
        """The file behind a media link, only if it still lies inside the library (or the thumbnail cache)."""
        with self.catalog.connection() as db:
            row = db.execute(f"SELECT {_MEDIA_COLUMNS[part]} FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not row or not row[0]:
            return None
        target = Path(row[0]).resolve()
        if part == "thumb":
            roots = [self.catalog.thumbs_dir.resolve()]
        else:
            roots = [Path(root).resolve() for root in self.catalog.folders()]
            if target.suffix.lower() not in _MEDIA_TYPES:
                return None
        if not target.is_file() or not any(root in target.parents for root in roots):
            return None
        return target

    def import_upload(self, source: Path, filename: str, digest: str) -> dict:
        """Move completed upload atomically into an explicit user-visible Pictures folder."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        filename = _safe_name(filename)
        with self.catalog.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS iphone_imports (
                sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, original_name TEXT NOT NULL
            )""")
            row = db.execute("SELECT path FROM iphone_imports WHERE sha256=?", (digest,)).fetchone()
            if row and Path(row["path"]).is_file() and sha256_file(Path(row['path'])) == digest:
                return {"status": "duplicate", "name": filename}
        # No decoding/copying is performed inside a SQLite write transaction.
        folder = self.inbox / digest[:2]
        dest = folder / (digest + Path(filename).suffix.lower())
        with SafeDirectory(folder) as directory:
            try:
                existing = directory.digest(dest.name)
            except FileNotFoundError:
                existing = None
            if existing is not None and existing != digest:
                dest = folder / (digest + '-recovered-' + secrets.token_hex(8) + Path(filename).suffix.lower())
                existing = None
            if existing is None:
                directory.copy(source, dest.name, expected=digest, replace=False)
        with self.catalog.connection() as db:
            db.execute("INSERT OR REPLACE INTO iphone_imports (sha256,path,original_name) VALUES(?,?,?)",
                       (digest, str(dest), filename))

        roots = self.catalog.folders()
        containing = [r for r in roots if Path(r) == dest or Path(r) in dest.parents]
        if containing:
            root = max(containing, key=len)
        else:
            try:
                self.catalog.add_folder(str(self.inbox))
                root = str(self.inbox)
            except ValueError as exc:
                # Avoid treating arbitrary folder registration errors as success.
                raise RuntimeError(f"Imported file saved but library registration failed: {exc}") from exc
        prepared = self.catalog.prepare(dest)
        with self.catalog.connection() as db:
            photo_id, _, _ = self.catalog._upsert(db, root, dest, prepared=prepared)
            db.execute('UPDATE photos SET name=? WHERE id=?', (filename, photo_id))
        return {"status": "imported", "photo_id": photo_id, "name": filename}

    def _handler_class(self):
        class Handler(BaseHTTPRequestHandler):
            server_version = "WindowsAIPhotos/0.1"
            def log_message(self, fmt, *args):
                # Request lines carry media link signatures: log the path only.
                args = tuple(arg.split("?", 1)[0] if isinstance(arg, str) else arg for arg in args)
                LOG.info("mobile: " + fmt, *args)

            def _common_headers(self, ctype: str, length: int, cache: str = "no-store"):
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'none'; "
                                 "script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self'; "
                                 "connect-src 'self'; base-uri 'none'; form-action 'none'")

            def _send(self, code: int, payload: bytes, ctype="application/json; charset=utf-8"):
                self.send_response(code)
                self._common_headers(ctype, len(payload))
                self.end_headers()
                self.wfile.write(payload)

            def _json(self, code: int, value: dict):
                self._send(code, json.dumps(value, ensure_ascii=False).encode("utf-8"))

            def _auth(self):
                supplied = self.headers.get("Authorization", "")
                if not secrets.compare_digest(supplied, "Bearer " + self.server.mobile.token):
                    self._json(401, {"error": "Pairing token required"})
                    return False
                return True

            def _media(self, query: dict):
                """Stream an original, Live Photo motion or thumbnail; byte ranges let Safari play video."""
                service = self.server.mobile
                try:
                    photo_id, part, signature = int(query["id"][0]), query["part"][0], query["sig"][0]
                    expires = int(query["exp"][0])
                except (ValueError, KeyError, IndexError):
                    return self._json(400, {"error": "Invalid media link"})
                if part not in _MEDIA_COLUMNS or not secrets.compare_digest(
                        signature, service.media_signature(photo_id, part, expires)):
                    return self._json(403, {"error": "Invalid media link"})
                if expires < time.time():
                    return self._json(403, {"error": "Media link expired; refresh the page"})
                target = service.media_file(photo_id, part)
                if target is None:
                    return self._json(404, {"error": "File unavailable"})
                size = target.stat().st_size
                start, end, status = 0, size - 1, 200
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
                if match and (match[1] or match[2]):
                    if match[1]:
                        start = int(match[1])
                        end = min(int(match[2]), size - 1) if match[2] else size - 1
                    else:
                        start = max(0, size - int(match[2]))
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self._common_headers("text/plain", 0)
                        return self.end_headers()
                    status = 206
                self.send_response(status)
                ctype = "image/jpeg" if part == "thumb" else _MEDIA_TYPES[target.suffix.lower()]
                self._common_headers(ctype, end - start + 1, cache="private, max-age=86400")
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                if "download" in query:
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(target.name))
                self.end_headers()
                remaining = end - start + 1
                try:
                    with target.open("rb") as f:
                        f.seek(start)
                        while remaining:
                            chunk = f.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Safari drops video requests as it seeks.

            def _sync_device(self) -> str | None:
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    self._json(401, {"error": "Device token required"})
                    return None
                device = self.server.mobile.sync_manager.authorized_device(auth[7:])
                if device is None:
                    self._json(401, {"error": "Device token invalid or revoked"})
                    return None
                return device

            def _read_sync_json(self) -> dict:
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise SyncFailure(400, "Invalid Content-Length") from exc
                if not 0 < size <= 4096:
                    raise SyncFailure(413, "JSON body must be 1 to 4096 bytes")
                try:
                    result = json.loads(self.rfile.read(size))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise SyncFailure(400, "Invalid JSON") from exc
                if not isinstance(result, dict):
                    raise SyncFailure(400, "JSON object required")
                return result

            def _sync_handle(self, method: str, path: str):
                manager = self.server.mobile.sync_manager
                try:
                    if path == "/api/sync/v1/pair" and method == "POST":
                        if not self._auth():
                            return
                        data = self._read_sync_json()
                        return self._json(201, manager.pair(data.get("device_id"), data.get("name")))
                    device = self._sync_device()
                    if device is None:
                        return
                    if path == '/api/sync/v1/verify' and method == 'POST':
                        return self._json(200, manager.verify(device, self._read_sync_json()))
                    if path == "/api/sync/v1/status" and method == "GET":
                        manager.mark_seen(device)
                        return self._json(200, {"device_id": device, "paused": manager.dashboard()["paused"],
                                                "receiver_id": manager.receiver_id})
                    if path == "/api/sync/v1/transfers" and method == "POST":
                        result = manager.begin(device, self._read_sync_json())
                        manager.mark_seen(device)
                        return self._json(200, result)
                    segments = path.split("/")
                    if len(segments) >= 6 and segments[:5] == ["", "api", "sync", "v1", "transfers"]:
                        transfer_id = segments[5]
                        if len(segments) == 6 and method == 'DELETE':
                            return self._json(200, manager.cancel(device, transfer_id))
                        if len(segments) == 6 and method == "GET":
                            manager.mark_seen(device)
                            return self._json(200, manager.status(device, transfer_id))
                        if len(segments) == 7 and segments[6] == "chunk" and method == "PUT":
                            try:
                                length = int(self.headers.get("Content-Length", "0"))
                                offset = int(self.headers.get("X-Upload-Offset", "-1"))
                            except ValueError as exc:
                                raise SyncFailure(400, "Invalid upload length or offset") from exc
                            if not 0 < length <= CHUNK_BYTES:
                                raise SyncFailure(413, "Chunk must be 1 to 8 MiB")
                            data = self.rfile.read(length)
                            if len(data) != length:
                                raise SyncFailure(400, "Incomplete chunk body")
                            response = manager.write_chunk(device, transfer_id, offset, data,
                                                           self.headers.get("X-Chunk-SHA256", ""))
                            return self._json(200, response)
                        if len(segments) == 7 and segments[6] == "complete" and method == "POST":
                            return self._json(200, manager.complete(device, transfer_id))
                    return self._json(404, {"error": "Unknown sync endpoint"})
                except SyncFailure as exc:
                    return self._json(exc.status, {"error": str(exc), **exc.details})
                except sqlite3.OperationalError:
                    LOG.exception('Sync database temporarily unavailable')
                    return self._json(503, {'error': 'Storage busy; retry after reconnecting', 'retryable': True})
                except ValueError:
                    return self._json(400, {'error': 'Invalid or unsafe storage path'})
                except (OSError, RuntimeError):
                    LOG.exception("Native sync I/O failure")
                    return self._json(500, {"error": "Sync storage error; source unchanged"})

            def do_PUT(self):
                path = urlsplit(self.path).path
                if path.startswith("/api/sync/v1/"):
                    return self._sync_handle("PUT", path)
                return self._json(404, {"error": "Not found"})

            def do_DELETE(self):
                path = urlsplit(self.path).path
                if path.startswith('/api/sync/v1/'):
                    return self._sync_handle('DELETE', path)
                return self._json(404, {'error': 'Not found'})

            def do_GET(self):
                path = urlsplit(self.path).path
                if path.startswith("/api/sync/v1/"):
                    return self._sync_handle("GET", path)
                if path in {"/", "/app.js", "/app.css"}:
                    name = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css"}[path]
                    data = files("ai_photos").joinpath("mobile_web", name).read_bytes()
                    mime = "text/html; charset=utf-8" if path == "/" else (
                        "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
                    return self._send(200, data, mime)
                if path == "/api/media":
                    return self._media(parse_qs(urlsplit(self.path).query, keep_blank_values=True))
                if not self._auth():
                    return
                if path == "/api/photos":
                    try:
                        offset = max(0, int(parse_qs(urlsplit(self.path).query).get("offset", ["0"])[0]))
                    except ValueError:
                        return self._json(400, {"error": "Invalid offset"})
                    service = self.server.mobile
                    # One extra row says whether another page follows.
                    results = service.catalog.browse(limit=PAGE_SIZE + 1, offset=offset)
                    page = [{"id": p["id"], "name": p["name"], "video": p["media_kind"] == "video",
                             "thumb": service.media_url(p["id"], "thumb") if p["thumb_path"] else None,
                             "original": service.media_url(p["id"], "original"),
                             "motion": service.media_url(p["id"], "motion") if p["companion"] else None}
                            for p in results[:PAGE_SIZE]]
                    more = len(results) > PAGE_SIZE
                    return self._json(200, {"photos": page, "next_offset": offset + PAGE_SIZE if more else None})
                if path == "/api/status":
                    return self._json(200, self.server.mobile.catalog.stats())
                return self._json(404, {"error": "Not found"})

            def do_POST(self):
                if urlsplit(self.path).path.startswith("/api/sync/v1/"):
                    return self._sync_handle("POST", urlsplit(self.path).path)
                if urlsplit(self.path).path != "/api/upload":
                    return self._json(404, {"error": "Not found"})
                if not self._auth():
                    return
                try:
                    filename = _safe_name(self.headers.get("X-Filename", ""))
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_UPLOAD_BYTES:
                        raise ValueError("Upload must be between 1 byte and 512 MiB")
                except (ValueError, TypeError) as exc:
                    return self._json(400, {"error": str(exc)})
                service = self.server.mobile
                service.inbox.mkdir(parents=True, exist_ok=True)
                temp_path = None
                try:
                    digest = hashlib.sha256()
                    with tempfile.NamedTemporaryFile(dir=service.inbox, prefix=".upload-", delete=False) as f:
                        temp_path = Path(f.name)
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise IOError("Incomplete upload")
                            f.write(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    result = service.import_upload(temp_path, filename, digest.hexdigest())
                    return self._json(201 if result["status"] == "imported" else 200, result)
                except Exception as exc:
                    LOG.exception("Import failed")
                    return self._json(500, {"error": "Upload failed; check Windows app logs"})
                finally:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)

        return Handler
