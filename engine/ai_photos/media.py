"""Read-only media helpers: Live Photo identifiers, video capture dates and Windows shell poster frames."""
from __future__ import annotations

import os
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from .file_safety import check_path, open_source

VIDEO_EXTENSIONS = frozenset({".mov", ".mp4", ".m4v"})
# Apple writes this key into the motion half of every Live Photo; the still
# carries the same identifier (maker note tag 0x11). Ordinary iPhone videos do not have it.
LIVE_KEY = b"com.apple.quicktime.content.identifier"
_APPLE_MAKER_NOTE = b"Apple iOS\0"
_MAX_MOOV = 32 << 20
_EPOCH_1904 = datetime(1904, 1, 1, tzinfo=timezone.utc)


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def _read_moov(f, size: int) -> bytes | None:
    pos = 0
    while pos + 8 <= size:
        f.seek(pos)
        header = f.read(16)
        if len(header) < 8:
            return None
        length, kind = struct.unpack(">I4s", header[:8])
        head = 8
        if length == 1:
            if len(header) < 16:
                return None
            length, head = struct.unpack(">Q", header[8:16])[0], 16
        elif length == 0:
            length = size - pos
        if length < head:
            return None
        if kind == b"moov":
            if length > _MAX_MOOV:
                return None
            f.seek(pos)
            return f.read(length)
        pos += length
    return None


def _creation_time(moov: bytes) -> str | None:
    at = moov.find(b"mvhd")
    if at < 4:
        return None
    body = moov[at + 4:]
    try:
        if body[0] == 1:
            seconds = struct.unpack(">Q", body[4:12])[0]
        else:
            seconds = struct.unpack(">I", body[4:8])[0]
    except (IndexError, struct.error):
        return None
    if seconds == 0:
        return None
    try:
        return (_EPOCH_1904 + timedelta(seconds=seconds)).isoformat()
    except OverflowError:
        return None


def _atoms(data: bytes, start: int = 0, end: int | None = None):
    """(kind, body start, end) of each box between start and end."""
    end = len(data) if end is None else end
    while start + 8 <= end:
        length, kind = struct.unpack(">I4s", data[start:start + 8])
        head = 8
        if length == 1:
            if start + 16 > end:
                return
            length, head = struct.unpack(">Q", data[start + 8:start + 16])[0], 16
        elif length == 0:
            length = end - start
        if length < head or start + length > end:
            return
        yield kind, start + head, start + length
        start += length


def _content_identifier(moov: bytes) -> str | None:
    """The value stored under LIVE_KEY in moov/meta: a 'keys' list plus an 'ilst' of 1-based entries."""
    for kind, moov_start, moov_end in _atoms(moov):
        if kind != b"moov":
            continue
        for kind, start, end in _atoms(moov, moov_start, moov_end):
            if kind != b"meta":
                continue
            keys, values = [], {}
            for kind, body, stop in _atoms(moov, start, end):
                if kind == b"keys":
                    pos = body + 8  # Version/flags, entry count.
                    while pos + 8 <= stop:
                        length = struct.unpack(">I", moov[pos:pos + 4])[0]
                        if length < 8:
                            break
                        keys.append(moov[pos + 8:pos + length])
                        pos += length
                elif kind == b"ilst":
                    for index, item, item_end in _atoms(moov, body, stop):
                        for kind, data, data_end in _atoms(moov, item, item_end):
                            if kind == b"data":  # Type indicator and locale, then the value.
                                values[struct.unpack(">I", index)[0]] = moov[data + 8:data_end]
            if LIVE_KEY in keys:
                value = values.get(keys.index(LIVE_KEY) + 1)
                return value.decode("utf-8", "replace") if value else None
    return None


def probe_video(path: Path) -> tuple[bool | None, str | None, str | None]:
    """Return (has Live Photo key, UTC capture time, Live Photo content identifier) without decoding.

    The flag is None when the file cannot be read, so the caller can retry later.
    """
    try:
        with open_source(path) as f:
            f.seek(0, 2)
            moov = _read_moov(f, f.tell())
    except (OSError, ValueError):
        return None, None, None
    if moov is None:
        return False, None, None
    return LIVE_KEY in moov, _creation_time(moov), _content_identifier(moov)


