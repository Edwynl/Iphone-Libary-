"""Opt-in, loopback-only iPhone web upload and photo browsing service.

For remote access, proxy localhost through Tailscale Serve HTTPS. Never
expose this development server directly to the public internet.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .catalog import PhotoCatalog
from .config import SUPPORTED_EXTENSIONS

LOG = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def default_inbox() -> Path:
    return Path.home() / "Pictures" / "Windows AI Photos Imports"


def _safe_name(value: str) -> str:
    value = unquote(value)
    name = re.split(r"[/\\]", value)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    stem = Path(name).stem.strip(" .")[:70] or "photo"
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("Unsupported image extension")
    return stem + suffix


class MobileServer:
    def __init__(self, catalog: PhotoCatalog, *, inbox: Path | None = None,
                 token: str | None = None, host: str = "127.0.0.1", port: int = 8787):
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Mobile service must bind to loopback; use Tailscale Serve for remote access")
        self.catalog = catalog
        self.inbox = (inbox or default_inbox()).expanduser().resolve(strict=False)
        if catalog.home.resolve() == self.inbox or catalog.home.resolve() in self.inbox.parents:
            raise ValueError("Import folder must not live inside the internal app data directory")
        self.token = token or secrets.token_urlsafe(32)
        if len(self.token) < 32:
            raise ValueError("Use a strong 32+ character pairing token")
        self.httpd = ThreadingHTTPServer((host, port), self._handler_class())
        self.httpd.daemon_threads = True
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
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=5)

    def import_upload(self, source: Path, filename: str, digest: str) -> dict:
        """Move completed upload atomically into an explicit user-visible Pictures folder."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        filename = _safe_name(filename)
        with self.catalog.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS iphone_imports (
                sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, original_name TEXT NOT NULL
            )""")
            row = db.execute("SELECT path FROM iphone_imports WHERE sha256=?", (digest,)).fetchone()
            if row and Path(row["path"]).is_file():
                return {"status": "duplicate", "name": filename}
            folder = self.inbox / digest[:2]
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / (Path(filename).stem[:65] + "-" + digest + Path(filename).suffix.lower())
            # Naming includes full SHA. A single duplicate upload never replaces files.
            if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Upload target collision; original retained")
            if not dest.exists():
                os.replace(source, dest)
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
        with self.catalog.connection() as db:
            photo_id, _, _ = self.catalog._upsert(db, root, dest)
        return {"status": "imported", "photo_id": photo_id, "name": filename}

    def _handler_class(self):
        class Handler(BaseHTTPRequestHandler):
            server_version = "WindowsAIPhotos/0.1"
            def log_message(self, fmt, *args):
                LOG.info("mobile: " + fmt, *args)

            def _common_headers(self, ctype: str, length: int):
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'none'; "
                                 "script-src 'self'; style-src 'self'; img-src 'self' blob:; "
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

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in {"/", "/app.js", "/app.css"}:
                    name = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css"}[path]
                    data = files("ai_photos").joinpath("mobile_web", name).read_bytes()
                    mime = "text/html; charset=utf-8" if path == "/" else (
                        "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
                    return self._send(200, data, mime)
                if not self._auth():
                    return
                if path == "/api/photos":
                    results = self.server.mobile.catalog.browse(limit=60)
                    safe = [{"id": p["id"], "name": p["name"], "has_thumb": bool(p["thumb_path"])}
                            for p in results]
                    return self._json(200, {"photos": safe})
                if path == "/api/status":
                    return self._json(200, self.server.mobile.catalog.stats())
                if path == "/api/thumb":
                    try:
                        photo_id = int(parse_qs(urlsplit(self.path).query)["id"][0])
                    except (ValueError, KeyError, IndexError):
                        return self._json(400, {"error": "Invalid ID"})
                    with self.server.mobile.catalog.connection() as db:
                        row = db.execute("SELECT thumb_path FROM photos WHERE id=?", (photo_id,)).fetchone()
                    if row and row["thumb_path"]:
                        target = Path(row["thumb_path"]).resolve()
                        thumbs = self.server.mobile.catalog.thumbs_dir.resolve()
                        if thumbs in target.parents and target.is_file():
                            return self._send(200, target.read_bytes(), "image/jpeg")
                    return self._json(404, {"error": "Thumbnail unavailable"})
                return self._json(404, {"error": "Not found"})

            def do_POST(self):
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
