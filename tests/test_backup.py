from __future__ import annotations

from PIL import Image
import pytest

from ai_photos.backup import backup_local, backup_s3
from ai_photos.catalog import PhotoCatalog


def test_local_backup_dryrun_and_verification(tmp_path):
    root = tmp_path / 'originals'
    root.mkdir()
    img = root / 'holiday.png'
    Image.new('RGB', (12, 12), 'orange').save(img)
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(root))
    catalog.scan()
    target = tmp_path / 'backup'
    planned = backup_local(catalog, target)
    assert planned['dry_run'] and planned['files'] == 1 and not target.exists()
    actual = backup_local(catalog, target, apply=True)
    assert actual['copied'] == 1
    assert backup_local(catalog, target, apply=True)['unchanged'] == 1
    assert list(target.rglob('*.png'))[0].read_bytes() == img.read_bytes()
    assert img.is_file()


def test_prevent_local_backup_loops(tmp_path):
    root = tmp_path / 'pictures'
    root.mkdir()
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(root))
    with pytest.raises(ValueError):
        backup_local(cat, root / 'backup', apply=True)
    with pytest.raises(ValueError):
        backup_local(cat, tmp_path, apply=True)


def test_cloud_requires_https_and_explicit_apply(tmp_path):
    cat = PhotoCatalog(tmp_path / 'state')
    with pytest.raises(ValueError):
        backup_s3(cat, bucket='bucket', endpoint_url='http://localhost:9000')
    result = backup_s3(cat, bucket='example-bucket', endpoint_url='https://example.com')
    assert result['dry_run'] and result['objects'] == 0 and result['uploaded'] == 0
