"""Exercise the shipped worker and web assets, with an isolated state directory."""
import argparse
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('engine', type=Path)
parser.add_argument('--state-dir', type=Path)
args = parser.parse_args()
engine = args.engine.resolve(strict=True)
for name in ('index.html', 'app.js', 'app.css'):
    assert (engine / 'ai_photos/mobile_web' / name).is_file(), name
with tempfile.TemporaryDirectory(dir=args.state_dir) as state:
    env = dict(os.environ, AI_PHOTOS_HOME=state, PYTHONPATH=str(engine), PYTHONIOENCODING='utf-8')
    worker = subprocess.Popen([sys.executable, '-u', '-m', 'ai_photos'], cwd=engine, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        def request(ident, action, **params):
            worker.stdin.write(json.dumps({'id': ident, 'action': action, 'params': params}) + '\n')
            worker.stdin.flush()
            result = json.loads(worker.stdout.readline())
            assert result['id'] == ident and result['ok'], result
            return result['data']
        assert request(1, 'ping')['pong']
        assert request(2, 'status')['photos'] == 0
        server = request(3, 'start_mobile', port=0)
        for path in ('/', '/app.js', '/app.css'):
            connection = http.client.HTTPConnection('127.0.0.1', server['port'], timeout=10)
            connection.request('GET', path)
            response = connection.getresponse()
            assert response.status == 200 and response.read(), path
            connection.close()
        assert request(4, 'stop_mobile')['stopped']
        print(json.dumps({'worker_ping': True, 'isolated_empty_catalog': True, 'web_resources_http_200': 3}))
    finally:
        worker.stdin.close()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=10)
