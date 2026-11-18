import sqlite3
import struct
from pathlib import Path

from PIL import Image

from ai_photos.catalog import PhotoCatalog
from ai_photos.media import LIVE_KEY, probe_video
from ai_photos.organize import virtual_albums


def atom(kind, body):
    return struct.pack('>I4s', 8 + len(body), kind) + body


def default_id(path):
    # Unless a test says otherwise, a still and a clip with the same stem are one Live Photo.
    return 'LIVE-' + Path(path).stem.upper()


def movie(path, live, seconds=3_850_000_000, version=0, identifier=None):
    # mdat before moov, as iPhone writes it; mvhd time counts from 1904-01-01 UTC.
    # A Live clip keeps its content identifier in moov/meta: a 'keys' list and an 'ilst' value.
    times = struct.pack('>QQ', seconds, seconds) if version else struct.pack('>II', seconds, seconds)
    mvhd = atom(b'mvhd', bytes([version, 0, 0, 0]) + times + b'\0' * 80)
    meta = b''
    if live:
        value = (identifier or default_id(path)).encode()
        keys = atom(b'keys', struct.pack('>II', 0, 1) + struct.pack('>I4s', 8 + len(LIVE_KEY), b'mdta') + LIVE_KEY)
        ilst = atom(b'ilst', atom(struct.pack('>I', 1), atom(b'data', struct.pack('>II', 1, 0) + value)))
        meta = atom(b'meta', atom(b'hdlr', b'\0' * 24) + keys + ilst)
    path.write_bytes(atom(b'ftyp', b'qt  ') + atom(b'mdat', b'\0' * 64) + atom(b'moov', mvhd + meta))


def still(path, identifier=None, taken=None):
    """A JPEG carrying Apple's maker note with the Live Photo content identifier (tag 0x11)."""
    value = (identifier or default_id(path)).encode() + b'\0'
    note = (b'Apple iOS\0' + b'\0\x01' + b'MM' + struct.pack('>H', 1)
            + struct.pack('>HHII', 0x11, 2, len(value), 14 + 2 + 12 + 4) + b'\0\0\0\0' + value)
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x927C] = note
    if taken:
        exif.get_ifd(0x8769)[0x9003] = taken  # DateTimeOriginal
    Image.new('RGB', (4, 4), (90, 30, 30)).save(path, format='JPEG', exif=exif)


def test_probe_reads_live_identifier_and_capture_time(tmp_path):
    live, plain, junk = tmp_path / 'a.mov', tmp_path / 'b.mov', tmp_path / 'c.mov'
    movie(live, True, identifier='ABC-123')
    movie(plain, False, version=1)
    junk.write_bytes(b'not a movie')
    flag, taken, identifier = probe_video(live)
    assert flag is True and taken.startswith('2025-') and identifier == 'ABC-123'
    assert probe_video(plain)[0] is False and probe_video(plain)[1].startswith('2025-')
    assert probe_video(plain)[2] is None
    assert probe_video(junk) == (False, None, None)
    assert probe_video(tmp_path / 'missing.mov') == (None, None, None)


def test_still_identifier_from_apple_maker_note(tmp_path):
    from ai_photos.media import still_content_id
    still(tmp_path / 'a.jpg', identifier='ABC-123')
    Image.new('RGB', (4, 4)).save(tmp_path / 'plain.jpg')
    assert still_content_id(tmp_path / 'a.jpg') == 'ABC-123'
    assert still_content_id(tmp_path / 'plain.jpg') == ''
    assert still_content_id(tmp_path / 'missing.jpg') is None


