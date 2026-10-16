"""Mouse-wheel scrolling on Windows for scrolling capture (SendInput, no hooks).

The cursor is parked in the middle of the captured region (so the wheel goes
to the right scrollable element, like a user would do) and restored when the
capture finishes. Esc stops the capture; it is read with GetAsyncKeyState,
which only checks the key's current state and does not observe typing.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from screenpilot.capture.geometry import Point, Rect
from screenpilot.core.errors import PlatformNotSupportedError

if sys.platform != "win32":  # pragma: no cover
    raise PlatformNotSupportedError("win32_scroller can only be imported on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120
VK_ESCAPE = 0x1B
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("_pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


class Win32Scroller:
    def __init__(self) -> None:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        self._saved = Point(pt.x, pt.y)
        user32.GetAsyncKeyState(VK_ESCAPE)  # clear the "pressed since last call" bit

    def scroll(self, rect: Rect, notches: int) -> None:
        user32.SetCursorPos(rect.x + rect.width // 2, rect.y + rect.height // 2)
        event = INPUT(type=INPUT_MOUSE)
        event.u.mi = MOUSEINPUT(0, 0, ctypes.c_uint32(-WHEEL_DELTA * notches).value, MOUSEEVENTF_WHEEL, 0, 0)
        user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))

    def stop_requested(self) -> bool:
        return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)

    def restore_cursor(self) -> None:
        user32.SetCursorPos(self._saved.x, self._saved.y)
