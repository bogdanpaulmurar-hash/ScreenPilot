"""Platform abstraction for screen capture.

The capture service only talks to :class:`CaptureBackend`. The Windows
implementation uses GDI via ``ctypes`` (no third-party capture library); tests
use :class:`FakeBackend`. A DXGI / Windows.Graphics.Capture backend can be added
later for hardware-accelerated recording without touching callers.
"""

from __future__ import annotations

import sys
from typing import Protocol, Sequence

from screenpilot.capture.geometry import Monitor, Point, Rect, WindowInfo
from screenpilot.capture.image import BYTES_PER_PIXEL, CapturedImage
from screenpilot.core.errors import CaptureError, PlatformNotSupportedError


class CaptureBackend(Protocol):
    name: str

    def monitors(self) -> Sequence[Monitor]: ...

    def grab(self, rect: Rect) -> CapturedImage: ...

    def cursor_position(self) -> Point: ...

    def foreground_window(self) -> WindowInfo | None: ...

    def list_windows(self) -> Sequence[WindowInfo]: ...

    def window_at(self, point: Point, *, include_children: bool = False) -> WindowInfo | None: ...


def default_backend() -> CaptureBackend:
    if sys.platform == "win32":
        from screenpilot.capture.win32_backend import Win32CaptureBackend

        return Win32CaptureBackend()
    raise PlatformNotSupportedError(f"No capture backend for platform {sys.platform}")


class FakeBackend:
    """Deterministic backend for tests and demos.

    Each pixel's colour encodes its virtual-desktop coordinates, so tests can
    verify that the right area was grabbed. Very large grabs are filled with a
    flat colour instead, to keep the test suite fast.
    """

    PATTERN_LIMIT = 250_000

    name = "fake"

    def __init__(
        self,
        monitors: Sequence[Monitor],
        windows: Sequence[WindowInfo] = (),
        cursor: Point = Point(0, 0),
    ) -> None:
        self._monitors = list(monitors)
        self._windows = list(windows)
        self.cursor = cursor
        self.foreground: WindowInfo | None = self._windows[0] if self._windows else None
        self.grabs: list[Rect] = []

    @staticmethod
    def color_at(x: int, y: int) -> tuple[int, int, int]:
        return (x & 0xFF, y & 0xFF, ((x >> 8) ^ (y >> 8)) & 0xFF)

    def monitors(self) -> Sequence[Monitor]:
        return list(self._monitors)

    def grab(self, rect: Rect) -> CapturedImage:
        if rect.is_empty:
            raise CaptureError("Empty capture rectangle")
        self.grabs.append(rect)
        if rect.area > self.PATTERN_LIMIT:
            return CapturedImage(rect.width, rect.height, bytes((64, 64, 64, 255)) * rect.area)
        buf = bytearray(rect.width * rect.height * BYTES_PER_PIXEL)
        i = 0
        for y in range(rect.y, rect.bottom):
            for x in range(rect.x, rect.right):
                r, g, b = self.color_at(x, y)
                buf[i : i + 4] = bytes((b, g, r, 255))
                i += 4
        return CapturedImage(rect.width, rect.height, bytes(buf))

    def cursor_position(self) -> Point:
        return self.cursor

    def foreground_window(self) -> WindowInfo | None:
        return self.foreground

    def list_windows(self) -> Sequence[WindowInfo]:
        return list(self._windows)

    def window_at(self, point: Point, *, include_children: bool = False) -> WindowInfo | None:
        for window in self._windows:
            if window.bounds.contains(point):
                return window
        return None
