"""Windows frame source: one persistent GDI DIB section per recording.

Unlike single screenshots, a recording grabs the same rectangle many times a
second, so the memory DC and DIB section are created once in :meth:`open` and
reused. ``CAPTUREBLT`` is left out on purpose: with DWM composition the
desktop DC already contains layered windows, and ``CAPTUREBLT`` makes the
mouse cursor flicker on every grab.
"""

from __future__ import annotations

import ctypes
import logging

from screenpilot.capture import win32_backend as w
from screenpilot.capture.geometry import Rect
from screenpilot.core.errors import CaptureError

log = logging.getLogger(__name__)

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
SM_SWAPBUTTON = 23

w._sig(w.user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)


class Win32FrameSource:
    def __init__(self, *, show_cursor: bool = True) -> None:
        self.show_cursor = show_cursor
        self.rect = Rect(0, 0, 0, 0)
        self._screen_dc = None
        self._mem_dc = None
        self._bitmap = None
        self._old = None
        self._bits = ctypes.c_void_p()
        self._size = 0

    def open(self, rect: Rect) -> None:
        self.close()
        self.rect = rect
        self._screen_dc = w.user32.GetDC(None)
        if not self._screen_dc:
            raise w._last_error("GetDC")
        self._mem_dc = w.gdi32.CreateCompatibleDC(self._screen_dc)
        if not self._mem_dc:
            self.close()
            raise w._last_error("CreateCompatibleDC")
        bmi = w.BITMAPINFO()
        header = bmi.bmiHeader
        header.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        header.biWidth = rect.width
        header.biHeight = -rect.height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = w.BI_RGB
        self._bitmap = w.gdi32.CreateDIBSection(
            self._screen_dc, ctypes.byref(bmi), w.DIB_RGB_COLORS, ctypes.byref(self._bits), None, 0
        )
        if not self._bitmap or not self._bits.value:
            self.close()
            raise w._last_error("CreateDIBSection")
        self._old = w.gdi32.SelectObject(self._mem_dc, self._bitmap)
        self._size = rect.width * rect.height * 4
        log.info("Recording source opened for %s", rect)

    def grab(self) -> bytes:
        if not self._mem_dc:
            raise CaptureError("Frame source is not open")
        r = self.rect
        if not w.gdi32.BitBlt(self._mem_dc, 0, 0, r.width, r.height, self._screen_dc, r.x, r.y, w.SRCCOPY):
            raise w._last_error("BitBlt")
        if self.show_cursor:
            w.Win32CaptureBackend._draw_cursor(self._mem_dc, r)
        w.gdi32.GdiFlush()
        return ctypes.string_at(self._bits.value, self._size)

    def cursor(self) -> tuple[int, int] | None:
        info = w.CURSORINFO()
        info.cbSize = ctypes.sizeof(w.CURSORINFO)
        if not w.user32.GetCursorInfo(ctypes.byref(info)) or not (info.flags & w.CURSOR_SHOWING):
            return None
        return (info.ptScreenPos.x - self.rect.x, info.ptScreenPos.y - self.rect.y)

    def buttons(self) -> tuple[bool, bool]:
        # GetAsyncKeyState reports logical buttons, so swapped buttons need no special handling.
        left = bool(w.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        right = bool(w.user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)
        return left, right

    def close(self) -> None:
        if self._mem_dc and self._old:
            w.gdi32.SelectObject(self._mem_dc, self._old)
        if self._bitmap:
            w.gdi32.DeleteObject(self._bitmap)
        if self._mem_dc:
            w.gdi32.DeleteDC(self._mem_dc)
        if self._screen_dc:
            w.user32.ReleaseDC(None, self._screen_dc)
        self._screen_dc = self._mem_dc = self._bitmap = self._old = None
        self._bits = ctypes.c_void_p()


__all__ = ["Win32FrameSource"]
