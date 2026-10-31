"""Regressions for the PR #3 review findings (receiver, sync finalization, backups, scan)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from ai_photos import sync as sync_module
from ai_photos.backup import backup_local, backup_s3, restore_local
from ai_photos.catalog import PhotoCatalog
from ai_photos.file_safety import SafeDirectory
from ai_photos.receiver_agent import ReceiverController
from ai_photos.sync import SyncFailure, SyncManager


def manager_at(base):
    cat = PhotoCatalog(base / 'state')
    manager = SyncManager(cat, base / 'imports')
    manager.pair('test-device-12345', 'Test')
    return cat, manager, 'test-device-12345'


def staged(manager, device, data=b'video-bytes', asset='asset-1'):
    body = {'asset_id': asset, 'resource_type': 'video', 'filename': 'IMG.MOV',
            'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    state = manager.begin(device, body)
    manager.write_chunk(device, state['transfer_id'], 0, data, body['sha256'])
    return state['transfer_id']


def test_failed_control_request_does_not_stop_receiver(tmp_path):
    controller = ReceiverController(tmp_path / 'state')
    try:
        started = controller.start(port=0, inbox=tmp_path / 'imports')
        # SyncFailure (unknown transfer) used to escape the control loop.
        with pytest.raises(RuntimeError, match='Transfer not found'):
            controller.manage(None, 'cancel', device_id='unknown-device', transfer_id='x' * 32)
        assert controller.status()['pid'] == started['pid']
    finally:
        controller.stop()


@pytest.mark.skipif(os.name == 'nt', reason='stale POSIX socket state')
def test_stop_discards_stale_state_after_unclean_exit(tmp_path):
    controller = ReceiverController(tmp_path / 'state')
    controller.directory.mkdir(parents=True)
    # A killed agent leaves its socket file behind; connecting is refused.
    import socket
    leftover = socket.socket(socket.AF_UNIX)
    leftover.bind(str(tmp_path / 'left.sock'))
    leftover.close()
    state = controller.directory / 'state.json'
    state.write_text(json.dumps({'instance': 'gone', 'pid': 1, 'port': 1,
        'address': str(tmp_path / 'left.sock'), 'control_key': base64.b64encode(b'k').decode()}))
    state.chmod(0o600)
    assert controller.status().get('error')
    assert controller.stop()['stopped']
    assert not state.exists()
    assert controller.status() == {'running': False}


def test_owner_can_discard_staging_of_revoked_device(tmp_path):
    _, manager, device = manager_at(tmp_path)
    data = b'x' * 16
    body = {'asset_id': 'a', 'resource_type': 'video', 'filename': 'IMG.MOV',
            'sha256': hashlib.sha256(b'x' * 32).hexdigest(), 'size': 32}
    tid = manager.begin(device, body)['transfer_id']
    manager.write_chunk(device, tid, 0, data, hashlib.sha256(data).hexdigest())
    manager.revoke_device(device)
    with pytest.raises(SyncFailure):
        manager.cancel(device, tid)  # A revoked device itself is still refused.
    assert manager.cancel(device, tid, owner=True) == {'cancelled': True}
    assert not (manager.staging / (tid + '.part')).exists()


def test_finalize_copy_runs_without_global_lock_and_blocks_cancel(tmp_path):
    _, manager, device = manager_at(tmp_path)
    tid = staged(manager, device)
    entered, release, seen = threading.Event(), threading.Event(), {}
    original = SafeDirectory.copy

    def slow_copy(self, *args, **kwargs):
        entered.set()
        release.wait(10)
        return original(self, *args, **kwargs)

    with patch.object(SafeDirectory, 'copy', slow_copy):
        worker = threading.Thread(target=lambda: seen.setdefault('result', manager.complete(device, tid)))
        worker.start()
        assert entered.wait(10)
        try:
            def probe():
                seen['lock'] = manager.lock.acquire(timeout=5)
                if seen['lock']:
                    manager.lock.release()

            other = threading.Thread(target=probe)
            other.start()
            other.join()
            assert seen['lock']
            for call in (lambda: manager.cancel(device, tid, owner=True), lambda: manager.complete(device, tid)):
                with pytest.raises(SyncFailure) as error:
                    call()
                assert error.value.status == 409
        finally:
            release.set()
            worker.join(10)
    assert seen['result']['status'] == 'complete'
    assert manager.status(device, tid)['status'] == 'complete'


def test_complete_refuses_before_copy_when_import_volume_is_full(tmp_path, monkeypatch):
    _, manager, device = manager_at(tmp_path)
    tid = staged(manager, device)
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(sync_module.shutil, 'disk_usage', lambda _: usage(1, 1, 10))
    with pytest.raises(SyncFailure) as error:
        manager.complete(device, tid)
    assert error.value.status == 507
    assert manager.status(device, tid)['status'] == 'pending'
    assert (manager.staging / (tid + '.part')).is_file()
    monkeypatch.undo()
    assert manager.complete(device, tid)['status'] == 'complete'


def s3_fake(objects, *, fail=()):
    def upload(path, bucket, key, ExtraArgs):
        if Path(path).name in fail:
            raise OSError('network')
        objects[key] = ExtraArgs['Metadata']

    def head(Bucket, Key):
        if Key not in objects:
            raise KeyError(Key)
        return {'Metadata': objects[Key]}

    puts = []
    return SimpleNamespace(upload_file=upload, head_object=head,
                           put_object=lambda **k: puts.append(k)), puts


def test_s3_failed_run_never_replaces_manifest_and_objects_are_immutable(tmp_path, monkeypatch):
    source = tmp_path / 'photos'
    source.mkdir()
    for name, color in [('A.png', 'blue'), ('B.png', 'green')]:
        Image.new('RGB', (12, 12), color).save(source / name)
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(source))
    cat.scan()
    objects = {}
    fake, puts = s3_fake(objects)
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(client=lambda *a, **k: fake))
    first = backup_s3(cat, bucket='b', apply=True)
    assert first['complete'] and first['uploaded'] == 2 and first['snapshot_id']
    assert all('/objects/' in key for key in objects)
    assert {p['Key'].rsplit('/', 1)[-1] for p in puts} == {'backup-manifest.json', first['snapshot_id'] + '.json'}
    manifest = json.loads(puts[-1]['Body'])
    assert manifest['complete'] and manifest['version'] == 2 and len(manifest['files']) == 2

    again = backup_s3(cat, bucket='b', apply=True)
    assert again['uploaded'] == 0 and again['unchanged'] == 2

    Image.new('RGB', (12, 12), 'red').save(source / 'A.png')
    cat.scan()
    failing, failed_puts = s3_fake(objects, fail={'A.png'})
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(client=lambda *a, **k: failing))
    result = backup_s3(cat, bucket='b', apply=True)
    assert not result['complete'] and result['issues'] and failed_puts == []


def test_backup_reuses_verified_legacy_layout_instead_of_copying(tmp_path):
    source = tmp_path / 'photos'
    source.mkdir()
    for name, color in [('A.png', 'blue'), ('B.png', 'green')]:
        Image.new('RGB', (12, 12), color).save(source / name)
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(source))
    cat.scan()
    target = tmp_path / 'backup'
    records = []
    for path in sorted(source.glob('*.png')):
        relative = 'a' * 16 + '/' + path.name
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        (target / relative).write_bytes(path.read_bytes())
        records.append({'folder': str(source.resolve()), 'backup_path': relative, 'resources': [],
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size})
    (target / 'backup-manifest.json').write_text(json.dumps(
        {'version': 1, 'complete': True, 'files': records, 'issues': []}))
    # A corrupt legacy file is never trusted.
    (target / ('a' * 16) / 'B.png').write_bytes(b'corrupt')

    result = backup_local(cat, target, apply=True)
    assert result['complete'] and result['copied'] == 1 and result['unchanged'] == 1
    assert len(list((target / 'objects').rglob('*.png'))) == 1
    restored = tmp_path / 'restored'
    assert restore_local(target, restored, apply=True)['complete']
    assert {p.name: p.read_bytes() for p in restored.rglob('*.png')} == {
        p.name: p.read_bytes() for p in source.glob('*.png')}


def test_unchanged_undecodable_file_is_not_reported_as_updated_again(tmp_path):
    source = tmp_path / 'photos'
    source.mkdir()
    (source / 'raw.HEIC').write_bytes(b'fixture')
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(source))
    with patch.object(cat, '_thumb', return_value=('', 0, 0, 'missing decoder')):
        first = cat.scan()
        second = cat.scan()
    assert first['updated'] == 1 and first['errors'] == 1
    assert second['updated'] == 0 and second['errors'] == 0
