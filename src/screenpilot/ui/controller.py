"""GUI controller: turns hotkeys and menu clicks into captures.

Threading model:
* captures run on the GUI thread (GDI grabs take tens of milliseconds);
* the post-capture workflow (PNG encoding, clipboard, scripts) runs on a
  single background worker so the UI never freezes on large captures;
* bus events and hotkeys reach the GUI thread through Qt signals.
"""

from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

from screenpilot.app import ScreenPilotApp
from screenpilot.capture.geometry import Rect
from screenpilot.capture.image import CaptureResult
from screenpilot.core.errors import ScreenPilotError
from screenpilot.core.events import Event
from screenpilot.hotkeys import RegistrationStatus
from screenpilot.ui.countdown import CountdownOverlay
from screenpilot.ui.qt_helpers import EventBridge, Retranslator
from screenpilot.ui.region_selector import RegionSelector
from screenpilot.workflow import StepStatus

log = logging.getLogger(__name__)

MENU_SETTLE_MS = 250  # let the tray menu disappear before grabbing the screen


class AppController(QObject):
    hotkey_pressed = Signal(int)
    notify = Signal(str, str, bool)  # title, body, is_error
    hotkeys_changed = Signal()

    def __init__(self, app: ScreenPilotApp, *, register_hotkeys: bool = True) -> None:
        super().__init__()
        self.app = app
        self.t = app.translator.t
        self.retranslator = Retranslator(app.translator)
        self.bridge = EventBridge(app.bus, self)
        self.bridge.event.connect(self._on_event)
        self.selector = RegionSelector(app.translator, self)
        self.selector.finished.connect(self._on_region_selected)
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Workflow")
        self._windows: dict[str, QWidget] = {}
        self._countdown: CountdownOverlay | None = None
        self._listener = None
        self.hotkey_pressed.connect(self._on_hotkey)

        self.actions: dict[str, Callable[[], None]] = {
            "capture.region": self.capture_region,
            "capture.window": self.capture_window,
            "capture.fullscreen": self.capture_current_monitor,
            "capture.all_monitors": self.capture_all_monitors,
            "capture.delayed": lambda: self.capture_delayed(self.app.settings.get("capture.default_delay_seconds")),
            "capture.repeat_last": self.repeat_last,
            "ocr.region": self._not_available,
            "app.open_editor": self._not_available,
            "app.open_history": self.show_history,
        }
        if register_hotkeys:
            self.register_hotkeys()

    # -- hotkeys --------------------------------------------------------------
    def register_hotkeys(self) -> None:
        if sys.platform != "win32":
            log.info("Global hotkeys are only available on Windows")
            return
        from screenpilot.hotkeys.win32_listener import Win32HotkeyListener

        try:
            self._listener = Win32HotkeyListener(self.hotkey_pressed.emit)
            status = self.app.hotkeys.register_all(self._listener)
        except Exception:  # noqa: BLE001 - the app must still work from the tray
            log.exception("Hotkey listener could not start")
            return
        if any(s is RegistrationStatus.FAILED for s in status.values()):
            self.notify.emit(self.t("hotkeys.title"), self.t("notify.hotkey_conflicts"), True)

    @property
    def hotkeys_active(self) -> bool:
        return self._listener is not None

    def _on_hotkey(self, binding_id: int) -> None:
        action = self.app.hotkeys.action_for_binding_id(binding_id)
        log.info("Hotkey %s -> %s", binding_id, action)
        if action:
            self.trigger(action)

    def trigger(self, action: str, *, from_menu: bool = False) -> None:
        handler = self.actions.get(action)
        if handler is None:
            log.warning("No handler for action %s", action)
            return
        if from_menu and action.startswith("capture."):
            QTimer.singleShot(MENU_SETTLE_MS, handler)
        else:
            handler()

    # -- capture flows --------------------------------------------------------
    def _guard(self, fn: Callable[[], CaptureResult | None]) -> None:
        try:
            result = fn()
        except ScreenPilotError as exc:
            log.warning("Capture failed: %s", exc)
            self.notify.emit(self.t("notify.failed.title"), self.t(exc.message_key), True)
            return
        if result is not None:
            self.process(result)

    def capture_region(self) -> None:
        if self.selector.active:
            return
        try:
            frozen = self.app.capture.freeze()
            windows = self.app.capture.windows()
            monitors = self.app.capture.monitors()
        except ScreenPilotError as exc:
            self.notify.emit(self.t("notify.failed.title"), self.t(exc.message_key), True)
            return
        self.selector.start(frozen, monitors, windows)

    def _on_region_selected(self, rect: Rect | None) -> None:
        frozen = self.selector.frozen
        self.selector.frozen = None
        if rect is None or frozen is None:
            return
        self._guard(lambda: self.app.capture.region_from_frozen(frozen, rect))

    def capture_window(self) -> None:
        self._guard(self.app.capture.capture_window)

    def capture_current_monitor(self) -> None:
        self._guard(self.app.capture.capture_current_monitor)

    def capture_all_monitors(self) -> None:
        self._guard(self.app.capture.capture_all_monitors)

    def repeat_last(self) -> None:
        self._guard(self.app.capture.repeat_last)

    def capture_delayed(self, seconds: int, then: Callable[[], None] | None = None) -> None:
        if self._countdown is not None:
            return
        self._countdown = CountdownOverlay(int(seconds), self.app.translator)
        self._countdown.elapsed.connect(lambda: self._after_countdown(then or self.capture_region))
        self._countdown.cancelled.connect(self._countdown_cancelled)
        self._countdown.start()

    def _after_countdown(self, then: Callable[[], None]) -> None:
        self._countdown = None
        then()

    def _countdown_cancelled(self) -> None:
        self._countdown = None
        log.info("Delayed capture cancelled")

    # -- post capture ---------------------------------------------------------
    def process(self, result: CaptureResult) -> None:
        """Run the configured action chain in the background."""
        future = self._worker.submit(self.app.run_after_capture, result)
        future.add_done_callback(lambda f: self._workflow_done(f, result))

    def _workflow_done(self, future, result: CaptureResult) -> None:  # worker thread
        try:
            steps, ctx = future.result()
        except Exception as exc:  # noqa: BLE001
            log.exception("Workflow crashed")
            self.notify.emit(self.t("notify.failed.title"), str(exc), True)
            return
        failed = [s for s in steps if s.status is StepStatus.FAILED]
        size = dict(width=result.image.width, height=result.image.height)
        if failed:
            first = failed[0]
            body = self.t(
                "notify.workflow_failed", action=self.t(f"workflow.action.{first.action_id}"), reason=first.message
            )
            self.notify.emit(self.t("notify.failed.title"), body, True)
        elif ctx.saved_path is not None:
            self.notify.emit(
                self.t("notify.captured.title"), self.t("notify.captured.body", name=Path(ctx.saved_path).name, **size), False
            )
        else:
            self.notify.emit(self.t("notify.copied.title"), self.t("notify.copied.body", **size), False)

    def _on_event(self, event: Event) -> None:
        if event.name == "editor.open_requested":
            self._not_available()

    def _not_available(self) -> None:
        self.notify.emit(self.t("app.name"), self.t("notify.not_available"), False)

    # -- windows --------------------------------------------------------------
    def _show_single(self, key: str, factory: Callable[[], QWidget]) -> QWidget:
        window = self._windows.get(key)
        if window is None:
            window = factory()
            window.destroyed.connect(lambda *_: self._windows.pop(key, None))
            self._windows[key] = window
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def show_settings(self) -> QWidget:
        from screenpilot.ui.settings_dialog import SettingsDialog

        return self._show_single("settings", lambda: SettingsDialog(self))

    def show_history(self) -> QWidget:
        from screenpilot.ui.history_window import HistoryWindow

        return self._show_single("history", lambda: HistoryWindow(self))

    def show_wizard(self) -> QWidget:
        from screenpilot.ui.wizard import FirstRunWizard

        return self._show_single("wizard", lambda: FirstRunWizard(self))

    def open_captures_folder(self) -> None:
        folder = self.app.output_dir
        folder.mkdir(parents=True, exist_ok=True)
        try:
            self.app.shell.reveal(folder)
        except OSError:
            log.exception("Could not open %s", folder)

    # -- lifecycle ------------------------------------------------------------
    def shutdown(self) -> None:
        log.info("Shutting down UI")
        for window in list(self._windows.values()):
            window.close()
        if self._listener is not None:
            self._listener.stop()
        self._worker.shutdown(wait=True)
        self.bridge.close()
        self.retranslator.dispose()
        self.app.shutdown()

    def quit(self) -> None:
        self.shutdown()
        QApplication.quit()
