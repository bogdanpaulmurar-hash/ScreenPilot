"""Windows capture backend: GDI through ``ctypes``.

Why GDI + ctypes:
* zero third-party dependencies (no capture library to vet or update);
* works on every supported Windows version, including older corporate images;
* ``BitBlt`` with ``CAPTUREBLT`` includes layered windows (tooltips, menus).

The process opts into per-monitor DPI awareness (v2) so that coordinates are
physical pixels and captures are pixel-exact on mixed-DPI multi-monitor setups.

Not covered here (planned): DRM-protected content is black by design;
hardware-accelerated recording will use Windows.Graphics.Capture / DXGI.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Sequence

from screenpilot.capture.geometry import Monitor, Point, Rect, WindowInfo
from screenpilot.capture.image import CapturedImage
from screenpilot.core.errors import CaptureError, PlatformNotSupportedError

log = logging.getLogger(__name__)

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise PlatformNotSupportedError("win32_backend can only be imported on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
try:
    shcore = ctypes.WinDLL("shcore")
except OSError:  # Windows 7
    shcore = None

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0
MONITORINFOF_PRIMARY = 0x1
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
MDT_EFFECTIVE_DPI = 0
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
CWP_SKIPINVISIBLE = 0x0001


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _sig(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_sig(user32.EnumDisplayMonitors, wintypes.BOOL, wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM)
_sig(user32.GetMonitorInfoW, wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
_sig(user32.GetDC, wintypes.HDC, wintypes.HWND)
_sig(user32.ReleaseDC, ctypes.c_int, wintypes.HWND, wintypes.HDC)
_sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_sig(user32.GetForegroundWindow, wintypes.HWND)
_sig(user32.GetWindowTextLengthW, ctypes.c_int, wintypes.HWND)
_sig(user32.GetWindowTextW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetClassNameW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_sig(user32.IsWindowVisible, wintypes.BOOL, wintypes.HWND)
_sig(user32.IsIconic, wintypes.BOOL, wintypes.HWND)
_sig(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_sig(user32.WindowFromPoint, wintypes.HWND, wintypes.POINT)
_sig(user32.GetAncestor, wintypes.HWND, wintypes.HWND, wintypes.UINT)
_sig(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_sig(user32.GetWindowLongW, wintypes.LONG, wintypes.HWND, ctypes.c_int)
_sig(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
_sig(
    gdi32.CreateDIBSection,
    wintypes.HBITMAP,
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
)
_sig(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_sig(
    gdi32.BitBlt,
    wintypes.BOOL,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
)
_sig(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_sig(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
_sig(gdi32.GdiFlush, wintypes.BOOL)
_sig(dwmapi.DwmGetWindowAttribute, ctypes.c_long, wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
if shcore is not None:
    _sig(
        shcore.GetDpiForMonitor,
        ctypes.c_long,
        wintypes.HMONITOR,
        ctypes.c_int,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
    )


def enable_dpi_awareness() -> str:
    """Opt into the best available DPI awareness. Safe to call more than once.

    Qt 6 already makes the process per-monitor-v2 aware; in that case nothing
    is changed (changing awareness after windows exist is not allowed).
    """
    try:
        get_ctx = _sig(user32.GetThreadDpiAwarenessContext, ctypes.c_void_p)
        get_awareness = _sig(user32.GetAwarenessFromDpiAwarenessContext, ctypes.c_int, ctypes.c_void_p)
        if get_awareness(get_ctx()) == 2:  # DPI_AWARENESS_PER_MONITOR_AWARE
            return "per-monitor (already set)"
    except AttributeError:  # older than Windows 10 1607
        pass
    try:
        fn = _sig(user32.SetProcessDpiAwarenessContext, wintypes.BOOL, ctypes.c_void_p)
        if fn(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return "per-monitor-v2"
    except AttributeError:
        pass
    if shcore is not None:
        try:
            if shcore.SetProcessDpiAwareness(2) == 0:
                return "per-monitor"
        except AttributeError:
            pass
    user32.SetProcessDPIAware()
    return "system"


def _rect(r: wintypes.RECT) -> Rect:
    return Rect.from_ltrb(r.left, r.top, r.right, r.bottom)


def _last_error(what: str) -> CaptureError:
    code = ctypes.get_last_error()
    return CaptureError(f"{what} failed (Win32 error {code})", code=code)


class Win32CaptureBackend:
    name = "win32-gdi"

    def __init__(self) -> None:
        self.dpi_mode = enable_dpi_awareness()
        log.info("Win32 capture backend ready (DPI awareness: %s)", self.dpi_mode)

    # -- monitors -------------------------------------------------------------
    def monitors(self) -> Sequence[Monitor]:
        handles: list[int] = []

        def callback(hmon, _hdc, _rect_ptr, _data):
            handles.append(hmon)
            return True

        if not user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0):
            raise _last_error("EnumDisplayMonitors")

        result: list[Monitor] = []
        for hmon in handles:
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                log.warning("GetMonitorInfoW failed for monitor %s", hmon)
                continue
            result.append(
                Monitor(
                    index=0,
                    bounds=_rect(info.rcMonitor),
                    work_area=_rect(info.rcWork),
                    primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    device_name=info.szDevice,
                    scale=self._scale(hmon),
                )
            )
        # Stable, user-friendly order: primary first, then left-to-right, top-to-bottom.
        result.sort(key=lambda m: (not m.primary, m.bounds.x, m.bounds.y))
        return [
            Monitor(i + 1, m.bounds, m.work_area, m.primary, m.device_name, m.scale) for i, m in enumerate(result)
        ]

    @staticmethod
    def _scale(hmon) -> float:
        if shcore is None:
            return 1.0
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        if shcore.GetDpiForMonitor(hmon, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) != 0:
            return 1.0
        return round(dpi_x.value / 96.0, 2)

    # -- pixels ---------------------------------------------------------------
    def grab(self, rect: Rect) -> CapturedImage:
        if rect.is_empty:
            raise CaptureError("Empty capture rectangle")
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise _last_error("GetDC")
        mem_dc = bitmap = old = None
        try:
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            if not mem_dc:
                raise _last_error("CreateCompatibleDC")
            bmi = BITMAPINFO()
            header = bmi.bmiHeader
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = rect.width
            header.biHeight = -rect.height  # negative = top-down rows
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise _last_error("CreateDIBSection")
            old = gdi32.SelectObject(mem_dc, bitmap)
            if not gdi32.BitBlt(
                mem_dc, 0, 0, rect.width, rect.height, screen_dc, rect.x, rect.y, SRCCOPY | CAPTUREBLT
            ):
                raise _last_error("BitBlt")
            gdi32.GdiFlush()
            data = ctypes.string_at(bits.value, rect.width * rect.height * 4)
            return CapturedImage(rect.width, rect.height, data)
        finally:
            if mem_dc and old:
                gdi32.SelectObject(mem_dc, old)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)

    def cursor_position(self) -> Point:
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            raise _last_error("GetCursorPos")
        return Point(pt.x, pt.y)

    # -- windows --------------------------------------------------------------
    def _window_info(self, hwnd) -> WindowInfo | None:
        if not hwnd:
            return None
        bounds = self._window_bounds(hwnd)
        if bounds is None or bounds.is_empty:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return WindowInfo(
            handle=int(hwnd), title=title_buf.value, bounds=bounds, class_name=class_buf.value, process_id=pid.value
        )

    @staticmethod
    def _window_bounds(hwnd) -> Rect | None:
        # Extended frame bounds exclude the invisible resize border of Win10/11 windows.
        rect = wintypes.RECT()
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
            return _rect(rect)
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return _rect(rect)
        return None

    @staticmethod
    def _is_cloaked(hwnd) -> bool:
        cloaked = wintypes.DWORD()
        hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return hr == 0 and cloaked.value != 0

    def foreground_window(self) -> WindowInfo | None:
        return self._window_info(user32.GetForegroundWindow())

    def list_windows(self) -> Sequence[WindowInfo]:
        """Visible, non-minimised top-level windows in Z-order (topmost first)."""
        result: list[WindowInfo] = []

        def callback(hwnd, _data):
            try:
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or self._is_cloaked(hwnd):
                    return True
                if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                    return True
                info = self._window_info(hwnd)
                if info is not None and info.title:
                    result.append(info)
            except Exception:  # noqa: BLE001 - never let a callback raise into Win32
                log.exception("Window enumeration callback failed")
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return result

    def window_at(self, point: Point, *, include_children: bool = False) -> WindowInfo | None:
        hwnd = user32.WindowFromPoint(wintypes.POINT(point.x, point.y))
        if not hwnd:
            return None
        if not include_children:
            hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        return self._window_info(hwnd)
