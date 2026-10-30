"""Protocol contract tests for iOS background-upload client and Windows sync server."""
from __future__ import annotations

import hashlib
import http.client
import json
import threading
from pathlib import Path

import pytest

from ai_photos.catalog import PhotoCatalog
from ai_photos.mobile import MobileServer
from ai_photos.sync import CHUNK_BYTES, MAX_RESOURCE_BYTES


@pytest.fixture
def native(tmp_path):
    cat = PhotoCatalog(tmp_path / 'state')
    server = MobileServer(cat, inbox=tmp_path / 'imports', token='master-token-' + 'z' * 40, port=0)
    server.start()
    try:
        yield server
    finally:
        server.close()


def req(server, method, path, obj=None, *, token=None, body=None, offset=None, digest=None):
    if obj is not None:
        body = json.dumps(obj).encode('utf-8')
    body = body if body is not None else b''
    headers = {'Content-Length': str(len(body))}
    if obj is not None:
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if offset is not None:
        headers['X-Upload-Offset'] = str(offset)
    if digest is not None:
        headers['X-Chunk-SHA256'] = digest
    conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, json.loads(raw)


def pair(server, device='device-01234567890', name='Edwyn iPhone'):
    status, result = req(server, 'POST', '/api/sync/v1/pair',
                         {'device_id': device, 'name': name}, token=server.token)
    assert status == 201
    return result['device_token']