def test_live_motion_is_grouped_with_its_still(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    still(root / 'IMG_1.HEIC')
    movie(root / 'IMG_1.MOV', True)          # Live Photo pair
    movie(root / 'IMG_2.MOV', False)         # ordinary video
    movie(root / 'IMG_3.MOV', True)          # Live motion whose still is gone
    still(root / 'IMG_4.JPG')
    movie(root / 'IMG_4.MOV', False)         # same stem but no Live identifier
    still(root / 'IMG_5.HEIC', identifier='A')
    movie(root / 'IMG_5.MOV', True, identifier='B')  # same stem, a different Live Photo
    (root / 'clip.mp4').write_bytes(b'x')
    c = PhotoCatalog(tmp_path / 'state')
    c.add_folder(str(root))
    c.scan()
    names = lambda kind: sorted(r['name'] for r in c.browse(media_type=kind))
    assert names('photo') == ['IMG_1.HEIC', 'IMG_4.JPG', 'IMG_5.HEIC']
    assert names('video') == ['IMG_2.MOV', 'IMG_3.MOV', 'IMG_4.MOV', 'IMG_5.MOV', 'clip.mp4']
    assert 'IMG_1.MOV' not in names('all') and len(names('all')) == 8
    pair = next(r for r in c.browse(media_type='photo') if r['name'] == 'IMG_1.HEIC')
    assert pair['companion'] == str(root / 'IMG_1.MOV')
    video = next(r for r in c.browse(media_type='video') if r['name'] == 'IMG_2.MOV')
    assert video['taken_at'].startswith('2025-')
    stats = c.stats()
    assert (stats['photos'], stats['videos'], stats['live_photos']) == (3, 5, 1)
    assert c.search('IMG_1')['photos'][0]['name'] == 'IMG_1.HEIC'
    assert len(c.search('IMG_1')['photos']) == 1
    assert sum(m['count'] for m in virtual_albums(c)['timeline']) == 8
    # Recycling the still moves its identifier-confirmed motion file with it.
    plan = c.recycle_plan([str(root / 'IMG_1.HEIC')])
    assert {f['path'] for f in plan['files']} == {str(root / 'IMG_1.HEIC'), str(root / 'IMG_1.MOV')}
    assert plan['confirmed_companions'] == [str(root / 'IMG_1.MOV')] and plan['suggested_companions'] == []
    # Same stem, no identifier or a different one: only mentioned, never moved along.
    for still_name, motion_name in (('IMG_4.JPG', 'IMG_4.MOV'), ('IMG_5.HEIC', 'IMG_5.MOV')):
        plan = c.recycle_plan([str(root / still_name)])
        assert [f['path'] for f in plan['files']] == [str(root / still_name)]
        assert plan['suggested_companions'] == [str(root / motion_name)] and plan['confirmed_companions'] == []
    # Selected on purpose, a suggested file is moved like any other.
    plan = c.recycle_plan([str(root / 'IMG_5.HEIC'), str(root / 'IMG_5.MOV')])
    assert len(plan['files']) == 2 and plan['suggested_companions'] == []

    (root / 'IMG_1.HEIC').unlink()
    c.scan()
    assert 'IMG_1.MOV' in names('video')
    assert c.stats()['live_photos'] == 0


def test_existing_catalog_is_migrated(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    still(root / 'IMG_9.JPG')
    movie(root / 'IMG_9.MOV', True)
    state = tmp_path / 'state'
    state.mkdir()
    db = sqlite3.connect(state / 'catalog.db')
    db.executescript("""
        CREATE TABLE folders (path TEXT PRIMARY KEY COLLATE NOCASE, added_at TEXT NOT NULL);
        CREATE TABLE photos (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE COLLATE NOCASE,
          folder TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
          taken_at TEXT, width INTEGER, height INTEGER, thumb_path TEXT, error TEXT, indexed_at TEXT NOT NULL);
    """)
    for p in root.iterdir():
        s = p.stat()
        db.execute("INSERT INTO photos(path,folder,name,size,mtime_ns,indexed_at) VALUES(?,?,?,?,?,'x')",
                   (str(p), str(root), p.name, s.st_size, s.st_mtime_ns))
    db.execute("INSERT INTO folders VALUES (?, 'x')", (str(root),))
    db.commit()
    db.close()
    c = PhotoCatalog(state)
    assert [r['name'] for r in c.browse(media_type='video')] == ['IMG_9.MOV']
    c.scan()  # Unchanged files are classified without being re-indexed.
    assert c.browse(media_type='video') == []
    assert [Path(r['path']).name for r in c.browse()] == ['IMG_9.JPG']
    # A catalog paired before identifiers were stored reads them once and keeps the pair.
    with c.connection() as db:
        db.execute('UPDATE photos SET content_id=NULL')
    c.scan()
    assert c.browse()[0]['companion'] == str(root / 'IMG_9.MOV')


def test_ai_search_puts_typed_filenames_first(tmp_path):
    from test_catalog import FakeEncoder
    root = tmp_path / '2026 trip'
    root.mkdir()
    Image.new('RGB', (4, 4), (250, 5, 5)).save(root / 'red.jpg')
    Image.new('RGB', (4, 4), (5, 5, 250)).save(root / 'IMG_3164.jpg')
    c = PhotoCatalog(tmp_path / 'state')
    c.add_folder(str(root))
    c.scan(encoder=FakeEncoder())
    found = c.search('IMG_3164', encoder=FakeEncoder())
    assert found['mode'] == 'semantic' and found['filename_matches'] == 1
    assert [p['name'] for p in found['photos']] == ['IMG_3164.jpg']
    # A folder-name query does not turn every file into a "filename" match.
    folder = c.search('2026', encoder=FakeEncoder())
    assert folder['filename_matches'] == 0 and len(folder['photos']) == 2
    # A word query keeps AI results after the filename hits.
    assert [p['name'] for p in c.search('red', encoder=FakeEncoder())['photos']] == ['red.jpg', 'IMG_3164.jpg']


def test_video_posters_are_cached_embedded_and_skip_live_motion(tmp_path, monkeypatch):
    from test_catalog import FakeEncoder
    import ai_photos.catalog as catalog_module
    calls = []

    def fake_poster(path, out, size=384):
        calls.append(Path(path).name)
        if Path(path).suffix.lower() == '.mp4':
            return False  # Windows has no codec for this one.
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (8, 6), (5, 5, 250)).save(out, format='JPEG')
        return True

    monkeypatch.setattr(catalog_module, 'video_thumbnail', fake_poster)
    root = tmp_path / 'source'
    root.mkdir()
    still(root / 'IMG_1.HEIC')
    movie(root / 'IMG_1.MOV', True)
    movie(root / 'IMG_2.MOV', False)
    (root / 'clip.mp4').write_bytes(b'x')
    c = PhotoCatalog(tmp_path / 'state')
    c.add_folder(str(root))
    result = c.scan(encoder=FakeEncoder())
    assert 'IMG_1.MOV' not in calls and result['errors'] == 0
    videos = {r['name']: r for r in c.browse(media_type='video')}
    assert Path(videos['IMG_2.MOV']['thumb_path']).is_file()
    assert videos['clip.mp4']['thumb_path'] == ''
    # The poster frame makes the video findable by content.
    assert c.search('blue', encoder=FakeEncoder())['photos'][0]['name'] == 'IMG_2.MOV'
    calls.clear()
    c.scan(encoder=FakeEncoder())
    assert calls == ['clip.mp4']  # Only the missing poster is retried.


def test_shell_poster_frame_from_a_real_video(tmp_path):
    import os
    import shutil
    import subprocess
    import pytest
    from ai_photos.media import video_thumbnail
    ffmpeg = shutil.which('ffmpeg')
    if os.name != 'nt' or not ffmpeg:
        pytest.skip('needs Windows shell codecs and ffmpeg to make a sample')
    sample = tmp_path / 'sample.mov'  # .mov: some PCs lose the .mp4 thumbnail handler
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=320x240:d=2', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', str(sample)], check=True)
    out = tmp_path / 'poster.jpg'
    if not video_thumbnail(sample, out):
        pytest.skip('this Windows installation provides no H.264 thumbnailer')
    with Image.open(out) as poster:
        r, g, b = poster.convert('RGB').resize((1, 1)).getpixel((0, 0))
    assert r > 150 and g < 90 and b < 90
    assert video_thumbnail(tmp_path / 'missing.mp4', tmp_path / 'none.jpg') is False
