"""Data-safety and protocol regressions found during the first full review."""
from __future__ import annotations

import hashlib
import errno
import io
import json
import os
import sys
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from ai_photos.backup import backup_local, restore_local
from ai_photos.catalog import PhotoCatalog
from ai_photos.duplicates import find_duplicates, sha256_file
from ai_photos.file_safety import SafeDirectory
from ai_photos.sync import SyncFailure, SyncManager


def link_directory(link, target):
    if os.name == 'nt':
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path " + quote(link)
            + ' -Target ' + quote(target) + ' | Out-Null'], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def catalog_with_photo(base):
    source = base / 'photos'
    source.mkdir()
    Image.new('RGB', (12, 12), 'blue').save(source / 'original.png')
    cat = PhotoCatalog(base / 'state')
    cat.add_folder(str(source))
    cat.scan()
    return cat, source


def test_backup_junction_never_overwrites_originals(tmp_path):
    cat, source = catalog_with_photo(tmp_path)
    other = tmp_path / 'other'
    other.mkdir()
    Image.new('RGB', (12, 12), 'red').save(other / 'original.png')
    cat.add_folder(str(other))
    cat.scan()
    before = (sha256_file(source / 'original.png'), (source / 'original.png').stat().st_mtime_ns)
    target = tmp_path / 'backup'
    target.mkdir()
    root_id = hashlib.sha256(str(other.resolve()).encode()).hexdigest()[:16]
    link_directory(target / root_id, source)
    for apply in [False, True]:
        with pytest.raises((ValueError, OSError)):
            backup_local(cat, target, apply=apply)
    assert before == (sha256_file(source / 'original.png'), (source / 'original.png').stat().st_mtime_ns)


def test_writer_pins_parent_against_path_swap(tmp_path):
    parent = tmp_path / 'output'
    source = tmp_path / 'source'
    source.write_bytes(b'original')
    with SafeDirectory(parent) as directory:
        if os.name == 'nt':
            with pytest.raises(OSError):
                parent.rename(tmp_path / 'moved')
        else:
            parent.rename(tmp_path / 'moved')
            parent.mkdir()
        directory.copy(source, 'copy', replace=False)
    assert source.read_bytes() == b'original'
    actual = parent if os.name == 'nt' else tmp_path / 'moved'
    assert (actual / 'copy').read_bytes() == b'original'


@pytest.mark.skipif(os.name != 'nt', reason='NTFS reparse control is Windows-specific')
def test_directory_lease_prevents_in_place_junction_attachment(tmp_path):
    import ctypes
    import struct
    from ctypes import wintypes
    from ai_photos.file_safety import _kernel
    outside = tmp_path / 'outside'
    outside.mkdir()
    substitute = ('\\??\\' + str(outside)).encode('utf-16-le')
    printable = str(outside).encode('utf-16-le')
    paths = substitute + b'\0\0' + printable + b'\0\0'
    raw = struct.pack('<IHHHHHH', 0xA0000003, 8 + len(paths), 0, 0, len(substitute),
                      len(substitute) + 2, len(printable)) + paths
    _kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    def attach(path):
        handle = _kernel.CreateFileW(str(path), 0x40000000, 7, None, 3, 0x02200000, None)
        assert handle != ctypes.c_void_p(-1).value
        try:
            data = ctypes.create_string_buffer(raw)
            returned = wintypes.DWORD()
            succeeded = _kernel.DeviceIoControl(handle, 0x900A4, data, len(raw), None, 0, ctypes.byref(returned), None)
            return bool(succeeded), ctypes.get_last_error()
        finally:
            _kernel.CloseHandle(handle)
    control = tmp_path / 'unprotected'
    control.mkdir()
    assert attach(control)[0], 'The test must demonstrate a real working junction operation'
    protected = tmp_path / 'protected'
    with SafeDirectory(protected):
        succeeded, error = attach(protected)
        assert not succeeded and error == 145  # ERROR_DIR_NOT_EMPTY


