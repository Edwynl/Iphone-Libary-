"""Explicit, detached receiver for the current login session (no auto-start).

Local management uses authenticated JSON over a remote-rejecting Windows pipe,
or a private Unix socket in development. No management endpoint is proxied by
Tailscale. The media listener remains loopback-only.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from multiprocessing import connection
from pathlib import Path

from .catalog import PhotoCatalog
from .file_safety import SafeDirectory, check_path, open_source
from .mobile import MobileServer, default_inbox


class ReceiverBusy(RuntimeError):
    pass


if os.name == 'nt':
    import _winapi
    from ctypes import wintypes

    from .file_safety import _kernel

    class _Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.c_void_p)]

    _crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    _crypt.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _kernel.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel.LocalFree.restype = ctypes.c_void_p

    class _LocalPipeListener(connection.PipeListener):
        def _new_handle(self, first=False):
            flags = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
            if first:
                flags |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
            return _winapi.CreateNamedPipe(self._address, flags,
                _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | 0x8,  # REJECT_REMOTE_CLIENTS
                _winapi.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, _winapi.NULL)


def _protect(data: bytes, *, decrypt=False) -> bytes:
    if os.name != 'nt':
        return data  # The state file is owner-only on POSIX.
    source = ctypes.create_string_buffer(data)
    incoming = _Blob(len(data), ctypes.cast(source, ctypes.c_void_p))
    outgoing = _Blob()
    call = _crypt.CryptUnprotectData if decrypt else _crypt.CryptProtectData
    if not call(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        _kernel.LocalFree(outgoing.data)


STARTUP_SECONDS = 15
CONTROL_WAIT_SECONDS = 20  # Covers a starter's spawn/readiness handshake.


@contextmanager
def receiver_lease(home, name='agent.lock', *, wait=0.0):
    """One agent per data directory; never kill a process by an untrusted PID."""
    deadline = time.monotonic() + wait
    with ExitStack() as stack:
        while True:  # Retry acquisition only, never the guarded body.
            try:
                stack.enter_context(_lease(home, name))
                break
            except ReceiverBusy:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(0.05)
        yield


def control_lease(home):
    # Held by start() from spawn until readiness and by stop() throughout, so
    # a stop can never report success while a spawned child is still pending.
    return receiver_lease(home, 'control.lock', wait=CONTROL_WAIT_SECONDS)


@contextmanager
def _lease(home, name):
    with SafeDirectory(Path(home) / 'receiver') as directory:
        path = directory.path / name
        if os.name == 'nt':
            handle = _kernel.CreateFileW(str(path), 0x80000000, 0, None, 4, 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ReceiverBusy('Receiver is already running or starting')
            try:
                attributes = (wintypes.DWORD * 2)()
                if not _kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)) or attributes[0] & 0x400:
                    raise ValueError('Unsafe receiver lock path')
                yield
            finally:
                _kernel.CloseHandle(handle)
        else:
            import fcntl
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ReceiverBusy('Receiver is already running or starting') from exc
                yield
            finally:
                os.close(descriptor)


def _connect(address):
    if os.name != 'nt':
        # Socket creation/connect has a bounded wait before Connection framing.
        import socket
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.settimeout(0.5)
            sock.connect(address)
            sock.settimeout(None)
            return connection.Connection(sock.detach())
        finally:
            sock.close()
    _winapi.WaitNamedPipe(address, 500)
    handle = _winapi.CreateFile(address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
        0, _winapi.NULL, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
    try:
        _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
        return connection.PipeConnection(handle)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise


class ReceiverController:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.directory = self.home / 'receiver'
        self._child = None

    def _state(self):
        path = check_path(self.directory / 'state.json')
        if os.name != 'nt' and path.stat().st_mode & 0o077:
            raise ValueError('Receiver control state must be owner-only')
        with open_source(path) as stream:
            state = json.load(stream)
        # A controller never connects to TCP or a remote machine from state data.
        address = state['address']
        if os.name == 'nt':
            if not address.startswith('\\\\.\\pipe\\WindowsAIPhotos-'):
                raise ValueError('Invalid local receiver pipe')
        elif not Path(address).is_absolute():
            raise ValueError('Invalid receiver socket path')
        return state

    def request(self, action, **params):
        state = self._state()
        token = _protect(base64.b64decode(state['control_key']), decrypt=True).decode('ascii')
        payload = json.dumps({'token': token, 'instance': state['instance'], 'action': action, 'params': params}).encode()
        with _connect(state['address']) as channel:
            channel.send_bytes(payload)
            if not channel.poll(5):
                raise TimeoutError('Receiver control timed out')
            response = json.loads(channel.recv_bytes(65536))
        if response.get('instance') != state['instance']:
            raise ValueError('Receiver identity changed; reconnect')
        if not response.get('ok'):
            raise RuntimeError(response.get('error', 'Receiver control failed'))
        return response['data']

    def status(self):
        try:
            return self.request('status')
        except FileNotFoundError:
            return {'running': False}
        except (OSError, ValueError, EOFError, KeyError) as exc:
            return {'running': False, 'error': str(exc)}

    def start(self, *, port=8787, inbox=None):
        if type(port) is not int or not 0 <= port <= 65535 or port in {8765, 8766}:
            raise ValueError('Use an independent receiver port (not 8765/8766)')
        with control_lease(self.home):
            return self._start(port, inbox)

    def _start(self, port, inbox):
        status = self.status()
        if status['running']:
            if port and status['port'] != port:
                raise ValueError('Receiver already uses a different port; stop it first')
            if inbox is not None and Path(status['inbox']) != Path(inbox).expanduser().resolve():
                raise ValueError('Receiver already uses a different import folder; stop it first')
            return self.request('pairing')
        instance = secrets.token_hex(16)
        arguments = [sys.executable, '-m', 'ai_photos.receiver_agent', '--home', str(self.home),
                     '--port', str(port), '--instance', instance,
                     '--inbox', str(Path(inbox or default_inbox()).expanduser().resolve())]
        common = dict(cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, env=dict(os.environ, AI_PHOTOS_HOME=str(self.home)))
        if os.name == 'nt':
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                child = subprocess.Popen(arguments, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
            except OSError as exc:
                # A host job that forbids breakaway may (directly or through an
                # enclosing job) kill its children on close, which cannot be
                # ruled out reliably, so never start a receiver that might
                # silently die with the window.
                if getattr(exc, 'winerror', None) != 5:
                    raise
                if os.environ.get('AI_PHOTOS_RECEIVER_JOB_BOUND') != '1':
                    raise RuntimeError('This app was launched inside a Windows job that does not allow the '
                                       'receiver to outlive it; start Windows AI Photos normally and retry') from exc
                # Explicit opt-in (CI, debugging hosts): the receiver may stop
                # with this host, and its status says so instead of implying it persists.
                child = subprocess.Popen(arguments + ['--job-bound'], creationflags=flags, **common)
        else:
            child = subprocess.Popen(arguments, start_new_session=True, **common)
        self._child = child
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            status = self.status()
            if status['running']:
                if (port and status['port'] != port) or (inbox is not None and Path(status['inbox']) != Path(inbox).expanduser().resolve()):
                    raise ValueError('Another receiver started with different settings')
                return self.request('pairing')
            if child.poll() is not None:
                if child.returncode == 2:  # Another starter owns the lease; wait for readiness.
                    time.sleep(0.05)
                    continue
                try:
                    with open_source(self.directory / 'last-error.json') as stream:
                        error = json.load(stream)
                    if error['instance'] == instance:
                        raise RuntimeError(error['error'])
                except (OSError, ValueError, KeyError):
                    pass
                raise RuntimeError('Receiver exited before startup; no existing service was replaced')
            time.sleep(0.05)
        # Still holding the control lease: end our own child (by its process
        # handle, never a PID from disk) so a later stop() cannot report
        # success while it might still come up.
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
            try:
                with receiver_lease(self.home):
                    with open_source(self.directory / 'state.json') as stream:
                        ours = json.load(stream).get('instance') == instance
                    if ours:
                        (self.directory / 'state.json').unlink()
            except (OSError, ValueError, ReceiverBusy):
                pass
        raise TimeoutError('Receiver did not become ready in time and was stopped; retry')

    def stop(self):
        with control_lease(self.home):
            return self._stop()

    def _stop(self):
        status = self.status()
        if not status['running']:
            try:
                # Holding the lease proves no agent runs; stale state from an
                # unclean exit (kill, crash, power loss) is safe to discard.
                with receiver_lease(self.home):
                    (self.directory / 'state.json').unlink(missing_ok=True)
            except ReceiverBusy as exc:
                if status.get('error'):
                    raise RuntimeError('Cannot confirm receiver state: ' + status['error']) from exc
                raise
            return {'stopped': True, 'stale_state_removed': bool(status.get('error'))}
        self.request('stop')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not (self.directory / 'state.json').exists():
                try:
                    with receiver_lease(self.home):
                        if self._child is not None:
                            self._child.wait(timeout=2)
                        return {'stopped': True}
                except ReceiverBusy:
                    pass
            time.sleep(0.05)
        raise TimeoutError('Receiver stop is still pending')

    def manage(self, manager, action, **params):
        if self.status()['running']:
            return self.request(action, **params)
        # Serialize offline staging mutations with an agent starting concurrently.
        with receiver_lease(self.home):
            return _manage(manager, action, params)


def _manage(manager, action, params):
    if action == 'pause':
        return manager.set_paused(bool(params['paused']))
    if action == 'revoke':
        return manager.revoke_device(params['device_id'])
    if action == 'cancel':
        return manager.cancel(params['device_id'], params['transfer_id'], owner=True)
    raise ValueError('Unknown receiver action')


def run_receiver(home, inbox, port, instance, job_bound=False):
    if port in {8765, 8766}:
        raise ValueError('Protected preview/service ports cannot host the receiver')
    home = Path(home).resolve()
    state_path = home / 'receiver/state.json'
    temporary = None
    with receiver_lease(home):
        catalog = PhotoCatalog(home)
        server = MobileServer(catalog, inbox=Path(inbox), port=port)
        secret = secrets.token_urlsafe(48)
        listener = None
        try:
            if os.name == 'nt':
                address = '\\\\.\\pipe\\WindowsAIPhotos-' + instance
                listener = _LocalPipeListener(address)
            else:
                temporary = tempfile.mkdtemp(prefix='wap-receiver-')
                address = str(Path(temporary) / 'control.sock')
                listener = connection.Listener(address, family='AF_UNIX')
                os.chmod(address, 0o600)
            state = {'instance': instance, 'pid': os.getpid(), 'address': address,
                     'control_key': base64.b64encode(_protect(secret.encode())).decode(), 'port': server.port}
            server.start()
            with SafeDirectory(home / 'receiver') as directory:
                directory.write('state.json', json.dumps(state).encode())
            stopped = False
            while not stopped:
                with listener.accept() as channel:
                    try:
                        if not channel.poll(2):
                            continue
                        message = json.loads(channel.recv_bytes(8192))
                        if not isinstance(message, dict):
                            raise ValueError('Receiver control request must be an object')
                        if message.get('instance') != instance or not secrets.compare_digest(message.get('token', ''), secret):
                            raise ValueError('Receiver control authentication required')
                        action = message.get('action')
                        status = {'running': True, 'pid': os.getpid(), 'port': server.port,
                                  'inbox': str(server.inbox), 'instance': instance, 'mode': 'background-agent',
                                  'job_bound': job_bound}
                        if action == 'status':
                            data = status
                        elif action == 'pairing':
                            data = dict(status, pairing_token=server.token,
                                url=f'http://127.0.0.1:{server.port}', tailscale_command=f'tailscale serve --bg {server.port}')
                        elif action == 'stop':
                            stopped, data = True, {'stopping': True}
                        else:
                            data = _manage(server.sync_manager, action, message.get('params') or {})
                        response = {'ok': True, 'instance': instance, 'data': data}
                    except Exception as exc:
                        # Any failed request (SyncFailure, sqlite3.Error, ...) is
                        # reported to its caller; it never stops the receiver.
                        response = {'ok': False, 'instance': instance, 'error': str(exc)}
                    try:
                        channel.send_bytes(json.dumps(response).encode())
                    except (OSError, EOFError):
                        pass
        finally:
            server.close()
            if listener:
                listener.close()
            if temporary:
                os.rmdir(temporary)  # Only our private, now-empty socket directory.
            try:
                with open_source(state_path) as stream:
                    owned = json.load(stream)['instance'] == instance
                if owned:
                    state_path.unlink()
            except (OSError, ValueError, KeyError):
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--inbox', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--job-bound', action='store_true')
    args = parser.parse_args()
    try:
        run_receiver(args.home, args.inbox, args.port, args.instance, args.job_bound)
    except ReceiverBusy:
        return 2
    except Exception as exc:
        with SafeDirectory(args.home / 'receiver') as directory:
            directory.write('last-error.json', json.dumps({'instance': args.instance, 'error': str(exc)}).encode())
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
