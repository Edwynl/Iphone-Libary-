"""History retention and last-good recovery under failed/interrupted backups."""
import hashlib
import json
from pathlib import Path

import pytest
from ai_photos.backup import backup_history, backup_local, restore_local
from ai_photos.catalog import PhotoCatalog
from ai_photos.file_safety import SafeDirectory
from PIL import Image


@pytest.fixture
def library(tmp_path):
    source = tmp_path / 'originals'
    source.mkdir()
    for name, color in [('A.png', 'blue'), ('B.png', 'green')]:
        Image.new('RGB', (12, 12), color).save(source / name)
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(source))
    catalog.scan()
    return catalog, source, tmp_path / 'backup'


def changed(catalog, source):
    for name, color in [('A.png', 'orange'), ('B.png', 'red')]:
        Image.new('RGB', (22, 22), color).save(source / name)
    catalog.scan()


def originals(source):
    return {p.name: p.read_bytes() for p in source.glob('*.png')}


def restored_bytes(backup, target, snapshot=None):
    result = restore_local(backup, target, apply=True, snapshot=snapshot)
    assert result['complete']
    return {p.name: p.read_bytes() for p in target.rglob('*.png')}


def test_change_between_backups_retains_and_restores_both_versions(library, tmp_path):
    catalog, source, target = library
    before = originals(source)
    first = backup_local(catalog, target, apply=True)
    first_manifest = (target / 'snapshots' / (first['snapshot_id'] + '.json')).read_bytes()
    old_objects = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (target / 'objects').rglob('*.png')}
    changed(catalog, source)
    after = originals(source)
    second = backup_local(catalog, target, apply=True)
    assert first['complete'] and second['complete'] and first['snapshot_id'] != second['snapshot_id']
    assert second['latest_published']
    assert restored_bytes(target, tmp_path / 'restore-v1', first['snapshot_id']) == before
    assert restored_bytes(target, tmp_path / 'restore-v2', second['snapshot_id']) == after
    assert restored_bytes(target, tmp_path / 'restore-latest') == after
    assert originals(source) == after
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == value for p, value in old_objects.items())
    assert (target / 'snapshots' / (first['snapshot_id'] + '.json')).read_bytes() == first_manifest
    history = backup_history(target)
    assert history['complete'] and len(history['snapshots']) == 2
    assert [row['snapshot_id'] for row in history['snapshots'] if row['latest']] == [second['snapshot_id']]


def test_failed_second_copy_preserves_last_good_and_retry_can_reuse_orphan(library, tmp_path, monkeypatch):
    catalog, source, target = library
    old = originals(source)
    first = backup_local(catalog, target, apply=True)
    last_good = (target / 'backup-manifest.json').read_bytes()
    changed(catalog, source)
    copy = SafeDirectory.copy
    copied = []
    with monkeypatch.context() as patch:
        def injected(directory, path, name, **kwargs):
            if Path(path).name == 'B.png':
                assert copied == ['A.png']
                raise OSError('Injected second-resource disk failure')
            result = copy(directory, path, name, **kwargs)
            copied.append(Path(path).name)
            return result
        patch.setattr(SafeDirectory, 'copy', injected)
        failed = backup_local(catalog, target, apply=True)
    assert not failed['complete'] and not failed['latest_published'] and failed['snapshot_id'] is None
    assert (target / 'backup-manifest.json').read_bytes() == last_good
    assert len(backup_history(target)['snapshots']) == 1
    assert restored_bytes(target, tmp_path / 'restore-good') == old
    assert restored_bytes(target, tmp_path / 'restore-good-explicit', first['snapshot_id']) == old
    retry = backup_local(catalog, target, apply=True)
    assert retry['complete'] and retry['copied'] == 1 and retry['unchanged'] == 1
    assert restored_bytes(target, tmp_path / 'restore-retry') == originals(source)


@pytest.mark.parametrize('boundary', ['snapshot', 'latest'])
def test_manifest_publication_failure_cannot_invalidate_previous_backup(library, tmp_path, monkeypatch, boundary):
    catalog, source, target = library
    old = originals(source)
    first = backup_local(catalog, target, apply=True)
    last_good = (target / 'backup-manifest.json').read_bytes()
    changed(catalog, source)
    write = SafeDirectory.write
    with monkeypatch.context() as patch:
        def injected(directory, name, data, **kwargs):
            if (boundary == 'latest' and name == 'backup-manifest.json') or (boundary == 'snapshot' and
                    directory.path.name == 'snapshots' and name != first['snapshot_id'] + '.json'):
                raise OSError('Injected manifest publication failure')
            return write(directory, name, data, **kwargs)
        patch.setattr(SafeDirectory, 'write', injected)
        result = backup_local(catalog, target, apply=True)
    assert not result['complete'] and not result['latest_published']
    assert (target / 'backup-manifest.json').read_bytes() == last_good
    assert restored_bytes(target, tmp_path / ('restore-' + boundary)) == old
    if boundary == 'latest':
        assert result['snapshot_id'] is not None
        assert restored_bytes(target, tmp_path / 'restore-unselected', result['snapshot_id']) == originals(source)


