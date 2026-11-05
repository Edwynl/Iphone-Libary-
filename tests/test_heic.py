"""HEIC, the iPhone's default format, works from a default install."""
from __future__ import annotations

import shutil
from pathlib import Path

from ai_photos.catalog import PhotoCatalog
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / 'fixtures' / 'tiny.heic'  # 32x24, taken 2024:03:05 08:30:00.


def test_a_real_heic_gets_a_thumbnail_and_its_capture_date(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    shutil.copy(FIXTURE, library / 'IMG_0001.HEIC')
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(library))
    assert catalog.scan()['errors'] == 0
    photo = catalog.browse()[0]
    assert (photo['width'], photo['height'], photo['taken_at']) == (32, 24, '2024:03:05 08:30:00')
    assert photo['error'] is None
    with Image.open(photo['thumb_path']) as thumbnail:
        assert thumbnail.format == 'JPEG'


def test_the_decoder_is_in_every_install_and_is_the_decode_only_build():
    core = (ROOT / 'engine/requirements-core.txt').read_text(encoding='utf-8').splitlines()
    assert any(line.startswith('pi-heif') for line in core)
    assert not any(line.startswith('pillow-heif') for line in core)  # Its Windows wheels are GPLv2.
    for lock in ('requirements-test.lock.txt', 'requirements-ai.lock.txt'):
        pins = (ROOT / 'engine' / lock).read_text(encoding='utf-8')
        assert 'pi-heif==' in pins and 'pillow-heif==' not in pins
