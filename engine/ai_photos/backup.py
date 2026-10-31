"""Explicit, verified resource backups. Original files are never written."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

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


def _relative_path(value):
    # Reject both Windows and POSIX traversal spellings on either OS.
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or value.startswith('/'):
        raise ValueError('Unsafe manifest path')
    parts = value.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)),
                *('LPT' + str(i) for i in range(1, 10))}
    if any(p in {'', '.', '..'} or p.endswith((' ', '.')) or
           any(ord(c) < 32 or c in '<>"|?*' for c in p) or p.split('.')[0].upper() in reserved for p in parts):
        raise ValueError('Unsafe manifest path')
    return Path(*parts)


def _snapshot_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9a-f]{32}|legacy-[0-9a-f]{64})', value):
        raise ValueError('Invalid snapshot ID')
    return value


def _validate_manifest(manifest, *, require_complete=True):
    if not isinstance(manifest, dict) or type(manifest.get('version')) is not int or manifest['version'] not in {1, 2}:
        raise ValueError('A supported backup manifest is required')
    if type(manifest.get('complete')) is not bool or (require_complete and not manifest['complete']):
        raise ValueError('A complete backup manifest is required')
    if not isinstance(manifest.get('files'), list):
        raise ValueError('Invalid manifest file inventory')
    if manifest['version'] == 2:
        _snapshot_id(manifest.get('snapshot_id'))
        if not isinstance(manifest.get('created_at'), str) or datetime.fromisoformat(manifest['created_at']).tzinfo is None:
            raise ValueError('Invalid snapshot timestamp')
    restore_paths = set()
    for record in manifest['files']:
        if not isinstance(record, dict) or not isinstance(record.get('folder'), str) or not Path(record['folder']).is_absolute():
            raise ValueError('Invalid original resource root')
        _relative_path(record.get('backup_path'))
        restored = _relative_path(record.get('restore_path') if manifest['version'] == 2 else record['backup_path'])
        # Windows is case-insensitive even when validation runs on another OS.
        key = restored.as_posix().casefold()
        if key in restore_paths:
            raise ValueError('Duplicate restore path in manifest')
        restore_paths.add(key)
        if not isinstance(record.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', record['sha256']):
            raise ValueError('Invalid resource checksum')
        if type(record.get('size')) is not int or record['size'] < 0:
            raise ValueError('Invalid resource size')
    return manifest


def _immutable_manifest(directory, name, payload):
    try:
        directory.write(name, payload, replace=False)
    except FileExistsError:
        with directory.open_read(name) as stream:
            if stream.read() != payload:
                raise ValueError('Existing immutable snapshot differs; refusing overwrite')


def _preserve_previous(target, previous):
    if previous is None:
        return
    payload, manifest = previous
    if not manifest['complete']:
        # Retain an old failed v1 attempt for inspection, never advertise it as
        # a restorable snapshot. The new run still cannot publish on failure.
        folder, name = target / '.previous-manifests', hashlib.sha256(payload).hexdigest()
    elif manifest['version'] == 1:
        folder, name = target / 'snapshots', 'legacy-' + hashlib.sha256(payload).hexdigest()
    else:
        folder, name = target / 'snapshots', manifest['snapshot_id']
    with SafeDirectory(folder) as directory:
        _immutable_manifest(directory, name + '.json', payload)


def _previous_manifest(target):
    try:
        with open_source(target / 'backup-manifest.json') as stream:
            payload = stream.read()
    except FileNotFoundError:
        return None
    return payload, _validate_manifest(json.loads(payload), require_complete=False)


def backup_local(catalog: PhotoCatalog, target: Path, *, apply=False) -> dict:
    roots, records, issues = _inventory(catalog)
    if not roots and not records and not issues:
        raise ValueError('No source folders or completed resources registered')
    target = _check_target(catalog, target, roots, records)
    copied = unchanged = total = 0
    saved = []
    snapshot_id = None
    latest_published = False
    # Fail closed before any writes if the indexed library cannot be accounted for.
    if not issues:
        try:
            previous = _previous_manifest(target)
            with SafeDirectory(target) if apply else nullcontext() as root_directory:
                if apply:
                    _preserve_previous(target, previous)
                for record in records:
                    source = Path(record['path'])
                    try:
                        digest = sha256_file(source)
                        current = source.stat()
                        if (current.st_size, current.st_mtime_ns) != (record['size'], record['mtime_ns']):
                            raise OSError('Source changed after backup planning; rescan before retrying')
                        expected = {r['sha256'] for r in record['resources']}
                        if expected and expected != {digest}:
                            raise OSError('Completed resource checksum no longer matches')
                        relative = Path('objects') / digest[:2] / (digest + source.suffix.lower())
                        check_path(target / relative)
                        restore_path = Path(record['root_id']) / record['relative']
                        record = dict(record, sha256=digest, backup_path=relative.as_posix(),
                                      restore_path=restore_path.as_posix())
                        if not apply:
                            total += record['size']
                            continue
                        with SafeDirectory(target / relative.parent) as directory:
                            try:
                                existing = directory.digest(relative.name)
                            except FileNotFoundError:
                                existing = None
                            if existing == digest:
                                unchanged += 1
                            elif existing is not None:
                                raise OSError('Immutable backup object is corrupt; retained without replacement')
                            else:
                                directory.copy(source, relative.name, expected=digest, replace=False)
                                copied += 1
                                total += record['size']
                        saved.append(record)
                    except (OSError, ValueError) as exc:
                        issues.append({'path': str(source), 'error': str(exc)})
                if apply and not issues:
                    # Recheck every referenced object before publishing metadata.
                    # Failed/interrupted runs leave only unreferenced objects;
                    # they never replace bytes or the last successful manifest.
                    for record in saved:
                        path = target / record['backup_path']
                        with SafeDirectory(path.parent) as directory:
                            if directory.digest(path.name) != record['sha256']:
                                raise OSError('Backup object failed final verification')
                    candidate = uuid4().hex
                    manifest = dict(_manifest(saved, []), version=2, snapshot_id=candidate,
                                    created_at=datetime.now(timezone.utc).isoformat())
                    _validate_manifest(manifest)
                    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
                    with SafeDirectory(target / 'snapshots') as directory:
                        _immutable_manifest(directory, candidate + '.json', payload)
                    snapshot_id = candidate
                    try:
                        root_directory.write('backup-manifest.json', payload)
                        latest_published = True
                    except OSError:
                        # A filesystem can report a durability error after the
                        # atomic rename. Report whether the complete pointer is
                        # visible; do not roll it back or touch earlier history.
                        try:
                            with root_directory.open_read('backup-manifest.json') as stream:
                                latest_published = stream.read() == payload
                        except OSError:
                            pass
                        raise
        except (OSError, ValueError) as exc:
            issues.append({'path': str(target), 'error': str(exc)})
    return {'mode': 'local', 'dry_run': not apply, 'complete': not issues, 'issues': issues,
            'files': len(records), 'copied': copied, 'unchanged': unchanged, 'bytes_planned': total,
            'target': str(target), 'snapshot_id': snapshot_id, 'latest_published': latest_published,
            'note': 'Immutable snapshots retained; originals and previous backup versions are never replaced or deleted'}


def _s3_missing(exc):
    # Only a confirmed absence counts as missing; AccessDenied and other
    # errors propagate so they can never be mistaken for "safe to write".
    code = str((getattr(exc, 'response', None) or {}).get('Error', {}).get('Code', ''))
    return code in {'404', 'NoSuchKey', 'NotFound'}


def _s3_previous(client, bucket, key):
    """Content keys referenced by the last published complete manifest."""
    try:
        payload = client.get_object(Bucket=bucket, Key=key)['Body'].read()
    except Exception as exc:
        if _s3_missing(exc):
            return {}
        raise
    manifest = _validate_manifest(json.loads(payload))
    if manifest['version'] != 2:
        return {}
    return {r['sha256']: r['backup_path'] for r in manifest['files']}


def _s3_verified(client, bucket, key, digest, size):
    try:
        head = client.head_object(Bucket=bucket, Key=key, ChecksumMode='ENABLED')
    except Exception as exc:
        if _s3_missing(exc):
            return False
        raise
    if head.get('ContentLength') != size or (head.get('Metadata') or {}).get('sha256') != digest:
        return False
    provider = head.get('ChecksumSHA256')
    if provider and '-' not in provider:  # Full-object checksum computed by the provider.
        return provider == base64.b64encode(bytes.fromhex(digest)).decode()
    # Composite (multipart) or unsupported checksum: the key was written once by
    # this tool after a stable-source upload; size and recorded digest must match.
    return True


def backup_s3(catalog: PhotoCatalog, *, bucket: str, endpoint_url=None,
              prefix='windows-ai-photos', apply=False) -> dict:
    if not bucket or not bucket.strip():
        raise ValueError('Bucket is required')
    if endpoint_url and urlparse(endpoint_url).scheme != 'https':
        raise ValueError('Cloud endpoint must use HTTPS')
    if any(p in {'.', '..'} for p in prefix.split('/')):
        raise ValueError('Unsafe cloud backup prefix')
    _, records, issues = _inventory(catalog)
    uploaded = unchanged = 0
    saved = []
    client = None
    snapshot_id = None
    latest_published = False
    base = prefix.strip('/')
    key_for = lambda *parts: '/'.join(p for p in (base, *parts) if p)
    if apply and not issues:
        import boto3
        client = boto3.client('s3', endpoint_url=endpoint_url)
        try:
            from boto3.s3.transfer import TransferConfig
            # Single-part uploads up to 4 GiB let the provider keep a
            # full-object SHA-256 that is checked against the local digest.
            upload_args = {'Config': TransferConfig(multipart_threshold=4 * 1024 ** 3)}
        except ImportError:
            upload_args = {}
        try:
            known = _s3_previous(client, bucket, key_for('backup-manifest.json'))
        except Exception as exc:  # Unreadable history: fail closed, write nothing.
            issues.append({'path': key_for('backup-manifest.json'), 'error': str(exc)})
            client = None
    for record in records if client else []:
        source = Path(record['path'])
        try:
            digest = sha256_file(source)
            if record['resources'] and {r['sha256'] for r in record['resources']} != {digest}:
                raise ValueError('Resource checksum mismatch')
            key = known.get(digest)
            if key and _s3_verified(client, bucket, key, digest, record['size']):
                unchanged += 1
            else:
                # Every upload gets a fresh key, so no outcome of this run
                # (denied HEAD, changing source, partial write) can replace
                # bytes that an earlier snapshot references.
                key = key_for('objects', digest[:2], digest, uuid4().hex + source.suffix.lower())
                before = source.stat()
                client.upload_file(str(source), bucket, key, ExtraArgs={
                    'Metadata': {'sha256': digest}, 'ChecksumAlgorithm': 'SHA256'}, **upload_args)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or sha256_file(source) != digest:
                    raise OSError('Source changed during upload; retry required')
                if not _s3_verified(client, bucket, key, digest, record['size']):
                    raise OSError('Uploaded object failed size/checksum verification')
                known[digest] = key
                uploaded += 1
            saved.append(dict(record, sha256=digest, backup_path=key,
                              restore_path=(Path(record['root_id']) / record['relative']).as_posix()))
        except Exception as exc:
            issues.append({'path': str(source), 'error': str(exc)})
    if client and not issues:
        # Publish only complete snapshots; a failed run never replaces the
        # last restorable cloud manifest. The snapshot precedes the pointer.
        snapshot_id = uuid4().hex
        manifest = dict(_manifest(saved, []), version=2, snapshot_id=snapshot_id,
                        created_at=datetime.now(timezone.utc).isoformat())
        body = json.dumps(manifest, ensure_ascii=False).encode()
        try:
            for key in (key_for('snapshots', snapshot_id + '.json'), key_for('backup-manifest.json')):
                client.put_object(Bucket=bucket, Key=key, Body=body,
                                  ContentType='application/json', ChecksumAlgorithm='SHA256')
            latest_published = True
        except Exception as exc:
            issues.append({'path': key_for('backup-manifest.json'), 'error': str(exc)})
    return {'mode': 's3', 'dry_run': not apply, 'complete': not issues, 'issues': issues,
            'bucket': bucket, 'objects': len(records), 'uploaded': uploaded, 'unchanged': unchanged,
            'snapshot_id': snapshot_id, 'latest_published': latest_published,
            'bytes_planned': sum(r['size'] for r in records),
            'note': 'TLS transport; no client-side encryption. Objects use unique keys and manifests '
                    'are published only when complete; still enable bucket versioning.'}


def _load_snapshot(backup, snapshot=None):
    name = _snapshot_id(snapshot) if snapshot is not None else None
    path = backup / 'snapshots' / (name + '.json') if name else backup / 'backup-manifest.json'
    try:
        with open_source(path) as stream:
            payload = stream.read()
    except FileNotFoundError:
        # A legacy current backup is usable before its first v2 migration too.
        previous = _previous_manifest(backup) if name and name.startswith('legacy-') else None
        if previous is None or 'legacy-' + hashlib.sha256(previous[0]).hexdigest() != name:
            raise
        payload = previous[0]
    manifest = _validate_manifest(json.loads(payload))
    if name:
        actual = manifest.get('snapshot_id') if manifest['version'] == 2 else 'legacy-' + hashlib.sha256(payload).hexdigest()
        if name != actual:
            raise ValueError('Snapshot ID does not match manifest')
    return manifest


def backup_history(backup: Path) -> dict:
    """List immutable, complete manifests; object bytes are checked at restore."""
    backup = check_path(backup)
    if not backup.is_dir():
        raise ValueError('Backup directory not found')
    snapshots = {}
    issues = []
    try:
        previous = _previous_manifest(backup)
    except (OSError, ValueError) as exc:
        previous = None
        issues.append({'path': str(backup / 'backup-manifest.json'), 'error': str(exc)})
    latest = None
    if previous and previous[1]['complete']:
        latest = previous[1].get('snapshot_id') or 'legacy-' + hashlib.sha256(previous[0]).hexdigest()
        snapshots[latest] = previous[1]
    folder = check_path(backup / 'snapshots')
    if folder.exists():
        for path in sorted(folder.glob('*.json')):
            try:
                snapshots[path.stem] = _load_snapshot(backup, path.stem)
            except (OSError, ValueError) as exc:
                issues.append({'path': str(path), 'error': str(exc)})
    rows = [{'snapshot_id': ident, 'created_at': value.get('created_at'), 'files': len(value['files']),
             'latest': ident == latest, 'version': value['version']} for ident, value in snapshots.items()]
    rows.sort(key=lambda row: (row['created_at'] or '', row['snapshot_id']), reverse=True)
    return {'snapshots': rows, 'issues': issues, 'complete': not issues,
            'note': 'Listing validates manifests; restore verifies every referenced file checksum'}


def restore_local(backup: Path, target: Path, *, apply=False, snapshot=None) -> dict:
    backup = check_path(backup)
    target = check_path(target)
    if not backup.is_dir():
        raise ValueError('Backup directory not found')
    if target.exists() and any(target.iterdir()):
        raise ValueError('Choose an empty restore destination; existing files are never overwritten')
    manifest = _load_snapshot(backup, snapshot)
    if target == backup or backup in target.parents or target in backup.parents:
        raise ValueError('Restore destination must be independent of the backup')
    prepared = []
    for record in manifest['files']:
        source = check_path(backup / _relative_path(record['backup_path']))
        original_root = Path(record['folder']).resolve()
        if target == original_root or original_root in target.parents or target in original_root.parents:
            raise ValueError('Restore must not overlap original sources')
        if sha256_file(source) != record['sha256']:
            raise OSError('Backup checksum mismatch')
        if source.stat().st_size != record['size']:
            raise OSError('Backup size mismatch')
        relative = _relative_path(record['restore_path'] if manifest['version'] == 2 else record['backup_path'])
        out = check_path(target / relative)
        prepared.append((source, out, record['sha256']))
    # Validate the entire inventory before starting a restore; never hide a
    # missing/corrupt later resource behind a partially verified result.
    for source, out, digest in prepared:
        if apply:
            with SafeDirectory(out.parent) as directory:
                # Never overwrite an existing restore destination.
                directory.copy(source, out.name, expected=digest, replace=False)
    if apply:
        with SafeDirectory(target) as directory:
            directory.write('restored-resource-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    return {'mode': 'restore-local', 'dry_run': not apply, 'complete': True, 'files': len(prepared),
            'snapshot_id': manifest.get('snapshot_id', snapshot), 'target': str(target)}