def test_interrupted_backup_does_not_publish_a_partial_snapshot(library, tmp_path, monkeypatch):
    catalog, source, target = library
    old = originals(source)
    backup_local(catalog, target, apply=True)
    last_good = (target / 'backup-manifest.json').read_bytes()
    changed(catalog, source)
    copy = SafeDirectory.copy
    with monkeypatch.context() as patch:
        def interrupted(directory, path, name, **kwargs):
            if Path(path).name == 'B.png':
                raise KeyboardInterrupt('Simulated process interruption')
            return copy(directory, path, name, **kwargs)
        patch.setattr(SafeDirectory, 'copy', interrupted)
        with pytest.raises(KeyboardInterrupt):
            backup_local(catalog, target, apply=True)
    assert (target / 'backup-manifest.json').read_bytes() == last_good
    assert restored_bytes(target, tmp_path / 'restore-interrupted') == old
    assert len(backup_history(target)['snapshots']) == 1


def test_corrupt_immutable_object_is_never_silently_replaced(library):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    last_good = (target / 'backup-manifest.json').read_bytes()
    object_path = next((target / 'objects').rglob('*.png'))
    object_path.write_bytes(b'externally damaged backup')
    stamp = object_path.stat().st_mtime_ns
    result = backup_local(catalog, target, apply=True)
    assert not result['complete']
    assert object_path.read_bytes() == b'externally damaged backup'
    assert object_path.stat().st_mtime_ns == stamp
    assert (target / 'backup-manifest.json').read_bytes() == last_good


def test_final_checksum_failure_does_not_publish(library, tmp_path, monkeypatch):
    catalog, source, target = library
    old = originals(source)
    backup_local(catalog, target, apply=True)
    last_good = (target / 'backup-manifest.json').read_bytes()
    changed(catalog, source)
    copy = SafeDirectory.copy
    with monkeypatch.context() as patch:
        def damaged(directory, path, name, **kwargs):
            result = copy(directory, path, name, **kwargs)
            (directory.path / name).write_bytes(b'damage after copy')
            return result
        patch.setattr(SafeDirectory, 'copy', damaged)
        result = backup_local(catalog, target, apply=True)
    assert not result['complete'] and result['snapshot_id'] is None
    assert (target / 'backup-manifest.json').read_bytes() == last_good
    assert restored_bytes(target, tmp_path / 'restore-final-hash') == old


def legacy_backup(source, target):
    records = []
    for path in sorted(source.glob('*.png')):
        relative = 'a' * 16 + '/' + path.name
        out = target / relative
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(path.read_bytes())
        records.append({'folder': str(source.resolve()), 'backup_path': relative,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size,
                        'resources': []})
    payload = json.dumps({'version': 1, 'complete': True, 'files': records, 'issues': []}).encode()
    (target / 'backup-manifest.json').write_bytes(payload)
    return payload


def test_legacy_v1_migration_preserves_files_manifest_and_restore(library, tmp_path):
    catalog, source, target = library
    old = originals(source)
    payload = legacy_backup(source, target)
    ident = 'legacy-' + hashlib.sha256(payload).hexdigest()
    assert backup_history(target)['snapshots'][0]['snapshot_id'] == ident
    assert restored_bytes(target, tmp_path / 'restore-before-migration', ident) == old
    changed(catalog, source)
    result = backup_local(catalog, target, apply=True)
    assert result['complete'] and len(backup_history(target)['snapshots']) == 2
    assert (target / 'snapshots' / (ident + '.json')).read_bytes() == payload
    assert restored_bytes(target, tmp_path / 'restore-legacy', ident) == old
    assert restored_bytes(target, tmp_path / 'restore-new') == originals(source)
    assert {p.name: p.read_bytes() for p in (target / ('a' * 16)).glob('*.png')} == old


def test_failed_v1_migration_keeps_supported_default_restore(library, tmp_path, monkeypatch):
    catalog, source, target = library
    old = originals(source)
    payload = legacy_backup(source, target)
    changed(catalog, source)
    with monkeypatch.context() as patch:
        patch.setattr(SafeDirectory, 'copy', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('Disk failure')))
        assert not backup_local(catalog, target, apply=True)['complete']
    assert (target / 'backup-manifest.json').read_bytes() == payload
    assert restored_bytes(target, tmp_path / 'restore-failed-migration') == old


def test_identical_contents_share_object_but_restore_both_original_names(library, tmp_path):
    catalog, source, target = library
    (source / 'B.png').write_bytes((source / 'A.png').read_bytes())
    catalog.scan()
    result = backup_local(catalog, target, apply=True)
    assert result['complete'] and result['copied'] == 1 and result['unchanged'] == 1
    assert len(list((target / 'objects').rglob('*.png'))) == 1
    assert restored_bytes(target, tmp_path / 'restore-duplicates') == originals(source)


