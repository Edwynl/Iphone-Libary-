import os
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from ai_photos.catalog import PhotoCatalog
from ai_photos.device_import import STAGING_DIR, DeviceImporter
from test_live_photos import movie, still

DATE = '2026-10-31T09:15:00+11:00'


def item(key, name, size, date=DATE):
    return {'key': key, 'name': name, 'size': size, 'date': date}


def new_staging(dest):
    # The app creates this when it starts copying from the phone.
    staging = dest / STAGING_DIR / '20261031-091500'
    staging.mkdir(parents=True)
    return str(staging)


def copy_in(staging, name, data):
    # Stands in for the app copying one file from the phone into staging.
    path = Path(staging) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return name


def make_library(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    still(library / 'IMG_0001.HEIC')
    (library / 'old_copy.jpg').write_bytes(b'same bytes as a phone photo')
    dest = tmp_path / 'iPhone Imports'
    dest.mkdir()
    catalog = PhotoCatalog(tmp_path / 'state')
    catalog.add_folder(str(library))
    catalog.scan()
    return catalog, DeviceImporter(catalog), library, dest


def test_plan_skips_items_already_imported_or_in_library(tmp_path):
    catalog, importer, library, dest = make_library(tmp_path)
    size = (library / 'IMG_0001.HEIC').stat().st_size
    plan = importer.plan('phone', [item('a', 'img_0001.heic', size), item('b', 'IMG_0002.HEIC', 10)])
    assert plan['selected'] == ['b'] and plan['skipped_library'] == 1


def test_commit_files_new_items_and_drops_exact_duplicates(tmp_path):
    catalog, importer, library, dest = make_library(tmp_path)
    (dest / '2026-10').mkdir()
    still(dest / '2026-10' / 'IMG_0007.HEIC')  # An older photo already owns this name.
    items = [item('dup', 'IMG_0005.JPG', 27), item('live', 'IMG_0007.HEIC', 1000), item('vid', 'IMG_0008.MOV', 500)]
    plan = importer.plan('phone', items)
    staging = new_staging(dest)
    assert plan['selected'] == ['dup', 'live', 'vid']

    items[0]['files'] = [copy_in(staging, 'IMG_0005.JPG', b'same bytes as a phone photo')]
    movie(Path(staging) / 'IMG_0007.MOV', True)
    Image.new('RGB', (6, 6), (1, 2, 3)).save(Path(staging) / 'IMG_0007.HEIC', format='JPEG')
    items[1]['files'] = ['IMG_0007.HEIC', 'IMG_0007.MOV']  # Live Photo motion arrives as a sibling.
    movie(Path(staging) / 'IMG_0008.MOV', False)
    items[2]['files'] = [str(Path(staging) / 'IMG_0008.MOV')]

    result = importer.commit('phone', str(dest), staging, items)
    assert (result['imported'], result['duplicates'], result['items']) == (3, 1, 3)
    month = dest / '2026-10'
    assert sorted(p.name for p in month.iterdir()) == [
        'IMG_0007 (2).HEIC', 'IMG_0007 (2).MOV', 'IMG_0007.HEIC', 'IMG_0008.MOV']
    assert not (dest / STAGING_DIR).exists()
    assert result['library_folder'] == str(dest.resolve())

    # The renamed Live pair is still recognised after scanning the new folder.
    catalog.scan()
    pair = next(r for r in catalog.browse(media_type='photo') if r['name'] == 'IMG_0007 (2).HEIC')
    assert pair['companion'] == str(month.resolve() / 'IMG_0007 (2).MOV')

    # Reconnecting the same phone copies nothing again, duplicates included.
    again = importer.plan('phone', items)
    assert again['selected'] == [] and again['skipped_known'] == 3


def test_unlisted_copied_files_are_still_filed(tmp_path):
    catalog, importer, library, dest = make_library(tmp_path)
    staging = new_staging(dest)
    copy_in(staging, 'IMG_0200.HEIC', b'still')
    copy_in(staging, 'IMG_0200.MOV', b'motion the API did not list')
    stamp = datetime.fromisoformat(DATE).timestamp()
    os.utime(Path(staging) / 'IMG_0200.MOV', (stamp, stamp))  # Filed by its timestamp.
    result = importer.commit('phone', str(dest), staging,
                             [dict(item('s', 'IMG_0200.HEIC', 5), files=['IMG_0200.HEIC'])])
    assert result['imported'] == 2 and result['items'] == 1
    assert sorted(p.name for p in (dest / '2026-10').iterdir()) == ['IMG_0200.HEIC', 'IMG_0200.MOV']


def test_same_names_from_different_dcim_folders_are_both_kept(tmp_path):
    catalog, importer, library, dest = make_library(tmp_path)
    staging = new_staging(dest)
    items = [dict(item('a', 'IMG_0001.JPG', 3), files=[copy_in(staging, '100APPLE/IMG_0001.JPG', b'old')]),
             dict(item('b', 'IMG_0001.JPG', 3, '2026-10-31T10:00:00+11:00'),
                  files=[copy_in(staging, '101APPLE/IMG_0001.JPG', b'new')])]
    result = importer.commit('phone', str(dest), staging, items)
    assert result['imported'] == 2
    assert sorted(p.name for p in (dest / '2026-10').iterdir()) == ['IMG_0001 (2).JPG', 'IMG_0001.JPG']
    assert not (dest / STAGING_DIR).exists()
    with pytest.raises(ValueError):
        importer.commit('phone', str(dest), new_staging(dest), [dict(item('c', 'x.jpg', 1), files=['../../x.jpg'])])


def test_staging_must_belong_to_the_destination(tmp_path):
    catalog, importer, library, dest = make_library(tmp_path)
    with pytest.raises(ValueError):
        importer.commit('phone', str(dest), str(library), [])


def test_destination_inside_an_existing_root_is_not_added_twice(tmp_path):
    catalog, importer, library, dest = make_library(tmp_path)
    inner = library / 'from phone'
    inner.mkdir()
    staging = new_staging(inner)
    copy_in(staging, 'IMG_0100.JPG', b'new!')
    result = importer.commit('phone', str(inner), staging,
                             [dict(item('x', 'IMG_0100.JPG', 4), files=['IMG_0100.JPG'])])
    assert result['library_folder'] == str(library.resolve())
    assert catalog.folders() == [str(library.resolve())]


def test_settings_round_trip(tmp_path):
    catalog, importer, library, dest = make_library(tmp_path)
    assert importer.settings() == {'destination': '', 'auto': False}
    importer.settings({'destination': str(dest), 'auto': True})
    assert DeviceImporter(catalog).settings() == {'destination': str(dest), 'auto': True}


def test_destination_containing_a_library_folder_is_refused(tmp_path):
    catalog, importer, library, dest = make_library(tmp_path)
    with pytest.raises(ValueError, match='already added'):
        importer.settings({'destination': str(tmp_path)})
    assert importer.settings()['destination'] == ''
    importer.settings({'destination': str(library)})  # The library folder itself is fine.
