"""System-wide hotkeys on Windows via ``RegisterHotKey`` (no keyboard hooks).

``RegisterHotKey`` is the officially supported, low-privilege mechanism: it
does not observe other keystrokes (unlike low-level keyboard hooks, which
security tools often flag) and it fails cleanly when another application
already owns a combination - which feeds the conflict diagnostics.

Win32 requires registration and the message loop to run on the same thread,
so this class owns a dedicated thread and marshals calls onto it.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
from concurrent.futures import Future
from ctypes import wintypes
from typing import Callable

from screenpilot.core.errors import PlatformNotSupportedError
from screenpilot.hotkeys.model import Hotkey

log = logging.getLogger(__name__)

if sys.platform != "win32":  # pragma: no cover
    raise PlatformNotSupportedError("win32_listener can only be imported on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP = 0x8000
PM_NOREMOVE = 0x0000

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

CALL_TIMEOUT_S = 2.0


class Win32HotkeyListener:
    def __init__(self, on_hotkey: Callable[[int], None]) -> None:
        self._on_hotkey = on_hotkey
        self._requests: "queue.Queue[tuple[Callable[[], object], Future]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._registered: set[int] = set()

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="HotkeyListener", daemon=True)
        self._thread.start()
        if not self._ready.wait(CALL_TIMEOUT_S):
            raise RuntimeError("Hotkey listener thread did not start")

    def stop(self) -> None:
        if self._thread is None:
            return
        user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(CALL_TIMEOUT_S)
        self._thread = None

    # -- HotkeyListener protocol ---------------------------------------------
    def register(self, binding_id: int, hotkey: Hotkey) -> bool:
        def do() -> bool:
            ok = bool(user32.RegisterHotKey(None, binding_id, hotkey.win32_modifiers, hotkey.vk))
            if ok:
                self._registered.add(binding_id)
            else:
                log.info("RegisterHotKey(%s) failed, Win32 error %d", hotkey, ctypes.get_last_error())
            return ok

        return bool(self._call(do))

    def unregister(self, binding_id: int) -> None:
        def do() -> None:
            if binding_id in self._registered:
                user32.UnregisterHotKey(None, binding_id)
                self._registered.discard(binding_id)

        self._call(do)

    # -- thread internals -----------------------------------------------------
    def _call(self, fn: Callable[[], object]) -> object:
        self.start()
        future: Future = Future()
        self._requests.put((fn, future))
        user32.PostThreadMessageW(self._thread_id, WM_APP, 0, 0)
        return future.result(CALL_TIMEOUT_S)

    def _drain_requests(self) -> None:
        while True:
            try:
                fn, future = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                future.set_result(fn())
            except Exception as exc:  # noqa: BLE001
                future.set_exception(exc)

    def _run(self) -> None:
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)  # create the message queue
        self._thread_id = kernel32.GetCurrentThreadId()
        self._ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self._on_hotkey(int(msg.wParam))
                    except Exception:  # noqa: BLE001
                        log.exception("Hotkey handler failed")
                elif msg.message == WM_APP:
                    self._drain_requests()
        finally:
            for binding_id in list(self._registered):
                user32.UnregisterHotKey(None, binding_id)
            self._registered.clear()
            self._drain_requests()
            log.info("Hotkey listener stopped")
