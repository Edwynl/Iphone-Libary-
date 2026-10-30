"""Newline-delimited JSON RPC service over stdin/stdout, designed for WinUI.

Only the explicit `install_model` command uses the network. All other actions
read/write local files, model weights and SQLite. Stdout contains JSON only.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Any

from .catalog import PhotoCatalog
from .semantic import Siglip2Encoder
from .duplicates import find_duplicates
from .backup import backup_local, backup_s3
from .mobile import MobileServer, default_inbox
from .sync import SyncManager
from .organize import virtual_albums

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, catalog: PhotoCatalog | None = None, encoder=None, output=None):
        self.catalog = catalog or PhotoCatalog()
        self.encoder = encoder if encoder is not None else Siglip2Encoder()
        self.output = output or sys.stdout
        self._send_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._busy = False
        self._search_lock = threading.Lock()
        self.mobile_server = None
        self.sync_manager = SyncManager(self.catalog, default_inbox())

    def send(self, payload: dict) -> None:
        with self._send_lock:
            self.output.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            self.output.flush()

    def _background(self, request_id: Any, action: str, params: dict) -> None:
        with self._job_lock:
            if self._busy:
                self.send({"id": request_id, "ok": False, "error": "Another operation is in progress"})
                return
            self._busy = True

        def run():
            try:
                if action == "scan":
                    encoder = self.encoder if self.encoder.installed else None
                    result = self.catalog.scan(
                        encoder=encoder,
                        progress=lambda e: self.send({"event": "progress", "data": e}))
                elif action == "install_model":
                    self.send({"event": "progress", "data": {"stage": "installing", "message": "Downloading and preparing AI model"}})
                    self.encoder.install()
                    result = {"installed": True}
                elif action == "duplicates":
                    result = find_duplicates(self.catalog, similar_distance=int(params.get("distance", 4)))
                elif action == "backup_local":
                    result = backup_local(self.catalog, params["target"], apply=bool(params.get("apply", False)))
                elif action == "backup_s3":
                    result = backup_s3(self.catalog, bucket=params["bucket"],
                        endpoint_url=params.get("endpoint_url"), prefix=params.get("prefix", "windows-ai-photos"),
                        apply=bool(params.get("apply", False)))
                else:
                    raise ValueError("Unknown background action")
                self.send({"id": request_id, "ok": True, "data": result})
            except Exception as exc:
                LOG.exception("Worker job %s failed", action)
                self.send({"id": request_id, "ok": False, "error": str(exc)})
            finally:
                with self._job_lock:
                    self._busy = False
        threading.Thread(target=run, name=action, daemon=True).start()

    def handle(self, request: dict) -> None:
        request_id = request.get("id")
        try:
            action = request.get("action")
            params = request.get("params") or {}
            if action in ("scan", "install_model", "duplicates", "backup_local", "backup_s3"):
                self._background(request_id, action, params)
                return
            if action == 'search' and self.encoder.installed:
                if not self._search_lock.acquire(blocking=False):
                    raise RuntimeError('Search already running; retry when it completes')
                def search():
                    try:
                        result = self.catalog.search(params.get('query', ''), params.get('limit', 120), self.encoder)
                        self.send({'id': request_id, 'ok': True, 'data': result})
                    except Exception as exc:
                        self.send({'id': request_id, 'ok': False, 'error': str(exc)})
                    finally:
                        self._search_lock.release()
                threading.Thread(target=search, name='search', daemon=True).start()
                return
            if action == "start_mobile":
                if self.mobile_server is None:
                    self.mobile_server = MobileServer(self.catalog, port=int(params.get("port", 8787)),
                                                      sync_manager=self.sync_manager)
                    self.mobile_server.start()
                data = {"port": self.mobile_server.port, "pairing_token": self.mobile_server.token,
                        "url": f"http://127.0.0.1:{self.mobile_server.port}",
                        "tailscale_command": f"tailscale serve --bg {self.mobile_server.port}"}
            elif action == "stop_mobile":
                if self.mobile_server is not None:
                    self.mobile_server.close()
                    self.mobile_server = None
                data = {"stopped": True}
            elif action == "sync_dashboard":
                data = self.sync_manager.dashboard()
                data["server_running"] = self.mobile_server is not None
                data["server_port"] = self.mobile_server.port if self.mobile_server else None
            elif action == "sync_pause":
                data = self.sync_manager.set_paused(bool(params.get("paused", True)))
            elif action == "sync_revoke_device":
                data = self.sync_manager.revoke_device(params["device_id"])
            elif action == 'sync_cancel_transfer':
                data = self.sync_manager.cancel(params['device_id'], params['transfer_id'])
            elif action == "add_folder":
                data = self.catalog.add_folder(params["path"])
            elif action == "folders":
                data = {"folders": self.catalog.folders()}
            elif action == "remove_folder":
                data = self.catalog.remove_folder(params["path"])
            elif action == "organize":
                data = virtual_albums(self.catalog)
            elif action == "browse":
                data = {"photos": self.catalog.browse(params.get("limit", 120), params.get("offset", 0))}
            elif action == "search":
                encoder = self.encoder if self.encoder.installed else None
                data = self.catalog.search(params.get("query", ""), params.get("limit", 120), encoder)
            elif action == "status":
                data = self.catalog.stats(self.encoder.model_id)
                data["ai_installed"] = self.encoder.installed
                data['ai_error'] = getattr(self.encoder, 'last_error', '')
                data["busy"] = self._busy
            elif action == "ping":
                data = {"pong": True}
            else:
                raise ValueError(f"Unknown action: {action}")
            self.send({"id": request_id, "ok": True, "data": data})
        except Exception as exc:
            LOG.exception("Request failed")
            self.send({"id": request_id, "ok": False, "error": str(exc)})

    def serve(self, source=None) -> None:
        for raw in (source if source is not None else sys.stdin):
            try:
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise ValueError("Request must be a JSON object")
                self.handle(request)
            except Exception as exc:
                LOG.exception("Invalid request")
                self.send({"id": None, "ok": False, "error": str(exc)})
