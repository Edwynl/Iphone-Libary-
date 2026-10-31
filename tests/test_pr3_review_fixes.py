"""Regressions for the PR #3 review findings (receiver, sync finalization, backups, scan)."""
from __future__ import annotations

import base64
import hashlib
import io
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


class S3Error(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {'Error': {'Code': code}}


class FakeS3:
    """Stores object bodies so overwrites and corruption are observable."""

    def __init__(self, checksum='full'):
        self.bodies, self.metadata, self.puts = {}, {}, []
        self.head_error = None
        self.during_upload = None
        self.checksum = checksum  # 'full', None, 'composite' or 'malformed'

    def upload_file(self, path, bucket, key, ExtraArgs, Config=None):
        if self.during_upload:
            self.during_upload(Path(path))
        self.bodies[key] = Path(path).read_bytes()
        self.metadata[key] = ExtraArgs['Metadata']

    def head_object(self, Bucket, Key, ChecksumMode=None):
        if self.head_error:
            raise S3Error(self.head_error)
        if Key not in self.bodies:
            raise S3Error('404')
        body = self.bodies[Key]
        reply = {'ContentLength': len(body), 'Metadata': self.metadata[Key]}
        full = base64.b64encode(hashlib.sha256(body).digest()).decode()
        if self.checksum == 'full':
            reply['ChecksumSHA256'] = full
        elif self.checksum == 'composite':
            reply['ChecksumSHA256'] = full + '-3'
        elif self.checksum == 'malformed':
            reply['ChecksumSHA256'] = 'not-base64!'
        return reply

    def get_object(self, Bucket, Key):
        if Key not in self.bodies:
            raise S3Error('NoSuchKey')
        return {'Body': io.BytesIO(self.bodies[Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.puts.append(Key)
        self.bodies[Key] = Body


@pytest.fixture
def photo_library(tmp_path):
    source = tmp_path / 'photos'
    source.mkdir()
    for name, color in [('A.png', 'blue'), ('B.png', 'green')]:
        Image.new('RGB', (12, 12), color).save(source / name)
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(source))
    cat.scan()
    return cat, source


def run_s3(cat, fake, monkeypatch):
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(client=lambda *a, **k: fake))
    return backup_s3(cat, bucket='b', apply=True)


def object_snapshot(fake):
    return {k: v for k, v in fake.bodies.items() if '/objects/' in k}


def test_s3_publishes_complete_snapshots_and_reuses_verified_objects(photo_library, monkeypatch):
    cat, _ = photo_library
    fake = FakeS3()
    first = run_s3(cat, fake, monkeypatch)
    assert first['complete'] and first['uploaded'] == 2 and first['latest_published']
    assert fake.puts == [f"windows-ai-photos/snapshots/{first['snapshot_id']}.json",
                         'windows-ai-photos/backup-manifest.json']
    manifest = json.loads(fake.bodies['windows-ai-photos/backup-manifest.json'])
    assert manifest['complete'] and manifest['version'] == 2 and len(manifest['files']) == 2
    again = run_s3(cat, fake, monkeypatch)
    assert again['complete'] and again['uploaded'] == 0 and again['unchanged'] == 2


def test_s3_denied_head_never_overwrites_or_publishes(photo_library, monkeypatch):
    cat, _ = photo_library
    fake = FakeS3()
    run_s3(cat, fake, monkeypatch)
    before, puts = object_snapshot(fake), list(fake.puts)
    fake.head_error = 'AccessDenied'
    result = run_s3(cat, fake, monkeypatch)
    assert not result['complete'] and result['uploaded'] == 0
    assert object_snapshot(fake) == before and fake.puts == puts


def test_s3_source_change_during_upload_keeps_previous_snapshot_intact(photo_library, monkeypatch):
    cat, source = photo_library
    fake = FakeS3()
    run_s3(cat, fake, monkeypatch)
    before, pointer = object_snapshot(fake), fake.bodies['windows-ai-photos/backup-manifest.json']
    Image.new('RGB', (12, 12), 'red').save(source / 'A.png')
    cat.scan()
    fake.during_upload = lambda path: path.write_bytes(path.read_bytes() + b'tail')
    result = run_s3(cat, fake, monkeypatch)
    assert not result['complete'] and not result['latest_published']
    assert all(fake.bodies[k] == v for k, v in before.items())
    assert fake.bodies['windows-ai-photos/backup-manifest.json'] == pointer


@pytest.mark.parametrize('checksum', ['full', None, 'composite', 'malformed'])
def test_s3_corrupt_stored_object_is_uploaded_again_under_a_new_key(photo_library, monkeypatch, checksum):
    cat, _ = photo_library
    fake = FakeS3(checksum)
    run_s3(cat, fake, monkeypatch)
    corrupt = next(iter(object_snapshot(fake)))
    # Same length and untouched metadata: only the bytes themselves differ.
    fake.bodies[corrupt] = bytes(b ^ 0xFF for b in fake.bodies[corrupt])
    result = run_s3(cat, fake, monkeypatch)
    assert result['complete'] and result['uploaded'] == 1 and result['unchanged'] == 1
    manifest = json.loads(fake.bodies['windows-ai-photos/backup-manifest.json'])
    assert corrupt not in {r['backup_path'] for r in manifest['files']}


def test_migrated_backup_survives_old_client_rewriting_legacy_files(photo_library, tmp_path):
    cat, source = photo_library
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
    assert backup_local(cat, target, apply=True)['complete']
    for legacy in (target / ('a' * 16)).glob('*.png'):
        legacy.write_bytes(b'rewritten in place by another writer')
    restored = tmp_path / 'restored'
    assert restore_local(target, restored, apply=True)['complete']
    assert {p.name: p.read_bytes() for p in restored.rglob('*.png')} == {
        p.name: p.read_bytes() for p in source.glob('*.png')}


def test_undecodable_files_are_counted_as_errors_but_not_updates(tmp_path):
    source = tmp_path / 'photos'
    source.mkdir()
    (source / 'raw.HEIC').write_bytes(b'fixture')
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(source))
    with patch.object(cat, '_thumb', return_value=('', 0, 0, 'missing decoder')):
        first = cat.scan()
        second = cat.scan()
    with patch.object(cat, '_thumb', return_value=('decoded.jpg', 8, 8, None)):
        third = cat.scan()
    assert (first['updated'], first['errors']) == (1, 1)
    assert (second['updated'], second['errors']) == (0, 1)
    assert (third['updated'], third['errors']) == (1, 0)


def visible_imports(manager):
    return [p for p in manager.inbox.rglob('*') if p.is_file() and not p.name.endswith('.verified')]


@pytest.mark.parametrize('interrupt', ['revoke', 'pause'])
def test_revoke_or_pause_during_unlocked_copy_publishes_nothing(tmp_path, interrupt):
    cat, manager, device = manager_at(tmp_path)
    buffer = io.BytesIO()
    Image.new('RGB', (12, 12), 'purple').save(buffer, 'PNG')
    data = buffer.getvalue()
    body = {'asset_id': 'png-asset', 'resource_type': 'photo', 'filename': 'IMG.PNG',
            'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    tid = manager.begin(device, body)['transfer_id']
    manager.write_chunk(device, tid, 0, data, body['sha256'])
    manager.inbox.mkdir(parents=True, exist_ok=True)
    cat.add_folder(str(manager.inbox))
    original = SafeDirectory.copy

    def copy_then_interrupt(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        manager.revoke_device(device) if interrupt == 'revoke' else manager.set_paused(True)
        return result

    with patch.object(SafeDirectory, 'copy', copy_then_interrupt):
        with pytest.raises(SyncFailure) as error:
            manager.complete(device, tid)
    assert error.value.status == (401 if interrupt == 'revoke' else 423)
    assert (manager.staging / (tid + '.part')).is_file()
    assert visible_imports(manager) == []
    assert not list(manager.inbox.rglob('*.verified'))
    cat.scan()
    assert cat.browse() == []
    with manager.catalog.connection() as db:
        assert db.execute('SELECT status FROM sync_transfers WHERE id=?', (tid,)).fetchone()[0] == 'pending'
    if interrupt == 'pause':
        manager.set_paused(False)
        assert manager.complete(device, tid)['status'] == 'complete'
        assert len(visible_imports(manager)) == 1


def test_start_timeout_stops_its_own_child_before_releasing_control(tmp_path, monkeypatch):
    from ai_photos import receiver_agent
    real = receiver_agent.subprocess.Popen
    spawned = []

    def never_ready(arguments, **kwargs):  # A child that never takes the agent lease.
        spawned.append(real([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs))
        return spawned[-1]

    monkeypatch.setattr(receiver_agent, 'STARTUP_SECONDS', 0.5)
    monkeypatch.setattr(receiver_agent.subprocess, 'Popen', never_ready)
    controller = ReceiverController(tmp_path / 'state')
    with pytest.raises(TimeoutError, match='was stopped'):
        controller.start(port=0, inbox=tmp_path / 'imports')
    assert spawned and spawned[0].poll() is not None
    assert controller.stop()['stopped']


def test_concurrent_first_use_of_a_safe_directory(tmp_path):
    target = tmp_path / 'fresh' / 'nested'
    barrier, errors = threading.Barrier(8), []

    def enter():
        barrier.wait()
        try:
            with SafeDirectory(target):
                pass
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=enter) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == [] and target.is_dir()


def test_stop_waits_for_a_pending_starter_instead_of_reporting_stopped(tmp_path, monkeypatch):
    from ai_photos import receiver_agent
    controller = ReceiverController(tmp_path / 'state')
    monkeypatch.setattr(receiver_agent, 'CONTROL_WAIT_SECONDS', 0.3)
    holding, release = threading.Event(), threading.Event()

    def starter():  # Simulates start() between spawn and child readiness.
        with receiver_agent.control_lease(controller.home):
            holding.set()
            release.wait(10)

    thread = threading.Thread(target=starter)
    thread.start()
    try:
        assert holding.wait(10)
        with pytest.raises(receiver_agent.ReceiverBusy):
            controller.stop()
    finally:
        release.set()
        thread.join(10)
    assert controller.stop()['stopped']
