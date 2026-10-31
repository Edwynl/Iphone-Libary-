"""Manual engine smoke test. Run: python -m ai_photos.cli --help"""
from __future__ import annotations

import argparse
import json
import sys

from .backup import backup_history, backup_local, backup_s3, restore_local
from .catalog import PhotoCatalog
from .duplicates import find_duplicates
from .mobile import MobileServer
from .organize import virtual_albums
from .receiver_agent import ReceiverController
from .semantic import Siglip2Encoder


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Windows AI Photos local library engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("add-folder")
    p.add_argument("path")
    sub.add_parser("folders")
    p = sub.add_parser("remove-folder", help="Forget a folder and its indexes, never source files")
    p.add_argument("path")
    sub.add_parser("scan")
    sub.add_parser("install-ai", help="Explicitly download ~1.5GB SigLIP2 model")
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("browse")
    p.add_argument("--limit", type=int, default=20)
    sub.add_parser("organize", help="Virtual timeline/folder albums; do not move originals")
    p = sub.add_parser("duplicates", help="Review SHA-256 exact and perceptual similar groups; never delete")
    p.add_argument("--distance", type=int, default=4)
    p = sub.add_parser("backup-local", help="Copy originals to independent storage (dry-run by default)")
    p.add_argument("target")
    p.add_argument("--apply", action="store_true", help="Explicitly perform copy")
    p = sub.add_parser('restore-local', help='Verify a manifest and restore into an empty independent folder')
    p.add_argument('backup')
    p.add_argument('target')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--snapshot', help='Restore a retained snapshot ID; default is the last successful backup')
    p = sub.add_parser('backup-history', help='List retained local snapshot IDs (file hashes are verified at restore)')
    p.add_argument('backup')
    p = sub.add_parser("backup-s3", help="Opt-in S3 compatible cloud backup (dry-run by default)")
    p.add_argument("--bucket", required=True)
    p.add_argument("--endpoint-url")
    p.add_argument("--prefix", default="windows-ai-photos")
    p.add_argument("--apply", action="store_true", help="Explicitly upload original photos")
    p = sub.add_parser("serve-iphone", help="Opt-in loopback-only iPhone uploader for Tailscale Serve")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--inbox")
    p = sub.add_parser('receiver-start', help='Explicitly start the independent background receiver for this login session')
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--inbox')
    sub.add_parser('receiver-status', help='Inspect the background receiver without starting it')
    sub.add_parser('receiver-stop', help='Stop the background receiver without deleting original imports')
    args = parser.parse_args(argv)
    cat = PhotoCatalog()
    ai = Siglip2Encoder()
    try:
        if args.cmd == "status":
            data = cat.stats(ai.model_id) | {"ai_installed": ai.installed}
        elif args.cmd == 'receiver-start':
            data = ReceiverController(cat.home).start(port=args.port, inbox=args.inbox)
        elif args.cmd == 'receiver-status':
            data = ReceiverController(cat.home).status()
        elif args.cmd == 'receiver-stop':
            data = ReceiverController(cat.home).stop()
        elif args.cmd == "add-folder":
            data = cat.add_folder(args.path)
        elif args.cmd == "folders":
            data = {"folders": cat.folders()}
        elif args.cmd == "remove-folder":
            data = cat.remove_folder(args.path)
        elif args.cmd == "scan":
            data = cat.scan(encoder=ai if ai.installed else None)
        elif args.cmd == "install-ai":
            ai.install()
            data = {"installed": True}
        elif args.cmd == "search":
            data = cat.search(args.query, args.limit, encoder=ai if ai.installed else None)
        elif args.cmd == "browse":
            data = {"photos": cat.browse(args.limit)}
        elif args.cmd == "organize":
            data = virtual_albums(cat)
        elif args.cmd == "duplicates":
            data = find_duplicates(cat, similar_distance=args.distance)
        elif args.cmd == "backup-local":
            data = backup_local(cat, args.target, apply=args.apply)
        elif args.cmd == 'restore-local':
            data = restore_local(args.backup, args.target, apply=args.apply, snapshot=args.snapshot)
        elif args.cmd == 'backup-history':
            data = backup_history(args.backup)
        elif args.cmd == "backup-s3":
            data = backup_s3(cat, bucket=args.bucket, endpoint_url=args.endpoint_url,
                             prefix=args.prefix, apply=args.apply)
        elif args.cmd == "serve-iphone":
            from pathlib import Path
            server = MobileServer(cat, inbox=Path(args.inbox) if args.inbox else None, port=args.port)
            server.start()
            print(json.dumps({"host": "127.0.0.1", "port": server.port,
                              "pairing_token": server.token,
                              "tailnet_command": f"tailscale serve --bg {server.port}"}, indent=2), flush=True)
            try:
                server.thread.join()
            except KeyboardInterrupt:
                server.close()
            return 0
        else:
            parser.error("Invalid command")
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 2 if data.get('complete') is False else 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
