"""Repeat backups read only what changed, and fail closed on anything suspicious."""
from __future__ import annotations

import json
import os

import pytest
from ai_photos import backup
from ai_photos.backup import backup_local, restore_local
from ai_photos.catalog import PhotoCatalog
from ai_photos.file_safety import SafeDirectory
from PIL import Image


@pytest.fixture
def library(tmp_path):
    source = tmp_path / 'originals'
    source.mkdir()
    for name, color in (('a.png', 'red'), ('b.png', 'blue')):
        Image.new('RGB', (8, 8), color).save(source / name)
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(source))
    catalog.scan()
    return catalog, source, tmp_path / 'backup'


@pytest.fixture
def reads(monkeypatch):
    """Every file whose whole content a backup reads, source or backup object."""
    seen = []
    hash_source, hash_object = backup.sha256_file, SafeDirectory.digest
    monkeypatch.setattr(backup, 'sha256_file', lambda path: seen.append(str(path)) or hash_source(path))
    monkeypatch.setattr(SafeDirectory, 'digest',
                        lambda self, name: seen.append(str(self.path / name)) or hash_object(self, name))
    return seen


def restore(target):
    restored = target.parent / 'restored'
    restore_local(target, restored, apply=True)
    return restored


def test_a_plan_reads_no_file_contents(library, reads):
    catalog, source, target = library
    plan = backup_local(catalog, target)
    assert reads == []
    assert plan['complete'] and plan['files'] == 2
    assert plan['bytes_planned'] == sum(p.stat().st_size for p in source.iterdir())
    assert not target.exists()


def test_plan_after_a_backup_counts_only_changed_files(library):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    assert backup_local(catalog, target)['bytes_planned'] == 0
    Image.new('RGB', (8, 8), 'green').save(source / 'a.png')
    catalog.scan()
    assert backup_local(catalog, target)['bytes_planned'] == (source / 'a.png').stat().st_size


def test_repeat_backup_reads_nothing_and_still_publishes_a_snapshot(library, reads):
    catalog, source, target = library
    first = backup_local(catalog, target, apply=True)
    assert first['copied'] == 2
    reads.clear()
    second = backup_local(catalog, target, apply=True)
    assert reads == []
    assert (second['copied'], second['unchanged'], second['complete']) == (0, 2, True)
    assert second['snapshot_id'] != first['snapshot_id']
    restored = restore(target)
    assert {p.name: p.read_bytes() for p in restored.rglob('*.png')} == \
           {p.name: p.read_bytes() for p in source.iterdir()}


def test_only_a_changed_file_is_read(library, reads):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    Image.new('RGB', (9, 9), 'green').save(source / 'a.png')
    catalog.scan()
    reads.clear()
    result = backup_local(catalog, target, apply=True)
    assert (result['copied'], result['unchanged'], result['complete']) == (1, 1, True)
    assert not [p for p in reads if p.endswith('b.png')]
    assert str(source / 'a.png') in reads


def test_verify_detects_same_size_corruption_the_quick_run_cannot(library):
    catalog, _, target = library
    backup_local(catalog, target, apply=True)
    damaged = next((target / 'objects').rglob('*.png'))
    damaged.write_bytes(b'x' * damaged.stat().st_size)
    # The documented limit of a quick run: the size still matches, so it is not read.
    assert backup_local(catalog, target, apply=True)['complete']
    last_good = (target / 'backup-manifest.json').read_bytes()
    result = backup_local(catalog, target, apply=True, verify=True)
    assert not result['complete'] and 'corrupt' in result['issues'][0]['error']
    assert damaged.read_bytes() == b'x' * damaged.stat().st_size  # Never replaced.
    assert (target / 'backup-manifest.json').read_bytes() == last_good


def test_verify_detects_a_source_edited_without_changing_its_time(library):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    photo = source / 'a.png'
    info = photo.stat()
    photo.write_bytes(b'y' * info.st_size)
    os.utime(photo, ns=(info.st_atime_ns, info.st_mtime_ns))
    result = backup_local(catalog, target, apply=True, verify=True)
    assert result['complete'] and (result['copied'], result['unchanged']) == (1, 1)


def test_a_missing_object_is_copied_again(library):
    catalog, _, target = library
    backup_local(catalog, target, apply=True)
    next((target / 'objects').rglob('*.png')).unlink()
    result = backup_local(catalog, target, apply=True)
    assert (result['copied'], result['unchanged'], result['complete']) == (1, 1, True)
    restore(target)


def test_a_missing_object_with_a_changed_source_is_not_backed_up_silently(library):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    manifest = json.loads((target / 'backup-manifest.json').read_text(encoding='utf-8'))
    record = next(r for r in manifest['files'] if r['path'].endswith('a.png'))
    (target / record['backup_path']).unlink()
    photo = source / 'a.png'
    info = photo.stat()
    photo.write_bytes(b'z' * info.st_size)  # Differs from the recorded checksum, same size and time.
    os.utime(photo, ns=(info.st_atime_ns, info.st_mtime_ns))
    result = backup_local(catalog, target, apply=True)
    assert not result['complete'] and 'checksum' in result['issues'][0]['error'].lower()


def test_a_v1_manifest_is_not_trusted_for_reuse(library, reads):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    manifest_path = target / 'backup-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest = {'version': 1, 'complete': True, 'issues': [], 'note': '',
                'files': [dict(r, backup_path=r['restore_path']) for r in manifest['files']]}
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    reads.clear()
    backup_local(catalog, target, apply=True)
    assert str(source / 'a.png') in reads and str(source / 'b.png') in reads


def test_progress_reports_the_run_end_to_end(library):
    catalog, _, target = library
    events = []
    backup_local(catalog, target, apply=True, progress=events.append)
    assert {'stage': 'backing_up', 'done': 2, 'total': 2} in events
    assert {'stage': 'verifying', 'done': 2, 'total': 2} in events
    events.clear()
    backup_local(catalog, target, progress=events.append)
    assert events == []  # A plan finishes immediately.


def test_full_verification_rereads_everything(library, reads):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    reads.clear()
    result = backup_local(catalog, target, apply=True, verify=True)
    assert result['complete'] and result['unchanged'] == 2
    assert str(source / 'a.png') in reads and str(source / 'b.png') in reads
    objects = [p for p in reads if 'objects' in p]
    assert len(objects) == 4  # Each object compared, then rechecked before publishing.
