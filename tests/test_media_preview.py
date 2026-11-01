from pathlib import Path
from PIL import Image
import pytest
from ai_photos.catalog import PhotoCatalog


def test_media_filter_and_candidate_pair(tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    image = root / 'IMG_123.HEIC'
    # JPEG bytes under a HEIC name are enough to exercise indexing and pairing.
    Image.new('RGB', (4, 4)).save(image, format='JPEG')
    movie = root / 'IMG_123.MOV'; movie.write_bytes(b'synthetic video')
    unrelated = root / 'other.mp4'; unrelated.write_bytes(b'not a real video')
    c = PhotoCatalog(tmp_path / 'state'); c.add_folder(str(root)); c.scan()
    assert len(c.browse(media_type='photo')) == 1
    assert len(c.browse(media_type='video')) == 2
    assert len(c.browse(media_type='all')) == 3
    plan = c.recycle_plan([str(image)])
    # No matching Live identifiers: the same-name video is only suggested, not moved along.
    assert [x['path'] for x in plan['files']] == [str(image)]
    assert plan['suggested_companions'] == [str(movie)] and plan['confirmed_companions'] == []
    assert image.exists() and movie.exists()
    image.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed since scan'):
        c.recycle_plan([str(image)])


def test_sync_resource_is_protected(tmp_path):
    root=tmp_path/'source'; root.mkdir(); image=root/'a.jpg'
    Image.new('RGB',(2,2)).save(image)
    c=PhotoCatalog(tmp_path/'state'); c.add_folder(str(root)); c.scan()
    with c.connection() as db:
        db.execute('CREATE TABLE sync_transfers (stored_path TEXT)')
        db.execute('INSERT INTO sync_transfers VALUES (?)',(str(image),))
    with pytest.raises(ValueError, match='protected'):
        c.recycle_plan([str(image)])
    assert image.exists()
