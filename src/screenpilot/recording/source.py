"""Frame sources for recording.

A frame source owns everything that touches the screen during a recording:
grabbing pixels for a fixed rectangle, the cursor position and the mouse
button state (polled with ``GetAsyncKeyState`` on Windows — no input hooks,
which endpoint-security tools flag as keylogger behaviour).
"""

from __future__ import annotations

from typing import Protocol

from screenpilot.capture.geometry import Rect


class FrameSource(Protocol):
    rect: Rect

    def open(self, rect: Rect) -> None: ...

    def grab(self) -> bytes:
        """Top-down BGRA pixels of ``rect`` (len = w * h * 4)."""
        ...

    def cursor(self) -> tuple[int, int] | None:
        """Cursor position relative to ``rect`` or None when hidden."""
        ...

    def buttons(self) -> tuple[bool, bool]:
        """(left, right) mouse buttons currently pressed."""
        ...

    def close(self) -> None: ...


class BackendFrameSource:
    """Generic source on top of any :class:`CaptureBackend` (tests, demo, fallback)."""

    def __init__(self, backend) -> None:
        self.backend = backend
        self.rect = Rect(0, 0, 0, 0)
        self.pressed: tuple[bool, bool] = (False, False)

    def open(self, rect: Rect) -> None:
        self.rect = rect

    def grab(self) -> bytes:
        return self.backend.grab(self.rect).bgra

    def cursor(self) -> tuple[int, int] | None:
        try:
            pos = self.backend.cursor_position()
        except Exception:  # noqa: BLE001 - cursor is cosmetic
            return None
        return (pos.x - self.rect.x, pos.y - self.rect.y)

    def buttons(self) -> tuple[bool, bool]:
        return self.pressed

    def close(self) -> None:
        pass


def default_source(backend):
    """Persistent-GDI source on Windows, generic source elsewhere."""
    import sys

    if sys.platform == "win32" and getattr(backend, "name", "") == "win32-gdi":
        from screenpilot.recording.win32_source import Win32FrameSource

        return Win32FrameSource(show_cursor=True)
    return BackendFrameSource(backend)