def begin(server, token, content, *, asset='asset/001', filename='IMG_1001.HEIC', kind='photo'):
    request = {'asset_id': asset, 'filename': filename, 'resource_type': kind,
               'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    status, result = req(server, 'POST', '/api/sync/v1/transfers', request, token=token)
    assert status == 200
    return result['transfer_id'], request


def put(server, token, tid, content, offset):
    return req(server, 'PUT', '/api/sync/v1/transfers/' + tid + '/chunk',
               token=token, body=content, offset=offset, digest=hashlib.sha256(content).hexdigest())


def test_full_lifecycle_heic_including_resume_and_byte_integrity(native):
    token = pair(native)
    data = b'heic-original-content-for-test' * 100
    tid, request = begin(native, token, data)
    assert put(native, token, tid, data[:90], 0) == (200, {'transfer_id': tid, 'offset': 90, 'size': len(data), 'status': 'pending'})
    # Network failure / app restart: re-POST is idempotent and tells iOS the durable offset.
    status, same = req(native, 'POST', '/api/sync/v1/transfers', request, token=token)
    assert status == 200 and same['transfer_id'] == tid and same['offset'] == 90
    assert put(native, token, tid, data[90:], 90)[0] == 200
    assert req(native, 'POST', f'/api/sync/v1/transfers/{tid}/complete', token=token)[0] == 200
    assert req(native, 'POST', f'/api/sync/v1/transfers/{tid}/complete', token=token)[0] == 200
    assert len(list(native.inbox.rglob('*.heic'))) == 1
    assert next(native.inbox.rglob('*.heic')).read_bytes() == data
    assert native.catalog.stats()['photos'] == 1
    assert req(native, 'POST', '/api/sync/v1/transfers', request, token=token)[1]['status'] == 'complete'


def test_live_photo_is_two_original_resources_and_one_asset_group(native):
    token = pair(native)
    for kind, name, content in [('photo', 'IMG_31.HEIC', b'raw-heic'),
                                ('pairedVideo', 'IMG_31.MOV', b'original-live-video')]:
        tid, _ = begin(native, token, content, asset='same-live-asset', filename=name, kind=kind)
        assert put(native, token, tid, content, 0)[0] == 200
        assert req(native, 'POST', '/api/sync/v1/transfers/' + tid + '/complete', token=token)[0] == 200
    with native.catalog.connection() as db:
        rows = db.execute("SELECT asset_id,resource_type,stored_path FROM sync_transfers ORDER BY resource_type").fetchall()
    assert len(rows) == 2 and {r['resource_type'] for r in rows} == {'photo', 'pairedVideo'}
    assert {r['asset_id'] for r in rows} == {'same-live-asset'}
    assert all(Path(r['stored_path']).exists() for r in rows)


def test_wrong_offset_and_chunk_digest_do_not_corrupt(native):
    token = pair(native)
    tid, _ = begin(native, token, b'ABCDEF')
    assert put(native, token, tid, b'ABC', 0)[0] == 200
    status, value = put(native, token, tid, b'DEF', 0)
    assert status == 409 and value['expected_offset'] == 3
    status, _ = req(native, 'PUT', '/api/sync/v1/transfers/' + tid + '/chunk',
                    token=token, body=b'DEF', offset=3, digest='0' * 64)
    assert status == 422
    assert req(native, 'GET', '/api/sync/v1/transfers/' + tid, token=token)[1]['offset'] == 3
    assert put(native, token, tid, b'DEF', 3)[0] == 200


def test_authentication_revoke_and_pause(native):
    token_a = pair(native, 'device-AAAAAAAAAA')
    token_b = pair(native, 'device-BBBBBBBBBB')
    tid, _ = begin(native, token_a, b'abc')
    assert req(native, 'GET', '/api/sync/v1/transfers/' + tid, token=token_b)[0] == 404
    assert req(native, 'GET', '/api/sync/v1/transfers/' + tid)[0] == 401
    native.sync_manager.set_paused(True)
    assert put(native, token_a, tid, b'abc', 0)[0] == 423
    native.sync_manager.set_paused(False)
    assert put(native, token_a, tid, b'abc', 0)[0] == 200
    native.sync_manager.revoke_device('device-AAAAAAAAAA')
    assert req(native, 'GET', '/api/sync/v1/status', token=token_a)[0] == 401
    assert req(native, 'GET', '/api/sync/v1/status', token=token_b)[0] == 200
    dashboard = native.sync_manager.dashboard()
    assert dashboard['totals']['resources'] == 1
    assert len(dashboard['devices']) == 2


def test_name_and_size_checks_prevent_traversal_and_large_chunks(native):
    token = pair(native)
    payload = b'content'
    bad = {'asset_id': 'x', 'filename': '../private.heic', 'resource_type': 'photo',
           'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    assert req(native, 'POST', '/api/sync/v1/transfers', bad, token=token)[0] == 400
    bad['filename'] = 'private.exe'
    assert req(native, 'POST', '/api/sync/v1/transfers', bad, token=token)[0] == 400
    bad['filename'] = 'private.HEIC'
    bad['size'] = MAX_RESOURCE_BYTES + 1
    assert req(native, 'POST', '/api/sync/v1/transfers', bad, token=token)[0] == 400
    bad['size'] = len(payload)
    bad['sha256'] = 'invalid'
    assert req(native, 'POST', '/api/sync/v1/transfers', bad, token=token)[0] == 400
    assert not list(native.inbox.rglob('*.heic'))


def test_pairing_secret_required_and_old_device_token_rotated(native):
    assert req(native, 'POST', '/api/sync/v1/pair',
               {'device_id': 'device-XXXXXXXXXX', 'name': 'a'})[0] == 401
    previous = pair(native, 'device-XXXXXXXXXX')
    current = pair(native, 'device-XXXXXXXXXX')
    assert current != previous
    assert req(native, 'GET', '/api/sync/v1/status', token=previous)[0] == 401
    assert req(native, 'GET', '/api/sync/v1/status', token=current)[0] == 200


def test_restart_recovers_durable_offset_after_uncommitted_file_bytes(native):
    token = pair(native)
    content = b'123456789'
    tid, _ = begin(native, token, content)
    assert put(native, token, tid, content[:3], 0)[0] == 200
    staged = native.sync_manager.staging / (tid + '.part')
    with staged.open('ab') as out:
        out.write(b'corrupt-unacknowledged-tail')
    assert put(native, token, tid, content[3:], 3)[0] == 200
    assert staged.read_bytes() == content


def test_cross_device_replayed_transfer_rejected(native):
    token_a = pair(native, 'device-AAAAAAAAAA')
    token_b = pair(native, 'device-BBBBBBBBBB')
    tid, _ = begin(native, token_a, b'ab')
    assert put(native, token_b, tid, b'ab', 0)[0] == 404
    assert req(native, 'POST', f'/api/sync/v1/transfers/{tid}/complete', token=token_b)[0] == 404


def test_incomplete_transfer_cannot_be_marked_complete(native):
    token = pair(native)
    tid, _ = begin(native, token, b'abcd')
    assert put(native, token, tid, b'ab', 0)[0] == 200
    status, reply = req(native, 'POST', f'/api/sync/v1/transfers/{tid}/complete', token=token)
    assert status == 409 and reply['expected_offset'] == 2


def test_full_eight_megabyte_video_chunk(native):
    token = pair(native)
    content = b'V' * CHUNK_BYTES + b'final-tail'
    tid, _ = begin(native, token, content, kind='video', filename='IMG_42.MOV')
    assert put(native, token, tid, content[:CHUNK_BYTES], 0)[1]['offset'] == CHUNK_BYTES
    assert put(native, token, tid, content[CHUNK_BYTES:], CHUNK_BYTES)[0] == 200
    assert req(native, 'POST', f'/api/sync/v1/transfers/{tid}/complete', token=token)[0] == 200
    assert next(native.inbox.rglob('*.mov')).stat().st_size == len(content)


def test_corrupt_staging_blocks_completion_and_restarts_cleanly(native):
    token = pair(native)
    content = b'authentic-photo'
    tid, original_request = begin(native, token, content)
    assert put(native, token, tid, content, 0)[0] == 200
    stage = native.sync_manager.staging / (tid + '.part')
    stage.write_bytes(b'altered-contents')
    status, _ = req(native, 'POST', f'/api/sync/v1/transfers/{tid}/complete', token=token)
    assert status == 422
    with native.catalog.connection() as db:
        assert db.execute('SELECT status FROM sync_transfers WHERE id=?', (tid,)).fetchone()[0] == 'failed'
    assert not stage.exists()
    status, new = req(native, 'POST', '/api/sync/v1/transfers', original_request, token=token)
    assert status == 200 and new['offset'] == 0 and new['transfer_id'] != tid


def test_restart_keeps_pairing_and_confirmed_offset(tmp_path):
    catalog = PhotoCatalog(tmp_path / 'catalog')
    inbox = tmp_path / 'imports'
    first = MobileServer(catalog, inbox=inbox, token='m' * 40, port=0)
    first.start()
    token = pair(first)
    tid, request = begin(first, token, b'HELLO-RESUME')
    assert put(first, token, tid, b'HELLO-', 0)[0] == 200
    first.close()
    second = MobileServer(catalog, inbox=inbox, token='new-master-' + 'n' * 40, port=0)
    second.start()
    try:
        assert req(second, 'GET', f'/api/sync/v1/transfers/{tid}', token=token)[1]['offset'] == 6
        assert req(second, 'POST', '/api/sync/v1/transfers', request, token=token)[1]['offset'] == 6
        assert put(second, token, tid, b'RESUME', 6)[0] == 200
        assert req(second, 'POST', f'/api/sync/v1/transfers/{tid}/complete', token=token)[0] == 200
        assert next(inbox.rglob('*.heic')).read_bytes() == b'HELLO-RESUME'
    finally:
        second.close()


def test_dng_proraw_original_is_preserved(native):
    token = pair(native)
    data = b'original-dng-payload'
    tid, _ = begin(native, token, data, filename='IMG_002.DNG', kind='photo')
    assert put(native, token, tid, data, 0)[0] == 200
    assert req(native, 'POST', f'/api/sync/v1/transfers/{tid}/complete', token=token)[0] == 200
    assert next(native.inbox.rglob('*.dng')).read_bytes() == data


def test_missing_partial_file_is_not_resumed_with_zero_filled_data(native):
    token = pair(native)
    content = b'something longer than one chunk'
    tid, data = begin(native, token, content)
    assert put(native, token, tid, content[:5], 0)[0] == 200
    (native.sync_manager.staging / (tid + '.part')).unlink()
    new = req(native, 'POST', '/api/sync/v1/transfers', data, token=token)[1]
    assert new['offset'] == 0 and new['transfer_id'] != tid
    assert not list(native.inbox.rglob('*.heic'))
