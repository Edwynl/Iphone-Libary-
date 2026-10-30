"""Explicit, verified resource backups. Original files are never written."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from .catalog import PhotoCatalog
from .duplicates import sha256_file
from .file_safety import SafeDirectory, check_path, open_source


def _inventory(catalog):
    roots = [Path(f).resolve() for f in catalog.folders()]
    with catalog.connection() as db:
        photos = [dict(r) for r in db.execute("SELECT path,folder,size,mtime_ns FROM photos ORDER BY id")]
        has_sync = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_transfers'").fetchone()
        resources = [dict(r) for r in db.execute("SELECT * FROM sync_transfers WHERE status='complete'")] if has_sync else []
    records = {r['path']: dict(r, resources=[]) for r in photos}
    issues = []
    for resource in resources:
        path = resource['stored_path']
        if not path:
            issues.append({'path': None, 'transfer_id': resource['id'], 'error': 'Completed resource has no stored path'})
            continue
        root = next((r for r in roots if r in Path(path).parents), Path(path).parent)
        if path not in records:
            records[path] = {'path': path, 'folder': str(root), 'size': resource['size'], 'mtime_ns': None, 'resources': []}
        records[path]['resources'].append({k: resource.get(k) for k in (
            'device_id', 'asset_id', 'resource_type', 'filename', 'sha256',
            'asset_created_at', 'asset_modified_at', 'resource_key')})
    valid = []
    for record in records.values():
        path = Path(record['path'])
        try:
            check_path(path)
            info = path.stat()
            parent = Path(record['folder']).resolve()
            if parent not in path.resolve().parents or not path.is_file():
                raise ValueError('Source outside its registered resource root')
            if info.st_size != record['size'] or (record['mtime_ns'] is not None and info.st_mtime_ns != record['mtime_ns']):
                raise ValueError('Stale index; scan or reconcile the resource first')
            root_id = hashlib.sha256(str(parent).encode()).hexdigest()[:16]
            record.update(root_id=root_id, relative=path.resolve().relative_to(parent).as_posix(),
                          mtime_ns=info.st_mtime_ns, size=info.st_size)
            valid.append(record)
        except (OSError, ValueError) as exc:
            issues.append({'path': str(path), 'error': str(exc)})
    return roots, valid, issues


def _check_target(catalog, target, roots, records):
    target = check_path(target)
    protected = [*roots, catalog.home.resolve(), *[Path(r['folder']).resolve() for r in records]]
    for root in protected:
        if target == root or root in target.parents or target in root.parents:
            raise ValueError('Backup destination must not overlap photo sources or internal app data')
    for record in records:
        check_path(target / record['root_id'] / record['relative'])
    return target


def _manifest(records, issues):
    return {'version': 1, 'complete': not issues, 'issues': issues, 'files': records,
            'note': 'Contains original resource relationships and SHA-256; no source deletion.'}


def backup_local(catalog: PhotoCatalog, target: Path, *, apply=False) -> dict:
    roots, records, issues = _inventory(catalog)
    if not roots and not records and not issues:
        raise ValueError('No source folders or completed resources registered')
    target = _check_target(catalog, target, roots, records)
    copied = unchanged = total = 0
    saved = []
    # Fail closed before any writes if the indexed library cannot be accounted for.
    if not issues:
        for record in records:
            source = Path(record['path'])
            relative = Path(record['root_id']) / record['relative']
            out = target / relative
            try:
                digest = sha256_file(source)
                current = source.stat()
                if (current.st_size, current.st_mtime_ns) != (record['size'], record['mtime_ns']):
                    raise OSError('Source changed after backup planning; rescan before retrying')
                expected = {r['sha256'] for r in record['resources']}
                if expected and expected != {digest}:
                    raise OSError('Completed resource checksum no longer matches')
                record = dict(record, sha256=digest, backup_path=relative.as_posix())
                if not apply:
                    total += record['size']
                    continue
                with SafeDirectory(out.parent) as directory:
                    try:
                        existing = directory.digest(out.name)
                    except FileNotFoundError:
                        existing = None
                    if existing == digest:
                        unchanged += 1
                    else:
                        directory.copy(source, out.name, expected=digest)
                        copied += 1
                        total += record['size']
                saved.append(record)
            except (OSError, ValueError) as exc:
                issues.append({'path': str(source), 'error': str(exc)})
        if apply:
            with SafeDirectory(target) as directory:
                directory.write('backup-manifest.json', json.dumps(_manifest(saved, issues), ensure_ascii=False, indent=2).encode())
    return {'mode': 'local', 'dry_run': not apply, 'complete': not issues, 'issues': issues,
            'files': len(records), 'copied': copied, 'unchanged': unchanged, 'bytes_planned': total,
            'target': str(target), 'note': 'Originals were not removed or modified'}


def backup_s3(catalog: PhotoCatalog, *, bucket: str, endpoint_url=None,
              prefix='windows-ai-photos', apply=False) -> dict:
    if not bucket or not bucket.strip():
        raise ValueError('Bucket is required')
    if endpoint_url and urlparse(endpoint_url).scheme != 'https':
        raise ValueError('Cloud endpoint must use HTTPS')
    if any(p in {'.', '..'} for p in prefix.split('/')):
        raise ValueError('Unsafe cloud backup prefix')
    _, records, issues = _inventory(catalog)
    uploaded = 0
    manifest = []
    client = None
    if apply and not issues:
        import boto3
        client = boto3.client('s3', endpoint_url=endpoint_url)
    for record in records if not issues else []:
        key = '/'.join(p for p in (prefix.strip('/'), record['root_id'], record['relative']) if p)
        if apply:
            source = Path(record['path'])
            digest = sha256_file(source)
            if record['resources'] and {r['sha256'] for r in record['resources']} != {digest}:
                issues.append({'path': str(source), 'error': 'Resource checksum mismatch'})
                continue
            before = source.stat()
            client.upload_file(str(source), bucket, key, ExtraArgs={
                'Metadata': {'sha256': digest}, 'ChecksumAlgorithm': 'SHA256'})
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or sha256_file(source) != digest:
                issues.append({'path': str(source), 'error': 'Source changed during upload; retry required'})
                continue
            manifest.append(dict(record, sha256=digest, backup_path=key))
            uploaded += 1
    if client:
        client.put_object(Bucket=bucket, Key=prefix.strip('/') + '/backup-manifest.json',
                          Body=json.dumps(_manifest(manifest, issues), ensure_ascii=False).encode(),
                          ContentType='application/json', ChecksumAlgorithm='SHA256')
    return {'mode': 's3', 'dry_run': not apply, 'complete': not issues, 'issues': issues,
            'bucket': bucket, 'objects': len(records), 'uploaded': uploaded,
            'bytes_planned': sum(r['size'] for r in records),
            'note': 'TLS transport; no client-side encryption. Enable bucket versioning and test restores.'}


def restore_local(backup: Path, target: Path, *, apply=False) -> dict:
    backup = check_path(backup)
    target = check_path(target)
    if not backup.is_dir():
        raise ValueError('Backup directory not found')
    if target.exists() and any(target.iterdir()):
        raise ValueError('Choose an empty restore destination; existing files are never overwritten')
    with open_source(backup / 'backup-manifest.json') as stream:
        manifest = json.load(stream)
    if manifest.get('version') != 1 or not manifest.get('complete'):
        raise ValueError('A complete version 1 backup manifest is required')
    if target == backup or backup in target.parents or target in backup.parents:
        raise ValueError('Restore destination must be independent of the backup')
    count = 0
    for record in manifest['files']:
        relative = Path(record['backup_path'])
        if relative.is_absolute() or '..' in relative.parts or ':' in str(relative):
            raise ValueError('Unsafe manifest path')
        source = check_path(backup / relative)
        original_root = Path(record['folder']).resolve()
        if target == original_root or original_root in target.parents or target in original_root.parents:
            raise ValueError('Restore must not overlap original sources')
        if sha256_file(source) != record['sha256']:
            raise OSError('Backup checksum mismatch')
        out = check_path(target / relative)
        if apply:
            with SafeDirectory(out.parent) as directory:
                # Never overwrite an existing restore destination.
                directory.copy(source, out.name, expected=record['sha256'], replace=False)
        count += 1
    if apply:
        with SafeDirectory(target) as directory:
            directory.write('restored-resource-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    return {'mode': 'restore-local', 'dry_run': not apply, 'complete': True, 'files': count, 'target': str(target)}
