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
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from screenpilot.app import ScreenPilotApp
from screenpilot.capture.detect import FrameFinder
from screenpilot.capture.geometry import Point, Rect
from screenpilot.capture.image import CaptureResult
from screenpilot.core.errors import ScreenPilotError
from screenpilot.core.events import Event
from screenpilot.hotkeys import RegistrationStatus
from screenpilot.ui.countdown import CountdownOverlay
from screenpilot.ui.qt_helpers import EventBridge, Retranslator
from screenpilot.ui.region_selector import (
    ACTION_COPY,
    ACTION_PIN,
    ACTION_SAVE_AS,
    ACTION_SCROLL,
    RegionSelector,
    physical_to_logical,
)
from screenpilot.workflow import StepStatus

log = logging.getLogger(__name__)

MENU_SETTLE_MS = 250  # let the tray menu disappear before grabbing the screen
CHROME_SETTLE_MS = 150  # let our own floating bar disappear before grabbing


class AppController(QObject):
    hotkey_pressed = Signal(int)
    notify = Signal(str, str, bool)  # title, body, is_error
    hotkeys_changed = Signal()
    captured = Signal(object, object, str)  # CaptureResult, saved Path | None, error message
    scroll_finished = Signal(object, str, object)  # CaptureResult | None, reason, scroller

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
        self._preview: QWidget | None = None
        self.pins: list[QWidget] = []
        self.edge_bar: QWidget | None = None
        self.scroller_factory: Callable[[], object] | None = None  # tests inject a fake
        self._scroll_indicator: QWidget | None = None
        self._region_default = "default"  # what Enter does in the selector ("scroll" from the hub tile)
        self.scroll_finished.connect(self._on_scroll_finished)
        self.hotkey_pressed.connect(self._on_hotkey)
        self.captured.connect(self._on_captured)

        self.actions: dict[str, Callable[[], None]] = {
            "capture.region": self.capture_region,
            "capture.window": self.capture_window,
            "capture.fullscreen": self.capture_current_monitor,
            "capture.all_monitors": self.capture_all_monitors,
            "capture.delayed": lambda: self.capture_delayed(self.app.settings.get("capture.default_delay_seconds")),
            "capture.repeat_last": self.repeat_last,
            "capture.scrolling": self.capture_region_then_scroll,
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
        if action.startswith("capture."):
            hidden = self.hide_chrome()
            if from_menu or hidden:
                QTimer.singleShot(MENU_SETTLE_MS if from_menu else CHROME_SETTLE_MS, handler)
                return
        handler()

    @property
    def busy(self) -> bool:
        """A capture flow is in progress (selector, countdown or scrolling)."""
        return self.selector.active or self._countdown is not None or self._scroll_indicator is not None

    def hide_chrome(self) -> bool:
        """Hide ScreenPilot's own floating UI so it never appears in a capture."""
        bar = self.edge_bar
        if bar is not None and bar.isVisible():
            bar.suspend()
            return True
        return False

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
        self.selector.start(frozen, monitors, windows, self._frame_provider(frozen))

    def _frame_provider(self, frozen: CaptureResult):
        """Inner frames under the cursor: detected boxes in the image + child controls."""
        state: dict = {"finder": None, "children": {}}
        backend = self.app.capture.backend

        def build() -> None:
            try:
                state["finder"] = FrameFinder(frozen.image, Point(frozen.region.x, frozen.region.y))
            except Exception:  # noqa: BLE001 - detection is a convenience, never fatal
                log.exception("Frame detection unavailable")

        threading.Thread(target=build, name="FrameFinder", daemon=True).start()

        def frames(point: Point, window) -> list[Rect]:
            found: list[Rect] = []
            finder = state["finder"]
            if finder is not None:
                found.extend(finder.boxes_at(point))
            child_lister = getattr(backend, "child_windows", None)
            if window is not None and child_lister is not None:
                children = state["children"].get(window.handle)
                if children is None:
                    try:
                        children = [c.bounds for c in child_lister(window.handle)]
                    except Exception:  # noqa: BLE001
                        log.exception("Child window enumeration failed")
                        children = []
                    state["children"][window.handle] = children
                found.extend(r for r in children if r.contains(point))
            return found

        return frames

    def _on_region_selected(self, outcome: tuple[Rect, str] | None) -> None:
        frozen = self.selector.frozen
        self.selector.frozen = None
        if outcome is None or frozen is None:
            return
        rect, action = outcome
        default, self._region_default = self._region_default, "default"
        if action == "default":
            action = default
        if action == ACTION_SCROLL:
            self.start_scrolling(rect)
            return
        try:
            result = self.app.capture.region_from_frozen(frozen, rect)
        except ScreenPilotError as exc:
            self.notify.emit(self.t("notify.failed.title"), self.t(exc.message_key), True)
            return
        if action == ACTION_COPY:
            self.process(result, steps=[{"id": "copy_image"}])
        elif action == ACTION_SAVE_AS:
            path = self.save_result_as(result)
            if path is not None:
                self.process(result, steps=[{"id": "copy_image"}], saved_path=path)
        elif action == ACTION_PIN:
            self.pin(result)
        else:
            self.process(result)

    def capture_region_then_scroll(self) -> None:
        """Selector where Enter / click starts a scrolling capture."""
        self.capture_region()
        if self.selector.active:
            self._region_default = ACTION_SCROLL

    # -- scrolling ------------------------------------------------------------
    def _make_scroller(self):
        if self.scroller_factory is not None:
            return self.scroller_factory()
        if sys.platform == "win32":
            from screenpilot.capture.win32_scroller import Win32Scroller

            return Win32Scroller()
        raise ScreenPilotError("Scrolling capture needs Windows", message_key="error.platform_not_supported")

    def start_scrolling(self, rect: Rect, **options) -> None:
        if self._scroll_indicator is not None:
            return
        try:
            scroller = self._make_scroller()
        except ScreenPilotError as exc:
            self.notify.emit(self.t("notify.failed.title"), self.t(exc.message_key), True)
            return
        self.hide_chrome()
        from screenpilot.ui.preview import ScrollIndicator

        indicator = ScrollIndicator(self.app.translator, physical_to_logical(Point(rect.x, rect.bottom), self.app.capture.monitors()))
        self._scroll_indicator = indicator
        indicator.show_near()

        def run() -> None:
            try:
                result, reason = self.app.capture.capture_scrolling(rect, scroller, **options)
            except Exception as exc:  # noqa: BLE001
                log.exception("Scrolling capture failed")
                self.scroll_finished.emit(None, str(exc), scroller)
                return
            self.scroll_finished.emit(result, reason, scroller)

        threading.Thread(target=run, name="ScrollingCapture", daemon=True).start()

    def _on_scroll_finished(self, result: CaptureResult | None, reason: str, scroller) -> None:
        restore = getattr(scroller, "restore_cursor", None)
        if restore is not None:
            restore()
        if self._scroll_indicator is not None:
            self._scroll_indicator.close()
            self._scroll_indicator = None
        if result is None:
            self.notify.emit(self.t("notify.failed.title"), reason, True)
            return
        if reason == "no_match":
            self.notify.emit(self.t("action.capture.scrolling"), self.t("scroll.no_match"), False)
        elif reason == "end" and result.image.height <= result.metadata.get("view_height", 0):
            self.notify.emit(self.t("action.capture.scrolling"), self.t("scroll.not_scrollable"), False)
        self.process(result)

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
    def process(self, result: CaptureResult, steps: list[dict] | None = None, *, saved_path: Path | None = None) -> None:
        """Run the configured (or given) action chain in the background."""
        future = self._worker.submit(self.app.run_after_capture, result, steps)
        future.add_done_callback(lambda f: self._workflow_done(f, result, saved_path))

    def _workflow_done(self, future, result: CaptureResult, saved_path: Path | None) -> None:  # worker thread
        try:
            steps, ctx = future.result()
        except Exception as exc:  # noqa: BLE001
            log.exception("Workflow crashed")
            self.captured.emit(result, None, str(exc))
            return
        failed = [s for s in steps if s.status is StepStatus.FAILED]
        error = ""
        if failed:
            first = failed[0]
            error = self.t(
                "notify.workflow_failed", action=self.t(f"workflow.action.{first.action_id}"), reason=first.message
            )
        self.captured.emit(result, ctx.saved_path or saved_path, error)

    def _on_captured(self, result: CaptureResult, saved_path: Path | None, error: str) -> None:  # GUI thread
        size = dict(width=result.image.width, height=result.image.height)
        if error:
            self.notify.emit(self.t("notify.failed.title"), error, True)
            return
        if self.app.settings.get("capture.show_preview"):
            self.show_preview(result, saved_path)
        elif saved_path is not None:
            self.notify.emit(
                self.t("notify.captured.title"), self.t("notify.captured.body", name=Path(saved_path).name, **size), False
            )
        else:
            self.notify.emit(self.t("notify.copied.title"), self.t("notify.copied.body", **size), False)

    # -- preview, pin, save ---------------------------------------------------
    def show_preview(self, result: CaptureResult, saved_path: Path | None) -> QWidget:
        from screenpilot.ui.preview import QuickPreview

        if self._preview is not None:
            try:
                self._preview.close()
            except RuntimeError:
                pass
        preview = QuickPreview(
            result,
            saved_path,
            self.app.translator,
            copy=lambda: self.copy_result(result),
            save_as=lambda: self.save_result_as(result),
            reveal=self.app.shell.reveal,
            temp_file=lambda: self.temp_file(result),
        )
        preview.pin_requested.connect(self.pin)
        preview.more_requested.connect(lambda r: self.start_scrolling(r.region))
        preview.closed.connect(lambda: setattr(self, "_preview", None) if self._preview is preview else None)
        self._preview = preview
        preview.show_animated()
        return preview

    def copy_result(self, result: CaptureResult) -> None:
        try:
            self.app.clipboard.set_image(result.image)
        except Exception as exc:  # noqa: BLE001
            log.exception("Copy failed")
            self.notify.emit(self.t("notify.failed.title"), str(exc), True)

    def save_result_as(self, result: CaptureResult) -> Path | None:
        suggested = self.app.output_dir / "capture.png"
        path, _ = QFileDialog.getSaveFileName(
            None, self.t("workflow.action.save_as"), str(suggested), "PNG (*.png);;JPEG (*.jpg);;BMP (*.bmp)"
        )
        if not path:
            return None
        target = Path(path)
        try:
            return result.image.save(target, image_format=target.suffix.lstrip(".") or "png")
        except OSError as exc:
            self.notify.emit(self.t("notify.failed.title"), str(exc), True)
            return None

    def temp_file(self, result: CaptureResult) -> Path | None:
        """A file for drag-and-drop when the capture was only copied."""
        folder = self.app.paths.data_dir / "drag"
        try:
            for old in folder.glob("*.png"):
                old.unlink(missing_ok=True)
            return result.image.save(folder / f"capture-{uuid.uuid4().hex[:8]}.png")
        except OSError:
            log.exception("Could not create drag file")
            return None

    def pin(self, result: CaptureResult) -> QWidget:
        from screenpilot.ui.preview import PinWindow
        from screenpilot.ui.qt_helpers import qimage_from_captured

        window = PinWindow(qimage_from_captured(result.image), self.app.translator, copy=lambda: self.copy_result(result))
        self.pins.append(window)
        window.destroyed.connect(lambda *_: self.pins.remove(window) if window in self.pins else None)
        window.move(physical_to_logical(Point(result.region.x, result.region.y), self.app.capture.monitors()))
        window.show()
        return window

    def _on_event(self, event: Event) -> None:
        if event.name == "editor.open_requested":
            self._not_available()
        elif event.name == "capture.scroll_progress" and self._scroll_indicator is not None:
            self._scroll_indicator.set_progress(event.payload["frames"], event.payload["height"])

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

    def show_hub(self) -> QWidget:
        from screenpilot.ui.hub import CaptureHub

        return self._show_single("hub", lambda: CaptureHub(self))

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
        for window in list(self._windows.values()) + list(self.pins):
            window.close()
        if self.edge_bar is not None:
            self.edge_bar.close()
            self.edge_bar = None
        if self._listener is not None:
            self._listener.stop()
        self._worker.shutdown(wait=True)
        self.bridge.close()
        self.retranslator.dispose()
        self.app.shutdown()

    def quit(self) -> None:
        self.shutdown()
        QApplication.quit()
