"""Crashes leave a trail on disk, and importing the engine writes no log files."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ai_photos.mobile import BoundedHTTPServer

ENGINE =Path(__file__).resolve().parents[1] / 'engine'


def run(code, home, *args):
    env = dict(os.environ, AI_PHOTOS_HOME=str(home), PYTHONPATH=str(ENGINE))
    return subprocess.run([sys.executable, '-c', code, *args], env=env, capture_output=True,
                          text=True, encoding='utf-8', timeout=120, check=False)


def test_setup_logging_records_crashes_in_any_thread(tmp_path):
    result = run("""
import logging, sys, threading
from pathlib import Path
from ai_photos.logs import setup_logging
setup_logging('worker', Path(sys.argv[1]))
logging.getLogger('quiet').info('an info line')
logging.getLogger('loud').warning('a warning line')
thread = threading.Thread(target=lambda: 1 / 0, name='job')
thread.start()
thread.join()
raise RuntimeError('main thread crash')
""", tmp_path, str(tmp_path))
    log = (tmp_path / 'logs' / 'worker.log').read_text(encoding='utf-8')
    assert 'an info line' in log and 'a warning line' in log
    assert 'ZeroDivisionError' in log and 'in thread job' in log
    assert 'RuntimeError: main thread crash' in log
    # Stderr is what the desktop app shows: warnings and crashes, without routine lines.
    assert 'an info line' not in result.stderr and 'a warning line' in result.stderr
    assert result.returncode != 0


def test_importing_the_engine_configures_no_logging(tmp_path):
    result = run("""
import logging
import ai_photos.worker, ai_photos.receiver_agent, ai_photos.mobile
print(logging.getLogger().handlers)
""", tmp_path)
    assert result.stdout.strip() == '[]', result.stderr
    assert not (tmp_path / 'logs').exists()


def test_a_receiver_that_fails_to_start_says_why_in_its_log(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ENGINE))
    # A protected port is refused before anything binds to it.
    result = subprocess.run([sys.executable, '-m', 'ai_photos.receiver_agent', '--home', str(tmp_path),
                             '--inbox', str(tmp_path / 'inbox'), '--port', '8765', '--instance', 'test'],
                            env=env, capture_output=True, text=True, encoding='utf-8', timeout=120, check=False)
    assert result.returncode == 1
    assert 'Protected preview/service ports' in (tmp_path / 'logs' / 'receiver.log').read_text(encoding='utf-8')


def test_a_failed_receiver_request_goes_to_the_log(caplog):
    try:
        raise ValueError('boom in a request')
    except ValueError:
        BoundedHTTPServer.handle_error(None, None, None)
    assert 'boom in a request' in caplog.text


def test_the_worker_entry_point_logs_under_app_home(tmp_path):
    env = dict(os.environ, AI_PHOTOS_HOME=str(tmp_path), PYTHONPATH=str(ENGINE))
    result = subprocess.run([sys.executable, '-m', 'ai_photos'], input='{"id":1,"action":"nope"}\n', env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=120, check=True)
    assert '"id": 1' in result.stdout  # Stdout stays pure JSON for the app.
    assert (tmp_path / 'logs').is_dir()
