import os
from datetime import datetime

import pytest
from PIL import Image

from ai_photos.catalog import _MEDIA_FILTERS, _ORDERS, PhotoCatalog


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


def test_every_browse_page_walks_an_index_instead_of_sorting(tmp_path):
    catalog = PhotoCatalog(tmp_path / 'state')
    with catalog.connection() as db:
        for where in _MEDIA_FILTERS.values():
            for direction in _ORDERS.values():
                plan = ' '.join(row['detail'] for row in db.execute(
                    f"EXPLAIN QUERY PLAN SELECT id FROM photos WHERE {where} "
                    f"ORDER BY sort_time {direction},id {direction} LIMIT 60 OFFSET 6000"))
                assert 'TEMP B-TREE' not in plan and 'USING' in plan and 'INDEX' in plan, plan


def test_sort_key_follows_every_write_path(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    jpeg(library / 'a.jpg', taken='2020:01:01 00:00:00')
    jpeg(library / 'b.jpg', taken='2021:01:01 00:00:00')
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(library))
    catalog.scan()
    def names():
        return [row['name'] for row in catalog.browse()]

    assert names() == ['b.jpg', 'a.jpg']
    with catalog.connection() as db:
        db.execute("UPDATE photos SET taken_at='2022:01:01 00:00:00' WHERE name='a.jpg'")
        db.commit()
    assert names() == ['a.jpg', 'b.jpg']
    with catalog.connection() as db:  # Without a capture date the file time stands in.
        db.execute("UPDATE photos SET taken_at=NULL,mtime_ns=? WHERE name='b.jpg'",
                   (int(datetime(2023, 1, 1).timestamp()) * 10**9,))
        db.commit()
    assert names() == ['b.jpg', 'a.jpg']
    # A rewrite of a changed file goes through the scanner's own upsert.
    jpeg(library / 'a.jpg', taken='2024:01:01 00:00:00')
    catalog.scan()
    assert names() == ['a.jpg', 'b.jpg']


def test_an_existing_catalog_is_migrated_in_place(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    jpeg(library / 'old.jpg', taken='2020:01:01 00:00:00')
    jpeg(library / 'new.jpg', taken='2021:01:01 00:00:00')
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(library))
    catalog.scan()
    with catalog.connection() as db:  # As an older version left it.
        db.execute("DROP TRIGGER photos_sort_insert")
        db.execute("DROP TRIGGER photos_sort_update")
        db.execute("DROP INDEX idx_photos_sort")
        db.execute("DROP INDEX idx_photos_kind_sort")
        db.execute("CREATE INDEX idx_photos_kind ON photos(media_kind, mtime_ns DESC, id DESC)")
        db.execute("ALTER TABLE photos DROP COLUMN sort_time")
        db.commit()
    migrated = PhotoCatalog(tmp_path / 'state')
    assert [row['name'] for row in migrated.browse()] == ['new.jpg', 'old.jpg']
    with migrated.connection() as db:
        indexes = {row[1] for row in db.execute("PRAGMA index_list(photos)")}
    assert {'idx_photos_sort', 'idx_photos_kind_sort'} <= indexes and 'idx_photos_kind' not in indexes


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
