import http.client
import json
import socket
import threading
import time

from ai_photos.catalog import PhotoCatalog
from ai_photos.mobile import MobileServer


def test_saturated_connections_return_retryable_503_and_idle_socket_expires(tmp_path):
    server = MobileServer(PhotoCatalog(tmp_path / 'state'), inbox=tmp_path / 'imports',
                          port=0, max_connections=1, socket_timeout=1)
    server.start()
    first = socket.create_connection(('127.0.0.1', server.port), timeout=3)
    try:
        first.sendall(b'GET / HTTP/1.0\r\n')  # Incomplete header occupies the only slot.
        deadline = time.monotonic() + 2
        while server.httpd._slots.acquire(blocking=False):
            server.httpd._slots.release()
            assert time.monotonic() < deadline
            time.sleep(0.01)
        client = http.client.HTTPConnection('127.0.0.1', server.port, timeout=3)
        client.request('GET', '/')
        reply = client.getresponse()
        assert reply.status == 503 and json.loads(reply.read())['retryable']
        client.close()
        assert first.recv(1024) == b''  # No further data: inactivity timeout frees the slot.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            client = http.client.HTTPConnection('127.0.0.1', server.port, timeout=3)
            client.request('GET', '/')
            reply = client.getresponse()
            status = reply.status
            reply.read()
            client.close()
            if status == 200:
                break
            time.sleep(0.01)
        assert status == 200
    finally:
        first.close()
        server.close()


def test_unstarted_server_can_close(tmp_path):
    server = MobileServer(PhotoCatalog(tmp_path / 'state'), inbox=tmp_path / 'imports', port=0)
    server.close()  # Must not block on shutdown before serve_forever was started.


def test_stop_drains_admitted_request_before_returning(tmp_path):
    server = MobileServer(PhotoCatalog(tmp_path / 'state'), inbox=tmp_path / 'imports',
                          port=0, socket_timeout=3)
    server.start()
    body = json.dumps({'device_id': 'drain-test-device', 'name': 'Synthetic'}).encode()
    client = socket.create_connection(('127.0.0.1', server.port), timeout=4)
    stopped = threading.Event()
    thread = None
    try:
        headers = (f'POST /api/sync/v1/pair HTTP/1.0\r\nAuthorization: Bearer {server.token}\r\n'
                   f'Content-Length: {len(body)}\r\n\r\n').encode()
        client.sendall(headers + body[:1])
        deadline = time.monotonic() + 2
        while server.httpd._slots._value == 16:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        thread = threading.Thread(target=lambda: (server.close(), stopped.set()))
        thread.start()
        assert not stopped.wait(0.6)
        client.sendall(body[1:])
        response = http.client.HTTPResponse(client)
        response.begin()
        assert response.status == 201
        response.read()
        assert stopped.wait(4)
    finally:
        client.close()
        if thread:
            thread.join(timeout=5)
        else:
            server.close()
