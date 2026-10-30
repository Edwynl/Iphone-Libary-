from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

import pytest
from ai_photos.receiver_agent import ReceiverController, _connect, _protect


def http(port, method, path, *, token, body=b'', **headers):
    client = HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        client.request(method, path, body, {'Authorization': 'Bearer ' + token, **headers})
        reply = client.getresponse()
        data = reply.read()
        return reply.status, json.loads(data) if 'application/json' in reply.getheader('Content-Type', '') else data
    finally:
        client.close()


def worker(base):
    engine = Path(__file__).resolve().parents[1] / 'engine'
    return subprocess.Popen([sys.executable, '-u', '-m', 'ai_photos'], cwd=engine,
        env=dict(os.environ, AI_PHOTOS_HOME=str(base / 'state'), AI_PHOTOS_INBOX=str(base / 'imports'),
                 PYTHONIOENCODING='utf-8'),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')


def call(process, ident, action, **params):
    process.stdin.write(json.dumps({'id': ident, 'action': action, 'params': params}) + '\n')
    process.stdin.flush()
    reply = json.loads(process.stdout.readline())
    assert reply['id'] == ident and reply['ok'], reply
    return reply['data']


def close_worker(process):
    if process.poll() is None:
        process.stdin.close()
        process.wait(timeout=10)
    process.stdout.close()
    process.stderr.close()


def test_receiver_survives_worker_exit_and_reconnects_with_durable_offsets(tmp_path):
    controller = ReceiverController(tmp_path / 'state')
    first, second = None, None
    try:
        first = worker(tmp_path)
        assert not call(first, 1, 'sync_dashboard')['server_running']
        server = call(first, 2, 'start_mobile', port=0)
        port = server['port']
        status, paired = http(port, 'POST', '/api/sync/v1/pair', token=server['pairing_token'],
            body=json.dumps({'device_id': 'agent-test-device', 'name': 'Synthetic test'}).encode())
        assert status == 201
        token = paired['device_token']
        data = b'original-resumable-video'
        status, transfer = http(port, 'POST', '/api/sync/v1/transfers', token=token,
            body=json.dumps({'asset_id': 'a', 'resource_type': 'video', 'filename': 'A.MOV',
                             'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}).encode())
        assert status == 200
        path = '/api/sync/v1/transfers/' + transfer['transfer_id']
        assert http(port, 'PUT', path + '/chunk', token=token, body=data[:5],
                    **{'X-Upload-Offset': '0', 'X-Chunk-SHA256': hashlib.sha256(data[:5]).hexdigest()})[0] == 200
        close_worker(first)
        assert controller.status()['running']
        assert http(port, 'GET', path, token=token)[1]['offset'] == 5
        second = worker(tmp_path)
        dashboard = call(second, 3, 'sync_dashboard')
        assert dashboard['server_running'] and dashboard['server_port'] == port
        assert dashboard['receiver_pid'] == server['pid']
        call(second, 4, 'sync_pause', paused=True)
        assert http(port, 'PUT', path + '/chunk', token=token, body=data[5:],
                    **{'X-Upload-Offset': '5', 'X-Chunk-SHA256': hashlib.sha256(data[5:]).hexdigest()})[0] == 423
        call(second, 5, 'sync_pause', paused=False)
        assert http(port, 'PUT', path + '/chunk', token=token, body=data[5:],
                    **{'X-Upload-Offset': '5', 'X-Chunk-SHA256': hashlib.sha256(data[5:]).hexdigest()})[0] == 200
        assert http(port, 'POST', path + '/complete', token=token)[0] == 200
        assert next((tmp_path / 'imports').rglob('*.mov')).read_bytes() == data
        call(second, 6, 'sync_revoke_device', device_id='agent-test-device')
        assert http(port, 'GET', path, token=token)[0] == 401
        assert next((tmp_path / 'imports').rglob('*.mov')).read_bytes() == data
        assert call(second, 7, 'stop_mobile')['stopped']
        assert not controller.status()['running']
        assert not (controller.directory / 'state.json').exists()
    finally:
        if first:
            close_worker(first)
        if second:
            close_worker(second)
        controller.stop()


def test_control_rejects_invalid_auth_and_malformed_json_without_stopping_agent(tmp_path):
    controller = ReceiverController(tmp_path / 'state')
    try:
        started = controller.start(port=0, inbox=tmp_path / 'imports')
        state = controller._state()
        assert started['pairing_token'] not in json.dumps(state)
        if os.name == 'nt':
            import base64
            decoded = base64.b64decode(state['control_key'])
            assert not decoded.decode('ascii', errors='ignore').isascii() or len(decoded) > 64
            assert len(_protect(decoded, decrypt=True)) >= 48
        for payload in [[], {'instance': state['instance'], 'token': 'wrong', 'action': 'stop'}]:
            with _connect(state['address']) as pipe:
                pipe.send_bytes(json.dumps(payload).encode())
                assert pipe.poll(5)
                assert not json.loads(pipe.recv_bytes(65536))['ok']
            assert controller.status()['running']
        # An unrelated local client can disconnect before sending a request.
        with _connect(state['address']):
            pass
        assert controller.status()['running']
        again = controller.start(port=0, inbox=tmp_path / 'imports')
        assert again['pid'] == started['pid']
        with pytest.raises(ValueError, match='different import folder'):
            controller.start(port=0, inbox=tmp_path / 'other-imports')
        assert controller.status()['pid'] == started['pid']
    finally:
        controller.stop()


def test_receiver_default_off_and_protected_ports_never_start(tmp_path):
    controller = ReceiverController(tmp_path / 'state')
    assert controller.status() == {'running': False}
    assert not controller.directory.exists()
    for port in [8765, 8766, -1, 65536]:
        with pytest.raises(ValueError):
            controller.start(port=port, inbox=tmp_path / 'imports')
    assert not controller.directory.exists()


def test_receiver_survives_abrupt_stdio_parent_termination(tmp_path):
    controller = ReceiverController(tmp_path / 'state')
    process = worker(tmp_path)
    try:
        started = call(process, 1, 'start_mobile', port=0)
        process.kill()  # WinUI Dispose stops only its worker, not the detached receiver.
        process.wait(timeout=10)
        assert controller.status()['pid'] == started['pid']
        assert http(started['port'], 'GET', '/', token='')[0] == 200
    finally:
        close_worker(process)
        controller.stop()


def test_agent_restart_preserves_device_and_durable_transfer(tmp_path):
    controller = ReceiverController(tmp_path / 'state')
    try:
        started = controller.start(port=0, inbox=tmp_path / 'imports')
        port = started['port']
        _, paired = http(port, 'POST', '/api/sync/v1/pair', token=started['pairing_token'],
                         body=json.dumps({'device_id': 'restart-test-device', 'name': 'Synthetic'}).encode())
        token = paired['device_token']
        data = b'synthetic-original-survives-agent-restart'
        _, transfer = http(port, 'POST', '/api/sync/v1/transfers', token=token,
            body=json.dumps({'asset_id': 'restart-asset', 'resource_type': 'video', 'filename': 'R.MOV',
                             'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}).encode())
        path = '/api/sync/v1/transfers/' + transfer['transfer_id']
        assert http(port, 'PUT', path + '/chunk', token=token, body=data[:8],
                    **{'X-Upload-Offset': '0', 'X-Chunk-SHA256': hashlib.sha256(data[:8]).hexdigest()})[0] == 200
        controller.stop()
        restarted = controller.start(port=0, inbox=tmp_path / 'imports')
        port = restarted['port']
        assert restarted['pid'] != started['pid']
        assert http(port, 'GET', path, token=token)[1]['offset'] == 8
        assert http(port, 'PUT', path + '/chunk', token=token, body=data[8:],
                    **{'X-Upload-Offset': '8', 'X-Chunk-SHA256': hashlib.sha256(data[8:]).hexdigest()})[0] == 200
        assert http(port, 'POST', path + '/complete', token=token)[0] == 200
        assert next((tmp_path / 'imports').rglob('*.mov')).read_bytes() == data
    finally:
        controller.stop()


def test_concurrent_starters_attach_to_one_receiver(tmp_path):
    controllers = [ReceiverController(tmp_path / 'state') for _ in range(2)]
    try:
        with ThreadPoolExecutor(2) as executor:
            starts = list(executor.map(lambda c: c.start(port=0, inbox=tmp_path / 'imports'), controllers))
        assert starts[0]['pid'] == starts[1]['pid']
        assert starts[0]['port'] == starts[1]['port']
    finally:
        controllers[0].stop()
