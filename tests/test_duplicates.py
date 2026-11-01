from __future__ import annotations

import shutil
from PIL import Image

from ai_photos.catalog import PhotoCatalog
from ai_photos.duplicates import find_duplicates, sha256_file


def test_exact_duplicates_are_detected_without_deleting(tmp_path):
    root = tmp_path / 'photos'
    root.mkdir()
    Image.new('RGB', (100, 100), '#fc0000').save(root / 'original.png')
    shutil.copy2(root / 'original.png', root / 'copy.png')
    Image.new('RGB', (100, 100), '#0000fc').save(root / 'blue.png')
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(root))
    cat.scan()
    before = sha256_file(root / 'original.png')
    result = find_duplicates(cat)
    assert result['scanned'] == 3
    assert len(result['exact_groups']) == 1
    assert {x['name'] for x in result['exact_groups'][0]} == {'original.png', 'copy.png'}
    assert sha256_file(root / 'original.png') == before
    assert (root / 'copy.png').exists()
    assert find_duplicates(cat)['fingerprints_calculated'] == 0


def test_groups_carry_what_a_review_card_shows_and_skip_live_motion(tmp_path):
    root = tmp_path / 'photos'
    root.mkdir()
    Image.new('RGB', (100, 100), '#fc0000').save(root / 'original.png')
    shutil.copy2(root / 'original.png', root / 'copy.png')
    (root / 'IMG_1.MOV').write_bytes(b'motion')
    shutil.copy2(root / 'IMG_1.MOV', root / 'IMG_2.MOV')
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(root))
    cat.scan()
    with cat.connection() as db:  # Live Photo motion is reviewed and recycled through its still.
        db.execute("UPDATE photos SET media_kind='live_motion' WHERE name LIKE '%.MOV'")
        db.commit()

    groups = find_duplicates(cat)['exact_groups']
    assert len(groups) == 1
    card = groups[0][0]
    assert {'path', 'name', 'thumb_path', 'mtime_ns', 'taken_at', 'media_kind', 'companion'} <= card.keys()
    assert card['thumb_path']


def test_no_auto_delete_or_false_exact(tmp_path):
    root = tmp_path / 'photos'
    root.mkdir()
    Image.new('RGB', (100, 100), '#ff0000').save(root / 'one.png')
    Image.new('RGB', (100, 100), '#ee0000').save(root / 'two.png')
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(root))
    cat.scan()
    result = find_duplicates(cat)
    assert not result['exact_groups']
    assert len(list(root.glob('*.png'))) == 2
