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


def test_capture_time_comes_from_the_exif_sub_ifd(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()

    def photo(name, original=None, edited=None):
        exif = Image.Exif()
        if original:
            exif.get_ifd(0x8769)[0x9003] = original  # DateTimeOriginal lives in the Exif sub-IFD.
        if edited:
            exif[306] = edited  # DateTime: the last edit.
        Image.new('RGB', (6, 6)).save(library / name, format='JPEG', exif=exif)

    photo('original_only.jpg', original='2020:07:04 09:30:00')
    photo('edited_later.jpg', original='2019:01:01 00:00:00', edited='2026:10:01 08:00:00')
    photo('edit_time_only.jpg', edited='2021:01:01 00:00:00')
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(library))
    catalog.scan()
    oldest = ['edited_later.jpg', 'original_only.jpg', 'edit_time_only.jpg']
    assert [row['name'] for row in catalog.browse(order='oldest')] == oldest

    # A catalog indexed before the fix stored the edit time; its photos are read once more.
    with catalog.connection() as db:
        db.execute("UPDATE photos SET taken_at='2026:10:01 08:00:00',capture_read=NULL WHERE name='edited_later.jpg'")
        db.commit()
    catalog.scan()
    assert [row['name'] for row in catalog.browse(order='oldest')] == oldest
