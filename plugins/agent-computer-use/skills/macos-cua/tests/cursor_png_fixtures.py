"""PNG fixtures and an AppKit stand-in for cursor raster unit tests.

Shared by test_macos_cua.CursorProofTests (unit, every platform) and
integration_macos_cua_cursor.py (real AppKit on macOS). Not a test module.

fake_appkit() models only the NSBitmapImageRep surface that
runtime_snapshot._validate_cursor_raster reads, decoding the 8-bit,
non-interlaced, filter-0 PNGs that png_bytes() writes. Any other PNG decodes
to None, which production reports as "undecodable PNG output". The
integration suite checks that real AppKit reaches the same verdict on these
fixtures.
"""

from __future__ import annotations

import struct
import types
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# AppKit's NSBitmapFormat bit for alpha-first sample order.
NS_BITMAP_FORMAT_ALPHA_FIRST = 1 << 0


def png_header(width: int = 96, height: int = 96) -> bytes:
    return (
        PNG_SIGNATURE
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + int(width).to_bytes(4, "big")
        + int(height).to_bytes(4, "big")
    )


def png_bytes(width: int = 96, height: int = 96, alpha: int = 255) -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(name + payload) & 0xFFFFFFFF
        return len(payload).to_bytes(4, "big") + name + payload + checksum.to_bytes(4, "big")

    ihdr = int(width).to_bytes(4, "big") + int(height).to_bytes(4, "big") + bytes((8, 6, 0, 0, 0))
    pixel = bytes((16, 32, 48, int(alpha)))
    scanlines = (b"\x00" + pixel * int(width)) * int(height)
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class _Bitmap:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def tobytes(self) -> bytes:
        return self._data


class FakeBitmapImageRep:
    """Decoded 8-bit RGB/RGBA bitmap with AppKit's accessor names."""

    # Class-level so fake_appkit(alpha_first=True) can model AppKit handing
    # back ARGB sample order (NSBitmapFormatAlphaFirst) for the same PNG.
    alpha_first = False

    def __init__(self, width: int, height: int, samples: int, pixels: bytes) -> None:
        self._width = width
        self._height = height
        self._samples = samples
        if samples == 4 and self.alpha_first:
            pixels = b"".join(
                pixels[i + 3 : i + 4] + pixels[i : i + 3] for i in range(0, len(pixels), 4)
            )
        self._pixels = pixels

    @classmethod
    def imageRepWithContentsOfFile_(cls, path: str) -> "FakeBitmapImageRep | None":
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        if not data.startswith(PNG_SIGNATURE):
            return None
        offset = len(PNG_SIGNATURE)
        header = None
        idat = b""
        while offset + 8 <= len(data):
            (length,) = struct.unpack(">I", data[offset : offset + 4])
            name = data[offset + 4 : offset + 8]
            payload = data[offset + 8 : offset + 8 + length]
            if len(payload) != length:
                return None
            if name == b"IHDR":
                header = payload
            elif name == b"IDAT":
                idat += payload
            elif name == b"IEND":
                break
            offset += 12 + length
        if header is None or len(header) != 13:
            return None
        width, height, depth, color, _compression, _filter, interlace = struct.unpack(">IIBBBBB", header)
        samples = {2: 3, 6: 4}.get(color)
        if depth != 8 or samples is None or interlace != 0:
            return None
        try:
            raw = zlib.decompress(idat)
        except zlib.error:
            return None
        stride = width * samples
        if len(raw) != height * (stride + 1):
            return None
        rows = []
        for row in range(height):
            start = row * (stride + 1)
            if raw[start] != 0:
                return None
            rows.append(raw[start + 1 : start + 1 + stride])
        return cls(width, height, samples, b"".join(rows))

    def pixelsWide(self) -> int:
        return self._width

    def pixelsHigh(self) -> int:
        return self._height

    def hasAlpha(self) -> bool:
        return self._samples == 4

    def bitsPerSample(self) -> int:
        return 8

    def samplesPerPixel(self) -> int:
        return self._samples

    def isPlanar(self) -> bool:
        return False

    def bitmapData(self) -> _Bitmap:
        return _Bitmap(self._pixels)

    def bytesPerRow(self) -> int:
        return self._width * self._samples

    def bitmapFormat(self) -> int:
        return NS_BITMAP_FORMAT_ALPHA_FIRST if self.alpha_first else 0

    def colorAtX_y_(self, x: int, y: int):
        if not self.hasAlpha():
            return None
        offset = 0 if self.alpha_first else 3
        alpha = self._pixels[(y * self._width + x) * self._samples + offset] / 255.0
        return types.SimpleNamespace(alphaComponent=lambda: alpha)


def fake_appkit(*, alpha_first: bool = False) -> types.ModuleType:
    """An `AppKit` module exposing only what PNG validation imports."""
    module = types.ModuleType("AppKit")
    module.NSBitmapImageRep = type(
        "FakeBitmapImageRep", (FakeBitmapImageRep,), {"alpha_first": alpha_first}
    )
    module.NSBitmapFormatAlphaFirst = NS_BITMAP_FORMAT_ALPHA_FIRST
    return module