def test_invalid_existing_manifest_is_not_overwritten(library):
    catalog, source, target = library
    target.mkdir()
    (target / 'backup-manifest.json').write_bytes(b'user-file-not-a-manifest')
    result = backup_local(catalog, target, apply=True)
    assert not result['complete']
    assert (target / 'backup-manifest.json').read_bytes() == b'user-file-not-a-manifest'
    assert not (target / 'objects').exists()


def test_restore_preflights_all_objects_before_writing(library, tmp_path):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    manifest = json.loads((target / 'backup-manifest.json').read_text())
    (target / manifest['files'][1]['backup_path']).write_bytes(b'damaged second file')
    destination = tmp_path / 'restore-corrupt'
    with pytest.raises(OSError, match='checksum'):
        restore_local(target, destination, apply=True)
    assert not destination.exists()


@pytest.mark.parametrize('snapshot', ['../escape', 'folder\\id', 'a' * 31, 'CON', '/absolute'])
def test_restore_refuses_unsafe_snapshot_ids(library, tmp_path, snapshot):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    destination = tmp_path / 'restore-unsafe'
    with pytest.raises(ValueError, match='snapshot ID'):
        restore_local(target, destination, apply=True, snapshot=snapshot)
    assert not destination.exists()


def test_manifest_traversal_cannot_write_outside_restore(library, tmp_path):
    catalog, source, target = library
    backup_local(catalog, target, apply=True)
    manifest = json.loads((target / 'backup-manifest.json').read_text())
    manifest['files'][0]['restore_path'] = '../outside.png'
    (target / 'backup-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Unsafe manifest path'):
        restore_local(target, tmp_path / 'restore-traversal', apply=True)
    assert not (tmp_path / 'outside.png').exists()


def test_history_and_explicit_restore_work_without_latest_pointer(library, tmp_path):
    catalog, source, target = library
    result = backup_local(catalog, target, apply=True)
    # Simulate a lost pointer, not a lost snapshot. No user data is removed.
    (target / 'backup-manifest.json').rename(target / 'saved-pointer.json')
    history = backup_history(target)
    assert len(history['snapshots']) == 1 and not history['snapshots'][0]['latest']
    assert restored_bytes(target, tmp_path / 'restore-no-pointer', result['snapshot_id']) == originals(source)


def test_exclusive_snapshot_write_refuses_replacement(tmp_path):
    with SafeDirectory(tmp_path / 'snapshots') as directory:
        directory.write('existing.json', b'first version', replace=False)
        with pytest.raises(FileExistsError):
            directory.write('existing.json', b'new version', replace=False)
    assert (tmp_path / 'snapshots/existing.json').read_bytes() == b'first version'


def test_error_after_atomic_pointer_publish_keeps_both_snapshots_restorable(library, tmp_path, monkeypatch):
    catalog, source, target = library
    old = originals(source)
    first = backup_local(catalog, target, apply=True)
    changed(catalog, source)
    write = SafeDirectory.write
    with monkeypatch.context() as patch:
        def injected(directory, name, data, **kwargs):
            write(directory, name, data, **kwargs)
            if name == 'backup-manifest.json':
                raise OSError('Injected durability error after atomic rename')
        patch.setattr(SafeDirectory, 'write', injected)
        result = backup_local(catalog, target, apply=True)
    assert not result['complete'] and result['latest_published']
    assert restored_bytes(target, tmp_path / 'restore-old-after-error', first['snapshot_id']) == old
    assert restored_bytes(target, tmp_path / 'restore-new-after-error') == originals(source)


def test_history_can_find_good_snapshots_despite_corrupt_pointer(library, tmp_path):
    catalog, source, target = library
    result = backup_local(catalog, target, apply=True)
    (target / 'backup-manifest.json').write_bytes(b'corrupt latest pointer')
    history = backup_history(target)
    assert history['issues'] and not history['complete']
    assert history['snapshots'][0]['snapshot_id'] == result['snapshot_id']
    assert restored_bytes(target, tmp_path / 'restore-corrupt-pointer', result['snapshot_id']) == originals(source)


def test_cli_lists_and_restores_a_selected_old_snapshot(library, tmp_path, monkeypatch, capsys):
    from ai_photos.cli import main
    catalog, source, target = library
    old = originals(source)
    first = backup_local(catalog, target, apply=True)
    changed(catalog, source)
    backup_local(catalog, target, apply=True)
    monkeypatch.setenv('AI_PHOTOS_HOME', str(tmp_path / 'cli-state'))
    assert main(['backup-history', str(target)]) == 0
    history = json.loads(capsys.readouterr().out)
    assert len(history['snapshots']) == 2
    destination = tmp_path / 'cli-restore'
    assert main(['restore-local', str(target), str(destination), '--snapshot', first['snapshot_id'], '--apply']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['snapshot_id'] == first['snapshot_id']
    assert {p.name: p.read_bytes() for p in destination.rglob('*.png')} == old