def test_scan_does_not_follow_windows_junction(tmp_path):
    cat, source = catalog_with_photo(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    Image.new('RGB', (12, 12), 'red').save(outside / 'private.png')
    link_directory(source / 'redirect', outside)
    assert cat.scan()['seen'] == 1


def transfer(manager, device, data, name, kind, asset):
    body = {'asset_id': asset, 'resource_type': kind, 'filename': name,
            'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
            'asset_created_at': '2020-05-03T01:02:03Z', 'resource_key': asset + ':' + kind}
    state = manager.begin(device, body)
    manager.write_chunk(device, state['transfer_id'], 0, data, body['sha256'])
    manager.complete(device, state['transfer_id'])
    return state['transfer_id'], body


def manager_at(base):
    cat = PhotoCatalog(base / 'state')
    manager = SyncManager(cat, base / 'imports')
    device = 'test-device-12345'
    manager.pair(device, 'Test')
    return cat, manager, device


def test_completed_corruption_and_missing_files_are_not_acknowledged(tmp_path):
    cat, manager, device = manager_at(tmp_path)
    tid, body = transfer(manager, device, b'original-bytes', 'IMG.HEIC', 'photo', 'a')
    saved = next(manager.inbox.rglob('*.heic'))
    saved.write_bytes(b'corrupt!-bytes')
    assert manager.verify(device, body)['complete'] is False
    retry = manager.begin(device, body)
    assert retry['status'] == 'pending' and retry['offset'] == 0
    data = b'original-bytes'
    manager.write_chunk(device, retry['transfer_id'], 0, data, body['sha256'])
    manager.complete(device, retry['transfer_id'])
    assert saved.read_bytes() == b'corrupt!-bytes'  # Retain the old, possibly user-edited copy.
    assert manager.verify(device, body)['complete'] is True
    # Missing final files on a different completed resource must fail complete too.
    tid, _ = transfer(manager, device, b'other-original', 'IMG.MOV', 'video', 'b')
    next(manager.inbox.rglob('*.mov')).unlink()
    with pytest.raises(SyncFailure) as failure:
        manager.complete(device, tid)
    assert failure.value.status == 409


def test_all_resources_and_live_relationships_survive_backup_restore(tmp_path):
    cat, manager, device = manager_at(tmp_path)
    transfer(manager, device, b'heic-bytes', 'IMG.HEIC', 'photo', 'live')
    transfer(manager, device, b'paired-mov', 'IMG.MOV', 'pairedVideo', 'live')
    transfer(manager, device, b'raw-dng', 'RAW.DNG', 'alternatePhoto', 'raw')
    backup = tmp_path / 'backup'
    result = backup_local(cat, backup, apply=True)
    assert result['complete'] and result['copied'] == 3
    manifest = json.loads((backup / 'backup-manifest.json').read_text(encoding='utf-8'))
    resources = [r for entry in manifest['files'] for r in entry['resources']]
    assert {r['resource_type'] for r in resources} == {'photo', 'pairedVideo', 'alternatePhoto'}
    assert len([r for r in resources if r['asset_id'] == 'live']) == 2
    assert all(r['asset_created_at'] == '2020-05-03T01:02:03Z' for r in resources)
    restored = tmp_path / 'restored'
    assert restore_local(backup, restored, apply=True)['files'] == 3
    for entry in manifest['files']:
        assert sha256_file(restored / entry['backup_path']) == entry['sha256']


def test_stale_and_offline_sources_are_explicitly_incomplete(tmp_path):
    cat, source = catalog_with_photo(tmp_path)
    Image.new('RGB', (22, 22), 'red').save(source / 'original.png')
    result = backup_local(cat, tmp_path / 'backup', apply=True)
    assert not result['complete'] and result['issues'] and result['copied'] == 0
    assert not (tmp_path / 'backup').exists()


def test_same_content_different_names_uses_one_stored_resource(tmp_path):
    _, manager, device = manager_at(tmp_path)
    transfer(manager, device, b'identical', 'A.HEIC', 'photo', 'a')
    transfer(manager, device, b'identical', 'B.HEIC', 'photo', 'b')
    assert len(list(manager.inbox.rglob('*.heic'))) == 1
    assert manager.dashboard()['totals']['completed'] == 2


def test_cancel_does_not_delete_completed_originals(tmp_path):
    _, manager, device = manager_at(tmp_path)
    tid, _ = transfer(manager, device, b'safe', 'A.DNG', 'alternatePhoto', 'a')
    with pytest.raises(SyncFailure):
        manager.cancel(device, tid)
    assert next(manager.inbox.rglob('*.dng')).read_bytes() == b'safe'
    pending = manager.begin(device, {'asset_id': 'pending', 'resource_type': 'video', 'filename': 'B.MOV',
                                    'size': 4, 'sha256': hashlib.sha256(b'next').hexdigest()})
    manager.write_chunk(device, pending['transfer_id'], 0, b'ne', hashlib.sha256(b'ne').hexdigest())
    assert manager.cancel(device, pending['transfer_id'])['cancelled']
    assert not (manager.staging / (pending['transfer_id'] + '.part')).exists()
    assert next(manager.inbox.rglob('*.dng')).read_bytes() == b'safe'


def test_revocation_is_checked_again_after_request_authentication(tmp_path):
    _, manager, device = manager_at(tmp_path)
    data = b'revoked'
    body = {'asset_id': 'a', 'resource_type': 'photo', 'filename': 'A.HEIC',
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    pending = manager.begin(device, body)
    manager.revoke_device(device)
    with pytest.raises(SyncFailure) as failure:
        manager.write_chunk(device, pending['transfer_id'], 0, data, body['sha256'])
    assert failure.value.status == 401
    assert (manager.staging / (pending['transfer_id'] + '.part')).stat().st_size == 0


def test_near_hashes_differing_in_every_old_band_are_found(tmp_path):
    cat, source = catalog_with_photo(tmp_path)
    Image.new('RGB', (12, 12), 'red').save(source / 'other.png')
    cat.scan()
    def phash(path):
        return 0 if path.name == 'original.png' else sum(1 << (16 * i) for i in range(4))
    with patch('ai_photos.duplicates.dhash_file', side_effect=phash):
        assert len(find_duplicates(cat, 4)['similar_groups']) == 1


def test_failed_thumbnail_is_retried_on_explicit_scan(tmp_path):
    cat, source = catalog_with_photo(tmp_path)
    (source / 'raw.HEIC').write_bytes(b'fixture')
    with patch.object(cat, '_thumb', return_value=('', 0, 0, 'missing decoder')):
        cat.scan()
    with patch.object(cat, '_thumb', return_value=('decoded.jpg', 8, 8, None)) as decoder:
        cat.scan()
        assert decoder.call_count == 1
    assert next(p for p in cat.browse() if p['name'] == 'raw.HEIC')['error'] is None


def test_inference_does_not_hold_a_sqlite_writer_lock(tmp_path):
    cat, _ = catalog_with_photo(tmp_path)
    entered, release = threading.Event(), threading.Event()
    class Encoder:
        model_id = 'blocking-test'
        def embed_images(self, paths):
            entered.set()
            assert release.wait(10)
            return [np.array([1, 0], dtype=np.float32) for _ in paths]
    task = threading.Thread(target=lambda: cat.scan(Encoder()))
    task.start()
    try:
        assert entered.wait(5)
        with cat.connection() as db:
            db.execute('INSERT INTO folders(path,added_at) VALUES(?,?)', (str(tmp_path / 'other'), 'test'))
    finally:
        release.set()
        task.join(10)
    assert not task.is_alive()


def test_finalize_does_not_require_a_cross_directory_rename(tmp_path):
    _, manager, device = manager_at(tmp_path)
    rename, replace = os.rename, os.replace
    def same_directory_only(operation):
        def checked(source, target, *args, **kwargs):
            if Path(source).parent != Path(target).parent:
                raise OSError(errno.EXDEV, 'Simulated cross-volume rename')
            return operation(source, target, *args, **kwargs)
        return checked
    with patch('os.rename', side_effect=same_directory_only(rename)), patch('os.replace', side_effect=same_directory_only(replace)):
        transfer(manager, device, b'video', 'IMG.MOV', 'video', 'a')
    assert next(manager.inbox.rglob('*.mov')).read_bytes() == b'video'


def test_receiver_identity_is_durable_but_not_shared_by_new_databases(tmp_path):
    cat, manager, _ = manager_at(tmp_path)
    assert SyncManager(cat, manager.inbox).receiver_id == manager.receiver_id
    fresh = PhotoCatalog(tmp_path / 'fresh-db')
    assert SyncManager(fresh, tmp_path / 'other-imports').receiver_id != manager.receiver_id


def test_native_capture_time_and_original_display_name_are_preserved(tmp_path):
    cat, manager, device = manager_at(tmp_path)
    image = io.BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(image, 'PNG')
    transfer(manager, device, image.getvalue(), 'IPHONE_ORIGINAL.PNG', 'photo', 'a')
    row = cat.browse()[0]
    assert row['taken_at'] == '2020-05-03T01:02:03Z'
    assert row['name'] == 'IPHONE_ORIGINAL.PNG'


def test_s3_backups_include_all_resources_manifest_and_checksums(tmp_path, monkeypatch):
    from ai_photos.backup import backup_s3
    cat, manager, device = manager_at(tmp_path)
    transfer(manager, device, b'raw', 'RAW.DNG', 'alternatePhoto', 'a')
    transfer(manager, device, b'video', 'IMG.MOV', 'video', 'b')
    uploads, objects = [], []
    fake = SimpleNamespace(upload_file=lambda *a, **k: uploads.append((a, k)),
                           put_object=lambda **k: objects.append(k))
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(client=lambda *a, **k: fake))
    result = backup_s3(cat, bucket='test-never-contacted', apply=True)
    assert result['complete'] and result['uploaded'] == 2
    assert all(k['ExtraArgs']['ChecksumAlgorithm'] == 'SHA256' for _, k in uploads)
    manifest = json.loads(objects[0]['Body'])
    assert manifest['complete'] and len(manifest['files']) == 2


def test_sqlite_busy_returns_json_503_instead_of_disconnect(tmp_path):
    import http.client
    from ai_photos.mobile import MobileServer
    cat = PhotoCatalog(tmp_path / 'state')
    server = MobileServer(cat, inbox=tmp_path / 'imports', port=0)
    token = server.sync_manager.pair('test-device-12345', 'Test')['device_token']
    server.start()
    try:
        with patch.object(server.sync_manager, 'verify', side_effect=sqlite3.OperationalError('database is locked')):
            connection = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
            connection.request('POST', '/api/sync/v1/verify', '{}', {'Authorization': 'Bearer ' + token})
            response = connection.getresponse()
            assert response.status == 503 and json.loads(response.read())['retryable']
            connection.close()
    finally:
        server.close()


def test_slow_semantic_search_does_not_block_worker_control_messages(tmp_path):
    from ai_photos.worker import Worker
    cat, _ = catalog_with_photo(tmp_path)
    sink = io.StringIO()
    entered, release, pong = threading.Event(), threading.Event(), threading.Event()
    worker = Worker(cat, SimpleNamespace(installed=True, model_id='fake'), sink)
    def slow_search(*args):
        entered.set()
        assert release.wait(10)
        return {'mode': 'semantic', 'photos': []}
    original_send = worker.send
    def send(message):
        original_send(message)
        if message.get('id') == 2:
            pong.set()
    worker.send = send
    with patch.object(cat, 'search', side_effect=slow_search):
        reader = threading.Thread(target=worker.serve, args=([
            '{"id":1,"action":"search","params":{"query":"test"}}', '{"id":2,"action":"ping"}'],))
        reader.start()
        try:
            assert entered.wait(5)
            assert pong.wait(2), 'ping must not wait for model inference'
        finally:
            release.set()
            reader.join(5)


def test_unverified_or_unloadable_model_is_not_reported_ready(tmp_path, monkeypatch):
    from ai_photos.config import MODEL_ID, MODEL_REVISION
    from ai_photos.semantic import Siglip2Encoder
    monkeypatch.setenv('AI_PHOTOS_HOME', str(tmp_path / 'state'))
    from ai_photos.config import model_path
    folder = model_path()
    folder.mkdir(parents=True)
    (folder / 'config.json').write_text('{}')
    (folder / 'model.safetensors').write_bytes(b'not-real-weights')
    encoder = Siglip2Encoder()
    assert not encoder.installed
    manifest = {'repo': MODEL_ID, 'revision': MODEL_REVISION,
                'files': {name: sha256_file(folder / name) for name in ['config.json', 'model.safetensors']}}
    (folder / 'model-lock.json').write_text(json.dumps(manifest))
    assert encoder.installed  # Artifacts match; runtime has not loaded yet.
    def fail(*args, **kwargs):
        raise RuntimeError('invalid model configuration')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        AutoProcessor=SimpleNamespace(from_pretrained=fail), Siglip2Model=object))
    with pytest.raises(RuntimeError, match='invalid model'):
        encoder._ensure_loaded()
    assert encoder.last_error and not encoder.installed
