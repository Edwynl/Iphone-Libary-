"""Opt-in, non-destructive backup targets (local and S3-compatible cloud).

Never delete originals or cloud objects. Cloud backup is not client-side encrypted:
operators must choose a trusted account/bucket and manage credentials separately.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .catalog import PhotoCatalog
from .duplicates import sha256_file


def _sources(catalog: PhotoCatalog):
    roots = [Path(f).resolve(strict=False) for f in catalog.folders()]
    with catalog.connection() as db:
        rows = db.execute("SELECT path,folder,size,mtime_ns FROM photos ORDER BY id").fetchall()
    for row in rows:
        source = Path(row["path"])
        parent = Path(row["folder"]).resolve(strict=False)
        if parent not in roots:
            continue
        if source.is_symlink() or not source.is_file():
            continue
        try:
            real = source.resolve(strict=True)
            if parent not in real.parents:
                continue
            stat = source.stat()
            if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
                continue
        except OSError:
            continue
        root_id = hashlib.sha256(str(parent).encode("utf-8")).hexdigest()[:16]
        yield source, root_id, real.relative_to(parent)


def backup_local(catalog: PhotoCatalog, target: Path, *, apply: bool = False) -> dict:
    target = Path(target).expanduser().resolve(strict=False)
    roots = [Path(f).resolve(strict=False) for f in catalog.folders()]
    for root in roots:
        if target == root or root in target.parents or target in root.parents:
            raise ValueError("Backup destination must not overlap any photo source folder")
    if target == catalog.home.resolve() or catalog.home.resolve() in target.parents:
        raise ValueError("Backup destination must not be inside internal app data")
    if not roots:
        raise ValueError("No source folders registered")
    count = copied = skipped = bytes_total = 0
    for src, root_id, relative in _sources(catalog):
        count += 1
        out = target / root_id / relative
        if out.is_file() and out.stat().st_size == src.stat().st_size:
            # Equal size is only a prefilter. Verify bytes before skipping.
            if sha256_file(src) == sha256_file(out):
                skipped += 1
                continue
        bytes_total += src.stat().st_size
        if apply:
            out.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=out.parent, prefix=".backup-", delete=False) as f:
                tmp = Path(f.name)
            try:
                shutil.copy2(src, tmp)
                if sha256_file(src) != sha256_file(tmp):
                    raise IOError("Backup verification failed")
                os.replace(tmp, out)
                copied += 1
            finally:
                tmp.unlink(missing_ok=True)
    return {"mode": "local", "dry_run": not apply, "files": count,
            "copied": copied, "unchanged": skipped, "bytes_planned": bytes_total,
            "target": str(target), "note": "No originals removed or modified"}


def backup_s3(catalog: PhotoCatalog, *, bucket: str, endpoint_url: str | None = None,
              prefix: str = "windows-ai-photos", apply: bool = False) -> dict:
    """Opt-in S3-compatible object backup; credentials come from the AWS SDK chain.

    Requires `pip install boto3` ONLY when apply=True. For true disaster
    recovery, enable bucket versioning and use an independent second copy.
    """
    if not bucket or not bucket.strip():
        raise ValueError("Bucket is required")
    if endpoint_url and urlparse(endpoint_url).scheme != "https":
        raise ValueError("Cloud endpoint must use HTTPS")
    if any(part in {"..", "."} for part in prefix.split("/")):
        raise ValueError("Unsafe cloud backup prefix")
    client = None
    if apply:
        import boto3  # optional, user explicitly enables cloud backups
        client = boto3.client("s3", endpoint_url=endpoint_url)
    count = uploaded = total_bytes = 0
    for source, root_id, relative in _sources(catalog):
        count += 1
        total_bytes += source.stat().st_size
        if not apply:
            continue
        key = "/".join(x for x in (prefix.strip("/"), root_id, relative.as_posix()) if x)
        digest = sha256_file(source)
        # Server-side encryption varies between providers. Require the user to
        # configure default bucket encryption; no client-side E2EE is claimed.
        client.upload_file(str(source), bucket, key, ExtraArgs={"Metadata": {"sha256": digest}})
        uploaded += 1
    return {"mode": "s3", "dry_run": not apply, "bucket": bucket,
            "objects": count, "uploaded": uploaded, "bytes_planned": total_bytes,
            "note": "TLS transport only; client-side encryption not implemented. Enable bucket versioning."}