def still_content_id(path: Path) -> str | None:
    """A still's Live Photo content identifier from its Apple maker note.

    '' when it has none; None when the file cannot be read now, so the caller can retry later.
    """
    try:
        with Image.open(path) as image:
            note = image.getexif().get_ifd(0x8769).get(0x927C)
    except (OSError, ValueError, SyntaxError):
        return None
    if not isinstance(note, bytes) or not note.startswith(_APPLE_MAKER_NOTE) or len(note) < 16:
        return ""
    # Header, version, byte order, then an IFD whose offsets count from the maker note start.
    order = "<" if note[12:14] == b"II" else ">"
    count = struct.unpack(order + "H", note[14:16])[0]
    for i in range(count):
        entry = note[16 + 12 * i:28 + 12 * i]
        if len(entry) < 12:
            break
        tag, kind, length, offset = struct.unpack(order + "HHII", entry)
        if tag == 0x11 and kind == 2:  # ASCII
            raw = entry[8:8 + length] if length <= 4 else note[offset:offset + length]
            return raw.split(b"\0")[0].decode("ascii", "replace")
    return ""


def video_thumbnail(path: Path, out: Path, size: int = 384) -> bool:
    """Save the Windows shell's thumbnail of a video (or RAW photo) as JPEG; False if none.

    Uses the same codecs as File Explorer (IShellItemImageFactory). Nothing is
    decoded in-process and the source is only read by the shell.
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("type", ctypes.c_long), ("width", ctypes.c_long), ("height", ctypes.c_long),
                    ("stride", ctypes.c_long), ("planes", wintypes.WORD), ("bits", wintypes.WORD),
                    ("data", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("width", ctypes.c_long), ("height", ctypes.c_long),
                    ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                    ("image_size", wintypes.DWORD), ("xppm", ctypes.c_long), ("yppm", ctypes.c_long),
                    ("used", wintypes.DWORD), ("important", wintypes.DWORD)]

    try:
        path = check_path(Path(path))
    except (OSError, ValueError):
        return False
    ole32, shell32 = ctypes.WinDLL("ole32"), ctypes.WinDLL("shell32")
    gdi32, user32 = ctypes.WinDLL("gdi32"), ctypes.WinDLL("user32")
    shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                     ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    iid = GUID()
    ole32.CLSIDFromString("{bcc18b79-ba16-442f-80c4-8a59c30c463b}", ctypes.byref(iid))  # IShellItemImageFactory
    initialized = ole32.CoInitializeEx(None, 2) in (0, 1)  # STA; an existing apartment is fine too
    factory = ctypes.c_void_p()
    try:
        if shell32.SHCreateItemFromParsingName(str(path), None, ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return False
        vtable = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        get_image = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_void_p))(vtable[3])
        try:
            bitmap = ctypes.c_void_p()
            # SIIGBF_THUMBNAILONLY: a real frame or failure, never the file-type icon.
            if get_image(factory, SIZE(size, size), 0x8, ctypes.byref(bitmap)) != 0 or not bitmap:
                return False
            try:
                info = BITMAP()
                gdi32.GetObjectW(bitmap, ctypes.sizeof(info), ctypes.byref(info))
                width, height = info.width, abs(info.height)
                if width <= 0 or height <= 0:
                    return False
                header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0)
                pixels = ctypes.create_string_buffer(width * height * 4)
                dc = user32.GetDC(None)
                try:
                    lines = gdi32.GetDIBits(dc, bitmap, 0, height, pixels, ctypes.byref(header), 0)
                finally:
                    user32.ReleaseDC(None, dc)
                if lines != height:
                    return False
                image = Image.frombuffer("RGBA", (width, height), pixels.raw, "raw", "BGRA", 0, 1).convert("RGB")
                out.parent.mkdir(parents=True, exist_ok=True)
                temporary = out.with_suffix(".tmp")
                try:
                    image.save(temporary, "JPEG", quality=78)
                    os.replace(temporary, out)
                finally:
                    temporary.unlink(missing_ok=True)
                return True
            finally:
                gdi32.DeleteObject(bitmap)
        finally:
            release(factory)
    except OSError:
        return False
    finally:
        if initialized:
            ole32.CoUninitialize()
