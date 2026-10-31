import os
from datetime import datetime

import pytest
from PIL import Image

from ai_photos.catalog import PhotoCatalog


def jpeg(path, taken=None, modified=None):
    exif = Image.Exif()
    if taken:
        exif[306] = taken  # EXIF DateTime, local time as cameras write it.
    Image.new('RGB', (6, 6)).save(path, format='JPEG', exif=exif)
    if modified:
        stamp = datetime.fromisoformat(modified).timestamp()
        os.utime(path, (stamp, stamp))


def test_browse_sorts_by_capture_date_then_file_time(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    # Copied today, but taken on 1 September: the capture date wins over the file time.
    jpeg(library / 'taken.jpg', taken='2026:09:01 10:00:00')
    # A saved image or screenshot has no capture date; its file time stands in.
    jpeg(library / 'saved.jpg', modified='2026-09-10T12:00:00')
    jpeg(library / 'video.jpg', modified='2026-01-01T00:00:00')
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(library))
    catalog.scan()
    with catalog.connection() as db:  # Videos store an ISO UTC creation time.
        db.execute("UPDATE photos SET taken_at='2026-09-05T00:00:00+00:00' WHERE name='video.jpg'")
        db.commit()

    newest = [row['name'] for row in catalog.browse()]
    assert newest == ['saved.jpg', 'video.jpg', 'taken.jpg']
    assert [row['name'] for row in catalog.browse(order='oldest')] == newest[::-1]
    assert [row['name'] for row in catalog.browse(limit=1, offset=1)] == ['video.jpg']
    with pytest.raises(ValueError):
        catalog.browse(order='random')
