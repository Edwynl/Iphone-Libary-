from __future__ import annotations

import http.client
import json
from io import BytesIO

import pytest
from PIL import Image

from ai_photos.catalog import PhotoCatalog
from ai_photos.mobile import MobileServer, _safe_name


@pytest.fixture
def mobile(tmp_path):
    catalog = PhotoCatalog(tmp_path / 'state')
    server = MobileServer(catalog, inbox=tmp_path / 'iPhone Imports', port=0, token='x' * 40)
    server.start()
    try:
        yield server
    finally:
        server.close()


def request(server, method, path, body=b'', headers=None):
    con = http.client.HTTPConnection('127.0.0.1', server.port, timeout=4)
    con.request(method, path, body=body, headers=headers or {})
    response = con.getresponse()
    data = response.read()
    result = (response.status, data, dict(response.getheaders()))
    con.close()
    return result


def test_loopback_and_authorization(mobile):
    status, _, _ = request(mobile, 'GET', '/')
    assert status == 200
    status, _, _ = request(mobile, 'GET', '/api/photos')
    assert status == 401
    status, body, _ = request(mobile, 'GET', '/api/status', headers={'Authorization': 'Bearer ' + mobile.token})
    assert status == 200
    assert json.loads(body)['photos'] == 0


def test_upload_idempotent_and_originals_untouched(mobile):
    image = BytesIO()
    Image.new('RGB', (24, 24), 'purple').save(image, 'PNG')
    data = image.getvalue()
    headers = {'Authorization': 'Bearer ' + mobile.token, 'X-Filename': '../photo.png',
               'Content-Length': str(len(data))}
    assert request(mobile, 'POST', '/api/upload', data, headers)[0] == 201
    assert request(mobile, 'POST', '/api/upload', data, headers)[0] == 200
    files = list(mobile.inbox.rglob('*.png'))
    assert len(files) == 1 and files[0].read_bytes() == data
    status, body, _ = request(mobile, 'GET', '/api/photos', headers=headers)
    assert status == 200 and len(json.loads(body)['photos']) == 1
    assert '../' not in files[0].name


def test_reject_unauthorized_upload(mobile):
    status, _, _ = request(mobile, 'POST', '/api/upload', b'abc', {'X-Filename': 'a.jpg'})
    assert status == 401
    assert not list(mobile.inbox.rglob('*.jpg'))


def test_reject_unsafe_names_and_remote_binding(tmp_path):
    assert _safe_name('..\\hidden\\holiday.heic') == 'holiday.heic'
    with pytest.raises(ValueError):
        _safe_name('data.exe')
    with pytest.raises(ValueError):
        MobileServer(PhotoCatalog(tmp_path / 'state'), inbox=tmp_path / 'inbox', host='0.0.0.0')


@pytest.fixture
def library(mobile, tmp_path):
    from test_live_photos import movie, still
    root = tmp_path / 'library'
    root.mkdir()
    still(root / 'IMG_1.HEIC')
    movie(root / 'IMG_1.MOV', True)          # Live Photo pair
    movie(root / 'IMG_2.MOV', False)         # ordinary video
    for n in range(70):
        still(root / f'P{n:03}.JPG')
    mobile.catalog.add_folder(str(root))
    mobile.catalog.scan()
    return root


def pages(mobile):
    headers = {'Authorization': 'Bearer ' + mobile.token}
    items, offset = [], 0
    while offset is not None:
        status, body, _ = request(mobile, 'GET', f'/api/photos?offset={offset}', headers=headers)
        assert status == 200
        data = json.loads(body)
        items += data['photos']
        offset = data['next_offset']
    return items


def test_library_pages_through_everything_with_signed_media_links(mobile, library):
    items = pages(mobile)
    assert len(items) == 72 and len({p['id'] for p in items}) == 72
    assert 'IMG_1.MOV' not in {p['name'] for p in items}
    live = next(p for p in items if p['name'] == 'IMG_1.HEIC')
    video = next(p for p in items if p['name'] == 'IMG_2.MOV')
    assert live['motion'] and not live['video'] and video['video'] and not video['motion']
    # Media links work without the Authorization header, which <img> and <video> cannot send.
    status, body, headers = request(mobile, 'GET', live['original'])
    assert status == 200 and body == (library / 'IMG_1.HEIC').read_bytes()
    assert headers['Content-Type'] == 'image/heic'
    status, body, headers = request(mobile, 'GET', live['motion'])
    assert status == 200 and body == (library / 'IMG_1.MOV').read_bytes()
    assert headers['Content-Type'] == 'video/quicktime'
    status, body, headers = request(mobile, 'GET', live['thumb'])
    assert status == 200 and headers['Content-Type'] == 'image/jpeg' and body[:2] == b'\xff\xd8'
    status, _, headers = request(mobile, 'GET', live['original'] + '&download=1')
    assert status == 200 and headers['Content-Disposition'] == "attachment; filename*=UTF-8''IMG_1.HEIC"


def test_media_links_are_signed_per_file_and_part(mobile, library):
    items = pages(mobile)
    first, second = items[0], items[1]
    forged = first['original'].replace(f"id={first['id']}", f"id={second['id']}")
    assert request(mobile, 'GET', forged)[0] == 403
    live = next(p for p in items if p['motion'])
    assert request(mobile, 'GET', live['original'].replace('part=original', 'part=motion'))[0] == 403
    assert request(mobile, 'GET', '/api/media?id=1&part=original')[0] == 400
    # A file moved out of the library is not served even with a valid link.
    with mobile.catalog.connection() as db:
        db.execute('UPDATE photos SET path=? WHERE id=?', (str(mobile.inbox.parent / 'outside.jpg'), first['id']))
    (mobile.inbox.parent / 'outside.jpg').write_bytes(b'secret')
    assert request(mobile, 'GET', first['original'])[0] == 404


def test_video_byte_ranges(mobile, library):
    video = next(p for p in pages(mobile) if p['name'] == 'IMG_2.MOV')
    data = (library / 'IMG_2.MOV').read_bytes()
    status, body, headers = request(mobile, 'GET', video['original'], headers={'Range': 'bytes=0-1'})
    assert status == 206 and body == data[:2] and headers['Content-Range'] == f'bytes 0-1/{len(data)}'
    status, body, _ = request(mobile, 'GET', video['original'], headers={'Range': 'bytes=10-'})
    assert status == 206 and body == data[10:]
    status, body, _ = request(mobile, 'GET', video['original'], headers={'Range': 'bytes=-4'})
    assert status == 206 and body == data[-4:]
    status, _, headers = request(mobile, 'GET', video['original'], headers={'Range': f'bytes={len(data)}-'})
    assert status == 416 and headers['Content-Range'] == f'bytes */{len(data)}'


def test_upload_accepts_videos(mobile):
    assert _safe_name('IMG_0001.MOV') == 'IMG_0001.mov'
    assert _safe_name('clip.mp4') == 'clip.mp4'
