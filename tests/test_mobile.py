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
