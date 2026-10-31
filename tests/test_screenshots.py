import os

from PIL import Image

from ai_photos.catalog import PhotoCatalog


def png(path, comment=None):
    exif = Image.Exif()
    if comment:
        exif[0x8769] = {0x9286: b'ASCII\0\0\0' + comment.encode()}  # EXIF UserComment.
    Image.new('RGB', (6, 12), (20, 20, 20)).save(path, format='PNG', exif=exif)


def names(catalog, media_type):
    return sorted(row['name'] for row in catalog.browse(media_type=media_type))


def test_screenshots_are_recognised_by_exif_comment_or_name(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    png(library / 'IMG_0063.PNG', 'Screenshot')  # As an iPhone saves it.
    png(library / 'Screenshot 2026-10-31 091500.png')  # Windows / macOS / Android naming.
    Image.new('RGB', (6, 6)).save(library / '屏幕截图 2026-10-31.jpg', format='JPEG')
    png(library / 'ACKQ0838.PNG')  # A saved image, not a screenshot.
    Image.new('RGB', (6, 6)).save(library / 'IMG_0001.JPG', format='JPEG')
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(library))
    catalog.scan()

    assert names(catalog, 'screenshot') == ['IMG_0063.PNG', 'Screenshot 2026-10-31 091500.png', '屏幕截图 2026-10-31.jpg']
    assert len(names(catalog, 'photo')) == 5  # Screenshots stay in Photos too.
    assert catalog.stats()['screenshots'] == 3


def test_changed_file_is_checked_again(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    path = library / 'IMG_0100.PNG'
    png(path)
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(library))
    catalog.scan()
    assert names(catalog, 'screenshot') == []

    png(path, 'Screenshot')
    stamp = path.stat().st_mtime + 10
    os.utime(path, (stamp, stamp))
    catalog.scan()
    assert names(catalog, 'screenshot') == ['IMG_0100.PNG']
