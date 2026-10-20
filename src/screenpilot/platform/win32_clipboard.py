"""Windows clipboard via ``ctypes`` (text and images, no dependencies).

Images are placed twice: as ``CF_DIB`` (24-bit, bottom-up), the format every
Windows application understands, and as the registered ``PNG`` format, which
Office, Teams, Outlook and browsers prefer, so pasted captures stay lossless
instead of being recompressed.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes

from screenpilot.capture.image import CapturedImage
from screenpilot.platform.dib import dib_from_image, png_from_image
from screenpilot.core.errors import PlatformNotSupportedError, ScreenPilotError

log = logging.getLogger(__name__)

if sys.platform != "win32":  # pragma: no cover
    raise PlatformNotSupportedError("win32_clipboard can only be imported on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
CF_DIB = 8
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

OPEN_RETRIES = 10
OPEN_RETRY_DELAY_S = 0.05


class ClipboardError(ScreenPilotError):
    message_key = "error.generic"


class Win32Clipboard:
    def set_text(self, text: str) -> None:
        self._set(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))

    def set_image(self, image: CapturedImage) -> None:
        items = [(CF_DIB, dib_from_image(image))]
        png_format = user32.RegisterClipboardFormatW("PNG")
        if png_format:
            try:
                items.append((png_format, png_from_image(image)))
            except Exception:  # noqa: BLE001 - CF_DIB alone still works everywhere
                log.exception("PNG clipboard data could not be created")
        self._set_many(items)

    def _open(self) -> None:
        for _ in range(OPEN_RETRIES):  # another app may hold the clipboard briefly
            if user32.OpenClipboard(None):
                return
            time.sleep(OPEN_RETRY_DELAY_S)
        raise ClipboardError(f"Clipboard is busy (Win32 error {ctypes.get_last_error()})")

    def _set(self, fmt: int, data: bytes) -> None:
        self._set_many([(fmt, data)])

    @staticmethod
    def _global(data: bytes):
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            raise ClipboardError("GlobalLock failed")
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        return handle

    def _set_many(self, items: list[tuple[int, bytes]]) -> None:
        """Replace the clipboard with all ``(format, data)`` items at once."""
        handles = [(fmt, self._global(data)) for fmt, data in items]
        try:
            self._open()
        except ClipboardError:
            for _fmt, handle in handles:
                kernel32.GlobalFree(handle)
            raise
        try:
            user32.EmptyClipboard()
            for index, (fmt, handle) in enumerate(handles):
                if not user32.SetClipboardData(fmt, handle):
                    error = ctypes.get_last_error()
                    for _f, pending in handles[index:]:
                        kernel32.GlobalFree(pending)  # ownership not transferred
                    if index == 0:
                        raise ClipboardError(f"SetClipboardData failed (Win32 error {error})")
                    log.warning("Clipboard format %s could not be set (Win32 error %s)", fmt, error)
                    break
        finally:
            user32.CloseClipboard()
