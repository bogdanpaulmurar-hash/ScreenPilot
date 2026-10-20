"""Clipboard image encodings (pure Python + Pillow, testable on any OS)."""

from __future__ import annotations

import struct

from screenpilot.capture.image import CapturedImage


def dib_from_image(image: CapturedImage) -> bytes:
    """BITMAPINFOHEADER + 24-bit bottom-up pixel rows (padded to 4 bytes)."""
    row_bytes = image.width * 3
    padding = (4 - row_bytes % 4) % 4
    header = struct.pack("<IiiHHIIiiII", 40, image.width, image.height, 1, 24, 0, 0, 2835, 2835, 0, 0)
    rows = []
    stride = image.stride
    for y in range(image.height - 1, -1, -1):
        row = image.bgra[y * stride : (y + 1) * stride]
        bgr = bytearray(row_bytes)
        bgr[0::3] = row[0::4]
        bgr[1::3] = row[1::4]
        bgr[2::3] = row[2::4]
        rows.append(bytes(bgr) + b"\x00" * padding)
    return header + b"".join(rows)


def png_from_image(image: CapturedImage) -> bytes:
    """PNG bytes for the registered "PNG" clipboard format.

    Office, Teams, Outlook and browsers prefer it over ``CF_DIB``: a pasted
    PNG stays lossless, while a pasted bitmap is often recompressed as JPEG.
    Fast compression keeps copying large captures quick.
    """
    import io

    buffer = io.BytesIO()
    image.to_pil().save(buffer, "PNG", compress_level=1)
    return buffer.getvalue()
