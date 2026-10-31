"""Exercise versioned backup/selected restore through actual shipped interfaces."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from uuid import uuid4

from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument('engine', type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
engine = args.engine.resolve(strict=True)
base = root / 'docs/review/next-snapshot-output-temp' / uuid4().hex
source = base / 'originals'
source.mkdir(parents=True)
photo = source / 'original.png'
Image.new('RGB', (12, 12), 'blue').save(photo)
old = photo.read_bytes()
env = dict(os.environ, AI_PHOTOS_HOME=str(base / 'home'), AI_PHOTOS_INBOX=str(base / 'imports'),
           PYTHONPATH=str(engine), PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
worker = subprocess.Popen([sys.executable, '-u', '-m', 'ai_photos'], cwd=engine, env=env,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
replies = queue.Queue()


def read_output():
    for line in worker.stdout:
        replies.put(json.loads(line))
    replies.put(None)


reader = threading.Thread(target=read_output, daemon=True)
reader.start()


def call(ident, action, **params):
    worker.stdin.write(json.dumps({'id': ident, 'action': action, 'params': params}) + '\n')
    worker.stdin.flush()
    while True:
        reply = replies.get(timeout=20)
        assert reply is not None, 'Shipped worker exited before replying'
        if 'event' in reply:
            continue
        assert reply['id'] == ident and reply['ok'], reply
        return reply['data']


def cli(*arguments):
    result = subprocess.run([sys.executable, '-m', 'ai_photos.cli', *map(str, arguments)], cwd=engine,
                            env=env, capture_output=True, text=True, check=True, timeout=20)
    return json.loads(result.stdout)


try:
    call(1, 'add_folder', path=str(source))
    call(2, 'scan')
    first = call(3, 'backup_local', target=str(base / 'backup'), apply=True)
    assert first['complete'] and first['latest_published']
    Image.new('RGB', (22, 22), 'red').save(photo)
    current = photo.read_bytes()
    call(4, 'scan')
    second = call(5, 'backup_local', target=str(base / 'backup'), apply=True)
    assert second['complete'] and second['snapshot_id'] != first['snapshot_id']
    history = cli('backup-history', base / 'backup')
    assert len(history['snapshots']) == 2
    cli('restore-local', base / 'backup', base / 'restore-old', '--snapshot', first['snapshot_id'], '--apply')
    cli('restore-local', base / 'backup', base / 'restore-new', '--apply')
    assert next((base / 'restore-old').rglob('*.png')).read_bytes() == old
    assert next((base / 'restore-new').rglob('*.png')).read_bytes() == current
    assert photo.read_bytes() == current
    print(json.dumps({'shipped_worker_backup_local_twice': True, 'shipped_cli_history_snapshots': 2,
                      'shipped_cli_restore_old_exact': True, 'shipped_cli_restore_latest_exact': True,
                      'source_unchanged_by_backup_or_restore': True}))
finally:
    worker.stdin.close()
    try:
        worker.wait(timeout=10)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=5)
    reader.join(timeout=5)
    worker.stdout.close()
