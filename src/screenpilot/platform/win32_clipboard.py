"""Windows clipboard via ``ctypes`` (text and images, no dependencies).

Images are placed as ``CF_DIB`` (24-bit, bottom-up), the format every Windows
application understands (Word, Outlook, Teams, Paint, browsers).
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes

from screenpilot.capture.image import CapturedImage
from screenpilot.platform.dib import dib_from_image
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
        self._set(CF_DIB, dib_from_image(image))

    def _open(self) -> None:
        for _ in range(OPEN_RETRIES):  # another app may hold the clipboard briefly
            if user32.OpenClipboard(None):
                return
            time.sleep(OPEN_RETRY_DELAY_S)
        raise ClipboardError(f"Clipboard is busy (Win32 error {ctypes.get_last_error()})")

    def _set(self, fmt: int, data: bytes) -> None:
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            raise ClipboardError("GlobalLock failed")
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        self._open()
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(fmt, handle):
                kernel32.GlobalFree(handle)  # ownership not transferred
                raise ClipboardError(f"SetClipboardData failed (Win32 error {ctypes.get_last_error()})")
        finally:
            user32.CloseClipboard()
