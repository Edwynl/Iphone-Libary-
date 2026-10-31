"""Write only through pinned, non-reparse directories; never follow output links."""
from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import stat
from contextlib import ExitStack
from pathlib import Path


class ChecksumMismatch(OSError):
    pass


def is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def check_path(path: Path) -> Path:
    """Do not resolve first: that would hide a junction supplied as the target."""
    path = Path(os.path.abspath(Path(path).expanduser()))
    for parent in [*reversed(path.parents), path]:
        if is_reparse(parent):
            raise ValueError(f"Refusing linked/reparse path: {parent}")
    return path


if os.name == "nt":
    from ctypes import wintypes
    import msvcrt

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
    _kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    def _final_path(handle):
        buffer = ctypes.create_unicode_buffer(32768)
        length = _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value)

    def _win_handle(path: Path, directory: bool):
        # Denying DELETE sharing prevents replacing ancestors. A pinned lease
        # below also keeps the leaf nonempty, preventing junction attachment.
        # WRITE sharing is needed for Windows' own file rename operations.
        # READ_ATTRIBUTES alone does not participate in Windows sharing checks.
        # FILE_LIST_DIRECTORY is needed to enforce the no-DELETE lease.
        handle = _kernel.CreateFileW(str(path), 0x81 if directory else 0x80000000,
                                     3 if directory else 1, None, 3, 0x00200000 | (0x02000000 if directory else 0), None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = (wintypes.DWORD * 2)()
        if not _kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
            error = ctypes.WinError(ctypes.get_last_error())
            _kernel.CloseHandle(handle)
            raise error
        if attributes[0] & 0x400 or bool(attributes[0] & 0x10) != directory:
            _kernel.CloseHandle(handle)
            raise ValueError(f"Refusing linked or unexpected output type: {path}")
        return handle


def open_source(path):
    """Open the selected source without following a replaced link or junction."""
    path = check_path(Path(path))
    if os.name == 'nt':
        handle = _win_handle(path, False)
        try:
            actual = str(_final_path(handle))
            actual = ('\\\\' + actual[8:]) if actual.startswith('\\\\?\\UNC\\') else actual.removeprefix('\\\\?\\')
            if os.path.normcase(actual) != os.path.normcase(str(path)):
                raise ValueError('Source path changed while opening it')
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _kernel.CloseHandle(handle)
            raise
        return os.fdopen(fd, 'rb')
    with ExitStack() as stack:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, directory)
        for name in path.parts[1:-1]:
            directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    return os.fdopen(fd, 'rb')


class SafeDirectory:
    """Pin every Windows ancestor, or use relative directory fds on POSIX."""

    def __init__(self, path: Path):
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self._stack = ExitStack()
        self.fd = None

    def __enter__(self):
        try:
            chain = [*reversed(self.path.parents), self.path]
            if os.name == "nt":
                for directory in chain:
                    # Never mkdir an existing path: a drive root raises WinError 5.
                    if not directory.exists():
                        try:
                            directory.mkdir()
                        except FileExistsError:
                            pass  # A concurrent creator; the handle below validates it.
                    handle = _win_handle(directory, True)
                    self._stack.callback(_kernel.CloseHandle, handle)
                # A normal directory must remain nonempty to prevent SET_REPARSE
                # on its existing handle. Deny deletion of this private lease.
                lease = self.path / ('.lease-' + secrets.token_hex(16))
                lease_handle = _kernel.CreateFileW(str(lease), 0xC0010000, 1, None, 1,
                                                   0x04000100 | 0x00200000, None)
                if lease_handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._stack.callback(_kernel.CloseHandle, lease_handle)  # DELETE_ON_CLOSE
                attributes = (wintypes.DWORD * 2)()
                if not _kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if attributes[0] & 0x400 or _final_path(lease_handle).parent != _final_path(handle):
                    raise ValueError('Output directory changed while acquiring its lease')
            else:
                fd = os.open(self.path.anchor, os.O_RDONLY | os.O_DIRECTORY)
                self._stack.callback(os.close, fd)
                for name in self.path.parts[1:]:
                    try:
                        os.mkdir(name, dir_fd=fd)
                    except FileExistsError:
                        pass
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    self._stack.callback(os.close, fd)
                self.fd = fd
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, *args):
        self._stack.close()

    @staticmethod
    def _name(name):
        if not name or Path(name).name != name or name in {".", ".."} or ":" in name:
            raise ValueError("Output filename must be a single safe component")
        return name

    def open_read(self, name):
        name = self._name(name)
        if os.name == "nt":
            handle = _win_handle(self.path / name, False)
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                _kernel.CloseHandle(handle)
                raise
        else:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        return os.fdopen(fd, "rb")

    def digest(self, name):
        hashed = hashlib.sha256()
        with self.open_read(name) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hashed.update(block)
        return hashed.hexdigest()

    def _temporary(self):
        name = ".copy-" + secrets.token_hex(16)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        fd = os.open(self.path / name, flags, 0o600) if os.name == "nt" else os.open(
            name, flags | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        return name, os.fdopen(fd, "wb")

    def _unlink(self, name):
        if os.name == "nt":
            (self.path / name).unlink(missing_ok=True)
        else:
            os.unlink(name, dir_fd=self.fd)

    def _commit(self, temporary, name, replace):
        name = self._name(name)
        if os.name == "nt":
            if is_reparse(self.path / name):
                raise ValueError("Refusing linked output file")
            if replace:
                os.replace(self.path / temporary, self.path / name)
            else:
                # Windows rename fails atomically when the destination exists.
                # CreateHardLink unnecessarily reopens the leased directory for write.
                os.rename(self.path / temporary, self.path / name)
        elif replace:
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            self._unlink(temporary)
        if self.fd is not None:
            os.fsync(self.fd)

    def move_from(self, source, source_name, name):
        """Move a file from another pinned directory; never replaces an existing name."""
        source_name, name = self._name(source_name), self._name(name)
        if os.name == "nt":
            if is_reparse(self.path / name):
                raise ValueError("Refusing linked output file")
            os.rename(source.path / source_name, self.path / name)  # Fails if name exists.
        else:
            os.link(source_name, name, src_dir_fd=source.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            os.unlink(source_name, dir_fd=source.fd)
            os.fsync(self.fd)

    def copy(self, source, name, *, expected=None, replace=True):
        temporary, output = self._temporary()
        try:
            hashed = hashlib.sha256()
            with open_source(source) as stream, output:
                before = os.fstat(stream.fileno())
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    output.write(block)
                    hashed.update(block)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise OSError("Source changed while copying")
                output.flush()
                os.fsync(output.fileno())
            digest = hashed.hexdigest()
            if expected and digest != expected:
                raise ChecksumMismatch("Source checksum mismatch")
            try:
                self._commit(temporary, name, replace)
            except FileExistsError:
                if replace or self.digest(name) != digest:
                    raise
            return digest
        finally:
            output.close()
            try:
                self._unlink(temporary)
            except FileNotFoundError:
                pass

    def write(self, name, data, *, replace=True):
        temporary, output = self._temporary()
        try:
            with output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._commit(temporary, name, replace)
        finally:
            try:
                self._unlink(temporary)
            except FileNotFoundError:
                pass
