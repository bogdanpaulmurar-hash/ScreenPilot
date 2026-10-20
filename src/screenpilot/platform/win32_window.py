"""Small Win32 window helpers via ``ctypes``: focus, ghosting, key state, Explorer.

Every function is a safe no-op (returning ``False``) on other platforms or
when the call fails, so callers never need platform checks.

* :func:`force_foreground` — Windows only lets the process that received the
  last input bring a window to the front. A capture started by a global
  hotkey may lose that race against Excel or a browser, and then the selector
  overlay is visible but keyboard input still goes to the other app. Briefly
  attaching to the foreground thread's input queue is the documented way to
  hand focus over (no input is injected, no hook is installed).
* :func:`disable_ghosting` — when a window does not answer for 5 s, Windows
  replaces it with a "ghost" copy; for a full-screen overlay that looks like
  the screen closing. ScreenPilot never wants that.
* :func:`key_down` — current state of one virtual key via
  ``GetAsyncKeyState``. It reads a single key on demand; it does not observe
  typing and is not a keyboard hook.
* :func:`reveal_in_explorer` — open the folder and select the file with
  ``SHOpenFolderAndSelectItems``. Passing ``/select,<path>`` to explorer.exe
  breaks on paths with spaces (Python quotes the whole argument, Explorer
  then ignores it and opens *Documents*), which opened the wrong folder.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

VK_ESCAPE = 0x1B

if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.SetFocus.argtypes = [wintypes.HWND]
    _user32.SetActiveWindow.argtypes = [wintypes.HWND]
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def force_foreground(hwnd: int) -> bool:
    """Bring our window ``hwnd`` to the foreground with keyboard focus."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:  # pragma: no cover - Windows only
        handle = wintypes.HWND(int(hwnd))
        foreground = _user32.GetForegroundWindow()
        if foreground == handle.value:
            return True
        current = _kernel32.GetCurrentThreadId()
        other = _user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = bool(other and other != current and _user32.AttachThreadInput(current, other, True))
        try:
            _user32.BringWindowToTop(handle)
            ok = bool(_user32.SetForegroundWindow(handle))
            _user32.SetActiveWindow(handle)
            _user32.SetFocus(handle)
        finally:
            if attached:
                _user32.AttachThreadInput(current, other, False)
        return ok
    except Exception:  # noqa: BLE001 - focus is best effort
        log.debug("force_foreground failed", exc_info=True)
        return False


def disable_ghosting() -> bool:
    if sys.platform != "win32":
        return False
    try:  # pragma: no cover - Windows only
        _user32.DisableProcessWindowsGhosting()
        return True
    except Exception:  # noqa: BLE001
        return False


def key_down(vk: int) -> bool:
    """Is the key held down right now? (``False`` off Windows)."""
    if sys.platform != "win32":
        return False
    try:  # pragma: no cover - Windows only
        return bool(_user32.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


def reveal_in_explorer(path: Path) -> bool:
    """Open Explorer on the folder of ``path`` with ``path`` selected (a folder opens itself)."""
    if sys.platform != "win32":
        return False
    path = Path(path)
    try:  # pragma: no cover - Windows only
        if path.is_dir():
            import os

            os.startfile(str(path))  # noqa: S606 - opens the folder itself
            return True
        if _select_with_shell(path):
            return True
    except Exception:  # noqa: BLE001
        log.debug("SHOpenFolderAndSelectItems failed for %s", path, exc_info=True)
    try:  # pragma: no cover - Windows only
        # Fallback: a raw command line, so the path is quoted the way Explorer expects.
        subprocess.Popen(f'explorer.exe /select,"{path}"')  # noqa: S602,S607 - fixed program, quoted path
        return True
    except OSError:
        log.exception("Could not open Explorer for %s", path)
        return False


def _select_with_shell(path: Path) -> bool:  # pragma: no cover - Windows only
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    hr_init = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED; S_FALSE if already done
    pidl = shell32.ILCreateFromPathW(str(path))
    if not pidl:
        if hr_init in (0, 1):
            ole32.CoUninitialize()
        return False
    try:
        return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
    finally:
        shell32.ILFree(pidl)
        if hr_init in (0, 1):
            ole32.CoUninitialize()
