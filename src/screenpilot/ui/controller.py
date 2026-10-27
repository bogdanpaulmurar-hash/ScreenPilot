"""GUI controller: turns hotkeys and menu clicks into captures.

Threading model:
* captures run on the GUI thread (GDI grabs take tens of milliseconds);
* the post-capture workflow (PNG encoding, clipboard, scripts) runs on a
  single background worker so the UI never freezes on large captures;
* bus events and hotkeys reach the GUI thread through Qt signals.

Esc: while a capture flow runs (selector, countdowns, recording, interval
capture), the Esc key is polled with ``GetAsyncKeyState`` — one key, read on
demand, no hook — so Esc works even when another app has the keyboard focus.
Recording pauses and asks whether to continue; scrolling capture asks the same
from its worker (the scroller reads Esc itself); everything else is cancelled.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from screenpilot.app import ScreenPilotApp
from screenpilot.capture.detect import FrameFinder
from screenpilot.capture.geometry import Point, Rect
from screenpilot.capture.image import CaptureResult, stack_images
from screenpilot.core.errors import ScreenPilotError
from screenpilot.core.events import Event
from screenpilot.hotkeys import RegistrationStatus
from screenpilot.ui.countdown import CountdownOverlay
from screenpilot.ui.qt_helpers import EventBridge, Retranslator, present
from screenpilot.ui.region_selector import (
    ACTION_COLOR,
    ACTION_COPY,
    ACTION_EDIT,
    ACTION_INTERVAL,
    ACTION_MEASURE,
    ACTION_OCR,
    ACTION_PIN,
    ACTION_RECORD,
    ACTION_RECORD_GIF,
    ACTION_SAVE_AS,
    ACTION_SCROLL,
    ACTION_STEPS,
    RegionSelector,
    SelectorStyle,
    physical_to_logical,
)
from screenpilot.workflow import StepStatus

log = logging.getLogger(__name__)

MENU_SETTLE_MS = 250  # let the tray menu disappear before grabbing the screen
CHROME_SETTLE_MS = 150  # let our own floating bar disappear before grabbing
ESC_POLL_MS = 60
ESC_DEBOUNCE_S = 0.4  # an Esc the selector already handled must not cancel the next flow
MULTI_CLIPBOARD_DELAY_MS = 400  # between images copied one by one, so Windows' clipboard history keeps each
MULTI_EDITOR_GAP = 16  # px between the captures stacked in the editor


class AppController(QObject):
    hotkey_pressed = Signal(int)
    notify = Signal(str, str, bool)  # title, body, is_error
    notify_action = Signal(str, str, bool, object)  # title, body, is_error, callable run when the message is clicked
    hotkeys_changed = Signal()
    remote_command = Signal(object)  # dict forwarded by another start (DevPilot, CLI, shortcut)
    captured = Signal(object, object, str)  # CaptureResult, saved Path | None, error message
    ocr_finished = Signal(object, object, object)  # window, OcrResult | None, error str | None
    interval_changed = Signal()
    procedure_changed = Signal()
    scroll_finished = Signal(object, str, object)  # CaptureResult | None, reason, scroller
    backup_done = Signal(object, object)  # BackupResult | None, BackupError | None (automatic backup worker)
    scroll_ask = Signal(object)  # {"event": threading.Event, "answer": str} from the scrolling worker

    def __init__(self, app: ScreenPilotApp, *, register_hotkeys: bool = True) -> None:
        super().__init__()
        self.app = app
        self.t = app.translator.t
        self.retranslator = Retranslator(app.translator)
        self.bridge = EventBridge(app.bus, self)
        self.bridge.event.connect(self._on_event)
        self.selector = RegionSelector(app.translator, self)
        self.selector.finished.connect(self._on_region_selected)
        self.selector.clip_mode_changed.connect(self._on_clip_mode_changed)
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Workflow")
        self._windows: dict[str, QWidget] = {}
        self._countdown: CountdownOverlay | None = None
        self._listener = None
        self._preview: QWidget | None = None
        self.pins: list[QWidget] = []
        self.edge_bar: QWidget | None = None
        self.procedure = None  # ProcedureSession being recorded
        self._session_bar = None
        self._procedure_window = None
        self.scroller_factory: Callable[[], object] | None = None  # tests inject a fake
        self._scroll_indicator: QWidget | None = None
        self._region_default = "default"  # what Enter does in the selector ("scroll" from the hub tile)
        self.scroll_finished.connect(self._on_scroll_finished)
        self.scroll_ask.connect(self._on_scroll_ask)
        self.last_result: CaptureResult | None = None  # newest capture, also when only copied
        self._multi_copy: list[CaptureResult] | None = None  # captures still to copy one by one
        self._asking = False  # a "continue / stop" question is open
        self._last_esc_handled = 0.0
        self._esc_latch = None
        self._esc_timer = QTimer(self)
        self._esc_timer.setInterval(ESC_POLL_MS)
        self._esc_timer.timeout.connect(self._poll_escape)
        self.ocr_finished.connect(self._on_ocr_finished)
        self.ocr_engine_factory = None  # tests inject a fake engine
        self.hotkey_pressed.connect(self._on_hotkey)
        self.remote_command.connect(self.run_remote)
        self.captured.connect(self._on_captured)
        from screenpilot.ui.recording import RecordingController

        self.recording = RecordingController(self)

        self.actions: dict[str, Callable[[], None]] = {
            "capture.region": self.capture_region,
            "capture.window": self.capture_window,
            "capture.fullscreen": self.capture_current_monitor,
            "capture.all_monitors": self.capture_all_monitors,
            "capture.delayed": lambda: self.capture_delayed(self.app.settings.get("capture.default_delay_seconds")),
            "capture.repeat_last": self.repeat_last,
            "capture.scrolling": lambda: self.capture_region("capture.scrolling"),
            "capture.edit": lambda: self.capture_region("capture.edit"),
            "capture.steps": lambda: self.capture_region("capture.steps"),
            "capture.copy": lambda: self.capture_region("capture.copy"),
            "capture.pin": lambda: self.capture_region("capture.pin"),
            "tools.color": lambda: self.capture_region("tools.color"),
            "tools.measure": lambda: self.capture_region("tools.measure"),
            "capture.interval": self.ask_interval_capture,
            "procedure.toggle": self.toggle_procedure,
            "ocr.region": lambda: self.capture_region("ocr.region"),
            "app.open_editor": self.edit_last_capture,
            "app.open_history": self.show_history,
            "record.video": lambda: self.recording.toggle("mp4"),
            "record.gif": lambda: self.recording.toggle("gif"),
            "record.pause": lambda: self.recording.toggle_pause(),
            "record.zoom": lambda: self.recording.toggle_zoom(),
            "record.draw": lambda: self.recording.toggle_draw(),
            "app.edit_video": lambda: self.open_video_editor(),
            "app.backup_now": lambda: self.backup_now(),
        }
        self.backup_done.connect(self._on_backup_done)
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

    POLICY_FEATURES = {
        "capture.scrolling": "scrolling",
        "capture.pin": "pin",
        "ocr.region": "ocr",
        "record.video": "recording",
        "record.gif": "recording",
        "record.draw": "record_annotations",
        "app.edit_video": "video_edit",
    }

    def allowed(self, action: str) -> bool:
        feature = self.POLICY_FEATURES.get(action)
        return feature is None or self.app.policy.allows(feature)

    def trigger(self, action: str, *, from_menu: bool = False) -> None:
        if not self.allowed(action):
            self.notify.emit(self.t("app.name"), self.t("policy.feature_disabled"), True)
            return
        handler = self.actions.get(action)
        if handler is None:
            log.warning("No handler for action %s", action)
            return
        if action.startswith("capture.") or (action in ("record.video", "record.gif") and not self.recording.active):
            hidden = self.hide_chrome()
            if from_menu or hidden:
                QTimer.singleShot(MENU_SETTLE_MS if from_menu else CHROME_SETTLE_MS, handler)
                return
        handler()

    @property
    def busy(self) -> bool:
        """A capture flow is in progress (selector, countdown or scrolling)."""
        return (
            self.selector.active
            or self._countdown is not None
            or self._scroll_indicator is not None
            or self.recording.active
        )

    def hide_chrome(self) -> bool:
        """Hide ScreenPilot's own floating UI so it never appears in a capture."""
        hidden = False
        for bar in (self.edge_bar, self._session_bar):
            if bar is not None and bar.isVisible():
                bar.suspend()
                hidden = True
        window = self._procedure_window
        if window is not None and window.isVisible() and not window.isMinimized():
            window.showMinimized()
            hidden = True
        return hidden

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

    def selector_style(self, mode_id: str = "capture.region") -> SelectorStyle:
        from screenpilot.capture.profiles import BY_ID, mode_color

        s = self.app.settings
        mode = BY_ID.get(mode_id)
        return SelectorStyle(
            color=mode_color(s, mode_id),
            thickness=int(s.get("selector.thickness")),
            dim=int(s.get("selector.dim")),
            magnifier=bool(s.get("selector.magnifier")),
            mode_label=self.t(mode.label_key) if mode is not None and mode.id != "capture.region" else "",
            pick_color=mode_id == "tools.color",
            measure=mode_id == "tools.measure",
            action_bar=bool(s.get("selector.action_bar")),
            multi=mode is None or mode.action in ("default", "copy", "edit"),
        )

    def capture_region(self, mode_id: str = "capture.region") -> None:
        if self.selector.active:
            return
        from screenpilot.capture.profiles import BY_ID

        mode = BY_ID.get(mode_id, BY_ID["capture.region"])
        self._region_default = mode.action
        if mode.id == "capture.region" and self.app.settings.get("capture.region_action") == "copy":
            self._region_default = ACTION_COPY  # Region set to "copy to clipboard only"
        self._selector_started = time.perf_counter()
        try:
            frozen = self.app.capture.freeze()
            windows = self.app.capture.windows()
            monitors = self.app.capture.monitors()
        except ScreenPilotError as exc:
            self.notify.emit(self.t("notify.failed.title"), self.t(exc.message_key), True)
            return
        settings = self.app.settings
        self.selector.start(
            frozen, monitors, windows, self._frame_provider(frozen), self.selector_style(mode.id),
            clip_mode=settings.get("capture.multi_clipboard"),
            clip_locked=settings.is_locked("capture.multi_clipboard"),
        )
        QTimer.singleShot(0, self._selector_shown)
        self.watch_escape()

    def _on_clip_mode_changed(self, mode: str) -> None:
        """Switched in the selector: remembered, like the choice in Settings › Capture."""
        try:
            self.app.settings.set("capture.multi_clipboard", mode)
        except ScreenPilotError:
            log.info("capture.multi_clipboard is locked by policy")

    def _selector_shown(self) -> None:
        started = getattr(self, "_selector_started", None)
        if started is not None and self.selector.active:
            self.app.diagnostics.record("selector.open", (time.perf_counter() - started) * 1000)
        self._selector_started = None

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
            self._last_esc_handled = time.monotonic()
            return
        rect, action = outcome
        default, self._region_default = self._region_default, "default"
        if action == "default":
            action = default
        if isinstance(rect, list):
            self.capture_many(frozen, rect, action)
            return
        if action == ACTION_SCROLL:
            self.start_scrolling(rect)
            return
        if action == ACTION_COLOR:
            self._pick_color(frozen, rect)
            return
        if action == ACTION_MEASURE:
            diagonal = round((rect.width**2 + rect.height**2) ** 0.5)
            text = f"{rect.width} × {rect.height} px"
            self.copy_text(text)
            self.notify.emit(
                self.t("measure.copied.title"),
                self.t("measure.size", width=rect.width, height=rect.height, diagonal=diagonal),
                False,
            )
            return
        if action == ACTION_INTERVAL:
            self.start_interval(rect)
            return
        if action in (ACTION_RECORD, ACTION_RECORD_GIF):
            if not self.app.policy.allows("recording"):
                self.notify.emit(self.t("app.name"), self.t("policy.feature_disabled"), True)
                return
            self.recording.start(rect, "gif" if action == ACTION_RECORD_GIF else "mp4")
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
        elif action == ACTION_STEPS:
            self.open_steps(result)
        elif action == ACTION_EDIT:
            self.open_editor(result)
        elif action == ACTION_OCR:
            self.run_ocr(result)
        else:
            self.process(result)

    # -- several areas (Ctrl held in the selector) -----------------------------
    def capture_many(self, frozen: CaptureResult, rects: list[Rect], action: str) -> list[CaptureResult]:
        """Capture every area from the frozen desktop, then copy them or open the editor with all of them.

        "default" also runs the after-capture actions (saving, history) for each capture;
        the clipboard is always handled here, once for all: one by one or as one image."""
        results: list[CaptureResult] = []
        for rect in rects:
            try:
                result = self.app.capture.region_from_frozen(frozen, rect)
            except ScreenPilotError as exc:
                self.notify.emit(self.t("notify.failed.title"), self.t(exc.message_key), True)
                return []
            if self.app.settings.get("watermark.enabled"):
                from screenpilot.editor.watermark import apply_watermark

                result = apply_watermark(result, self.app.settings)
            results.append(result)
        if not results:
            return results
        self.last_result = results[-1]
        if action == ACTION_EDIT:
            self.open_editor_many(results)
            return results
        if action != ACTION_COPY:
            steps = [s for s in self.app.settings.get("workflow.after_capture") if s.get("id") != "copy_image"]
            for result in results:
                if steps:
                    self.process(result, steps=steps, quiet=True)
                if self.procedure is not None:
                    self._add_procedure_step(result)
        self.copy_many(results)
        return results

    def copy_many(self, results: list[CaptureResult]) -> None:
        """Clipboard for several captures: one by one (each lands in Win+V), or stacked into one image."""
        count = len(results)
        mode = "combined" if self.app.settings.get("capture.multi_clipboard") == "combined" else "sequence"

        def done() -> None:
            self.notify.emit(self.t("notify.multi.title", count=count), self.t(f"notify.multi.body.{mode}"), False)

        if mode == "combined":
            try:
                image = stack_images([r.image for r in results], gap=MULTI_EDITOR_GAP)
            except Exception as exc:  # noqa: BLE001
                log.exception("Combining captures failed")
                self.notify.emit(self.t("notify.failed.title"), str(exc), True)
                return
            self.copy_result(CaptureResult(image, results[0].mode, results[0].region))
            done()
            return
        pending = list(results)
        self._multi_copy = pending  # keeps the chain alive; a new multi-capture replaces it

        def next_image() -> None:
            if self._multi_copy is not pending or not pending:
                return
            self.copy_result(pending.pop(0))
            if pending:
                QTimer.singleShot(MULTI_CLIPBOARD_DELAY_MS, next_image)
            else:
                self._multi_copy = None
                done()

        next_image()

    def open_editor_many(self, results: list[CaptureResult]) -> QWidget:
        """Editor with the first capture as the fixed background and the others as movable pictures below it."""
        editor = self.open_editor(results[0])
        if len(results) > 1:
            editor.attach_below([r.image for r in results[1:]], gap=MULTI_EDITOR_GAP)
        return editor

    def _pick_color(self, frozen: CaptureResult, rect: Rect) -> None:
        x, y = rect.x - frozen.region.x, rect.y - frozen.region.y
        r, g, b = frozen.image.pixel(x, y)
        hex_value = f"#{r:02X}{g:02X}{b:02X}"
        self.copy_text(hex_value)
        self.last_color = hex_value
        self.notify.emit(self.t("color.copied.title"), self.t("color.copied.body", hex=hex_value, r=r, g=g, b=b), False)

    # -- interval capture -----------------------------------------------------
    def ask_interval_capture(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        seconds, ok = QInputDialog.getInt(None, self.t("interval.title"), self.t("interval.every"), 10, 1, 3600)
        if not ok:
            return
        count, ok = QInputDialog.getInt(None, self.t("interval.title"), self.t("interval.count"), 10, 0, 10000)
        if not ok:
            return
        self._interval_plan = (seconds, count)
        self.capture_region("capture.interval")

    def start_interval(self, rect: Rect, seconds: int | None = None, count: int | None = None) -> None:
        plan_seconds, plan_count = getattr(self, "_interval_plan", (10, 10))
        seconds = seconds or plan_seconds
        count = plan_count if count is None else count
        self.stop_interval()
        self.interval = {"rect": rect, "seconds": seconds, "count": count, "done": 0}
        self._interval_timer = QTimer(self)
        self._interval_timer.setInterval(int(seconds * 1000))
        self._interval_timer.timeout.connect(self._interval_tick)
        self._interval_timer.start()
        self._interval_tick()
        self.watch_escape()

    def _interval_tick(self) -> None:
        state = getattr(self, "interval", None)
        if not state:
            return
        try:
            result = self.app.capture.capture_region(state["rect"])
        except ScreenPilotError as exc:
            self.notify.emit(self.t("notify.failed.title"), self.t(exc.message_key), True)
            self.stop_interval()
            return
        self.process(result, quiet=True)
        state["done"] += 1
        self.interval_changed.emit()
        if state["count"] and state["done"] >= state["count"]:
            done = state["done"]
            self.stop_interval()
            self.notify.emit(self.t("interval.title"), self.t("interval.finished", done=done), False)

    def stop_interval(self) -> None:
        timer = getattr(self, "_interval_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._interval_timer = None
        self.interval = None
        self.interval_changed.emit()

    # -- procedure recording --------------------------------------------------
    def toggle_procedure(self) -> None:
        if self.procedure is None:
            self.start_procedure()
        else:
            self.finish_procedure()

    def start_procedure(self, title: str = "") -> None:
        from screenpilot.docs import ProcedureSession
        from screenpilot.ui.procedure import SessionBar

        if self.procedure is not None:
            return
        import getpass

        try:
            author = getpass.getuser()
        except Exception:  # noqa: BLE001
            author = ""
        self.procedure = ProcedureSession.create(self.app.paths.data_dir / "procedures", title=title, author=author)
        bar = SessionBar(self.app.translator, lambda: self.busy)
        bar.finish_clicked.connect(self.finish_procedure)
        bar.cancel_clicked.connect(self.cancel_procedure)
        self.retranslator.on_change(lambda: bar.retranslate() if self._session_bar is bar else None)
        self._session_bar = bar
        bar.show_bar()
        self.procedure_changed.emit()
        self.notify.emit(self.t("procedure.window"), self.t("procedure.started"), False)

    def _add_procedure_step(self, result: CaptureResult) -> None:
        session = self.procedure
        source = str(result.metadata.get("window_title", "")) or self.t(f"capture.mode.{result.mode}")
        session.add(result.image, source=source)
        if self._session_bar is not None:
            self._session_bar.set_count(len(session.steps))
        window = self._procedure_window
        if window is not None and window.session is session:
            window.step_added()
            window.showNormal()
        if getattr(self, "_one_shot_procedure", False):
            self._one_shot_procedure = False
            self.procedure = None
        self.procedure_changed.emit()

    def finish_procedure(self) -> QWidget | None:
        session = self.procedure
        if session is None:
            return None
        self.procedure = None
        if self._session_bar is not None:
            self._session_bar.close()
            self._session_bar = None
        self.procedure_changed.emit()
        return self.open_procedure(session)

    def cancel_procedure(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        session = self.procedure
        if session is None:
            return
        if session.steps:
            answer = QMessageBox.question(
                None, self.t("procedure.cancel"), self.t("procedure.confirm_discard", count=len(session.steps))
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.procedure = None
        if self._session_bar is not None:
            self._session_bar.close()
            self._session_bar = None
        session.discard()
        self.procedure_changed.emit()

    def open_procedure(self, session) -> QWidget:
        from screenpilot.ui.procedure import ProcedureWindow

        if isinstance(session, Path):
            from screenpilot.docs import ProcedureSession

            session = ProcedureSession.load(session)
        window = ProcedureWindow(session, self.app.translator, save_dir=self.app.output_dir, reveal=self.app.shell.reveal)
        window.add_capture.connect(lambda: self._procedure_add_capture(window))
        window.edit_image.connect(lambda row: self._procedure_edit_image(window, row))
        key = f"procedure-{id(window)}"
        self._windows[key] = window
        window.destroyed.connect(lambda *_: self._windows.pop(key, None))
        window.destroyed.connect(lambda *_: setattr(self, "_procedure_window", None) if self._procedure_window is window else None)
        self._procedure_window = window
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def _procedure_add_capture(self, window) -> None:
        """Capture one more step straight into an open procedure."""
        self.procedure = window.session
        self._one_shot_procedure = True
        window.showMinimized()
        QTimer.singleShot(MENU_SETTLE_MS, self.capture_region)

    def _procedure_edit_image(self, window, row: int) -> None:
        from PIL import Image

        from screenpilot.capture.image import CapturedImage

        if not 0 <= row < len(window.session.steps):
            return
        step = window.session.steps[row]
        with Image.open(window.session.image_path(step)) as img:
            image = CapturedImage.from_pil(img)
        result = CaptureResult(image, "region", Rect(0, 0, image.width, image.height))

        def done(edited: CaptureResult) -> None:
            window.session.replace_image(row, edited.image)
            window.reload(keep_row=True)

        from screenpilot.ui.editor_window import EditorWindow

        editor = EditorWindow(
            result,
            self.app.translator,
            on_done=done,
            copy=lambda img: self.copy_result(CaptureResult(img, "region", result.region)),
            save_dir=self.app.output_dir,
            settings=self.app.settings,
            ocr_engine=self.ocr_engine,
            recent_images=self.recent_images,
            on_copy_close=done,  # the step keeps the edit too
        )
        key = f"editor-{id(editor)}"
        self._windows[key] = editor
        editor.destroyed.connect(lambda *_: self._windows.pop(key, None))
        present(editor)  # in front of the procedure window, with keyboard focus

    def capture_region_then_scroll(self) -> None:
        """Selector where Enter / click starts a scrolling capture."""
        self.capture_region("capture.scrolling")

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
        if not self.app.policy.allows("scrolling"):
            self.notify.emit(self.t("app.name"), self.t("policy.feature_disabled"), True)
            return
        try:
            scroller = self._make_scroller()
        except ScreenPilotError as exc:
            self.notify.emit(self.t("notify.failed.title"), self.t(exc.message_key), True)
            return
        self.hide_chrome()
        from screenpilot.ui.preview import ScrollIndicator

        manual = options.setdefault("manual", self.app.settings.get("capture.scroll_mode") == "manual")
        monitors = self.app.capture.monitors()
        anchor = physical_to_logical(Point(rect.x, rect.bottom), monitors)
        top = physical_to_logical(Point(rect.x, rect.y), monitors).y()
        indicator = ScrollIndicator(self.app.translator, anchor, manual=manual, top=top)
        self._scroll_indicator = indicator
        indicator.show_near()
        # OK on the pill finishes like Esc (keeps what was captured, no question);
        # hovering it pauses automatic scrolling so the cursor stays on the button
        options.setdefault("finish", indicator.finished.is_set)
        options.setdefault("hold", indicator.hovered.is_set)
        if manual:
            scroller.restore_cursor = lambda: None  # the user moved the mouse; leave it where it is
        else:
            options.setdefault("on_stop", self._ask_scroll)

        def run() -> None:
            try:
                with self.app.diagnostics.timed("scroll.capture") as info:
                    result, reason = self.app.capture.capture_scrolling(rect, scroller, **options)
                    info.update(reason=reason, height=result.image.height if result is not None else 0)
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
        if reason == "cancelled":
            self.notify.emit(self.t("action.capture.scrolling"), self.t("scroll.cancelled"), False)
            return
        if result is None:
            self.notify.emit(self.t("notify.failed.title"), reason, True)
            return
        if reason == "no_match":
            self.notify.emit(self.t("action.capture.scrolling"), self.t("scroll.no_match"), False)
        elif reason == "end" and result.image.height <= result.metadata.get("view_height", 0):
            self.notify.emit(self.t("action.capture.scrolling"), self.t("scroll.not_scrollable"), False)
        self.process(result)

    def _ask_scroll(self) -> str:
        """Worker thread: Esc paused the scrolling capture; ask on the GUI thread and wait."""
        box = {"event": threading.Event(), "answer": "stop"}
        self.scroll_ask.emit(box)
        if not box["event"].wait(600):
            return "stop"
        return box["answer"]

    def _on_scroll_ask(self, box: dict) -> None:
        if self._scroll_indicator is not None:
            self._scroll_indicator.hide()
        try:
            box["answer"] = self.ask_continue("scroll.paused.title", "scroll.paused.body", stop_key="scroll.paused.stop")
        finally:
            if self._scroll_indicator is not None:
                self._scroll_indicator.show()
            box["event"].set()

    def ask_continue(self, title_key: str, body_key: str, *, stop_key: str) -> str:
        """Modal question: "continue", "stop" (keep what was captured) or "cancel" (discard)."""
        from PySide6.QtWidgets import QMessageBox

        from screenpilot.platform.win32_window import force_foreground

        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(self.t(title_key))
        box.setText(self.t(body_key))
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        keep = box.addButton(self.t("flow.continue"), QMessageBox.ButtonRole.AcceptRole)
        stop = box.addButton(self.t(stop_key), QMessageBox.ButtonRole.ActionRole)
        discard = box.addButton(self.t("flow.discard"), QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)  # a second Esc just closes the question and continues
        self._asking = True
        try:
            QTimer.singleShot(0, lambda: force_foreground(int(box.winId())))
            box.exec()
        finally:
            self._asking = False
            if self._esc_latch is not None:
                self._esc_latch.reset()
        clicked = box.clickedButton()
        if clicked is stop:
            return "stop"
        if clicked is discard:
            return "cancel"
        return "continue"

    # -- Esc ------------------------------------------------------------------
    def watch_escape(self) -> None:
        """Start polling Esc while a flow runs (stops by itself when nothing is active)."""
        if sys.platform != "win32" or self._esc_timer.isActive():
            return
        from screenpilot.platform.keys import KeyLatch
        from screenpilot.platform.win32_window import VK_ESCAPE, key_down

        self._esc_latch = KeyLatch(lambda: key_down(VK_ESCAPE))
        self._esc_timer.start()

    def _escape_flow_active(self) -> bool:
        return (
            self.selector.active
            or self._countdown is not None
            or self.recording.active
            or getattr(self, "interval", None) is not None
            or self._scroll_indicator is not None
            or self._preview_visible()
        )

    def _preview_visible(self) -> bool:
        preview = self._preview
        try:
            return preview is not None and preview.isVisible()
        except RuntimeError:  # deleted
            return False

    def _poll_escape(self) -> None:
        if not self._escape_flow_active():
            self._esc_timer.stop()
            return
        if self._esc_latch is not None and self._esc_latch.poll():
            self.escape_pressed()

    def escape_pressed(self) -> str:
        """Handle Esc for the topmost running flow; returns what was done (for logs and tests)."""
        if self._asking:
            return "ignored"
        if time.monotonic() - self._last_esc_handled < ESC_DEBOUNCE_S:
            return "ignored"  # the selector already handled this press
        self._last_esc_handled = time.monotonic()
        if self.selector.active:
            self.selector.cancel()
            done = "selector"
        elif self._countdown is not None:
            self._countdown.cancel()
            done = "countdown"
        elif self._scroll_indicator is not None:
            done = "scrolling"  # the scrolling worker reads Esc itself and asks
        elif self.recording.active:
            done = self.recording.escape()
        elif getattr(self, "interval", None) is not None:
            count = self.interval["done"]
            self.stop_interval()
            self.notify.emit(self.t("interval.title"), self.t("interval.finished", done=count), False)
            done = "interval"
        elif self._preview_visible():
            self._preview.dismiss()  # back to the normal screen right away
            done = "preview"
        else:
            done = "none"
        log.info("Esc: %s", done)
        return done

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
        self.watch_escape()

    def _after_countdown(self, then: Callable[[], None]) -> None:
        self._countdown = None
        then()

    def _countdown_cancelled(self) -> None:
        self._countdown = None
        log.info("Delayed capture cancelled")

    # -- post capture ---------------------------------------------------------
    def process(
        self, result: CaptureResult, steps: list[dict] | None = None, *, saved_path: Path | None = None, quiet: bool = False
    ) -> None:
        """Run the configured (or given) action chain in the background."""
        future = self._worker.submit(self.app.run_after_capture, result, steps)
        future.add_done_callback(lambda f: self._workflow_done(f, result, saved_path, quiet))

    def _workflow_done(self, future, result: CaptureResult, saved_path: Path | None, quiet: bool = False) -> None:
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
        if quiet and not error:
            return
        self.captured.emit(result, ctx.saved_path or saved_path, error)

    def _on_captured(self, result: CaptureResult, saved_path: Path | None, error: str) -> None:  # GUI thread
        size = dict(width=result.image.width, height=result.image.height)
        if self.procedure is not None and not error:
            self._add_procedure_step(result)
        if not error:
            self.last_result = result
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
        preview.steps_requested.connect(self.open_steps)
        preview.edit_requested.connect(self.open_editor)
        preview.closed.connect(lambda: setattr(self, "_preview", None) if self._preview is preview else None)
        self._preview = preview
        preview.show_animated()
        self.watch_escape()  # Esc dismisses it, wherever the keyboard focus is
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

    def open_editor(self, result: CaptureResult) -> QWidget:
        """Image editor; Done runs the usual after-capture actions."""
        from screenpilot.ui.editor_window import EditorWindow

        editor = EditorWindow(
            result,
            self.app.translator,
            on_done=self.process,
            copy=lambda image: self.copy_result(CaptureResult(image, result.mode, result.region)),
            save_dir=self.app.output_dir,
            settings=self.app.settings,
            ocr_engine=self.ocr_engine,
            recent_images=self.recent_images,
        )
        key = f"editor-{id(editor)}"
        self._windows[key] = editor
        editor.destroyed.connect(lambda *_: self._windows.pop(key, None))
        present(editor)
        return editor

    def recent_images(self, count: int = 15) -> list[tuple[str, str, str]]:
        """(label, path, thumbnail) of recent captures, for the editor's Insert picture menu."""
        items = []
        for entry in self.app.history.entries()[:count]:
            when = entry.created.astimezone().strftime("%d.%m %H:%M")
            items.append((f"{when} · {entry.width}×{entry.height}", entry.path, entry.thumbnail))
        return items

    def edit_history_entry(self, entry) -> QWidget | None:
        """Open a capture from the history in the editor."""
        result = self.load_history_capture(entry)
        return self.open_editor(result) if result is not None else None

    def edit_file(self, path: Path | str | None = None, parent: QWidget | None = None) -> QWidget | None:
        """Open any image file in the editor (asks for one when ``path`` is None)."""
        if path is None:
            result = self.open_image_file(parent)
        else:
            result = self.load_image_file(Path(path))
        return self.open_editor(result) if result is not None else None

    def load_image_file(self, path: Path, mode: str = "region") -> CaptureResult | None:
        from PIL import Image

        from screenpilot.capture.image import CapturedImage

        try:
            with Image.open(path) as img:
                image = CapturedImage.from_pil(img.convert("RGB"))
        except OSError as exc:
            self.notify.emit(self.t("notify.failed.title"), str(exc), True)
            return None
        return CaptureResult(image, mode, Rect(0, 0, image.width, image.height), metadata={"source": str(path)})

    def last_capture(self) -> CaptureResult | None:
        """The newest capture: the one taken in this session (even if only
        copied), else the newest one in the history."""
        if self.last_result is not None:
            return self.last_result
        entries = self.app.history.entries()
        return self.load_history_capture(entries[0]) if entries else None

    def edit_last_capture(self) -> QWidget | None:
        """Open the editor without a fresh capture ("Open editor" action).

        ``editor.open_mode``: ``"ask"`` shows the picker (last capture, the 10
        most recent, clipboard, a file, or the full history); ``"last"`` opens
        the newest capture straight away.
        """
        if self.app.settings.get("editor.open_mode") == "ask":
            return self.show_editor_picker()
        result = self.last_capture()
        if result is None:
            return self.show_editor_picker()
        return self.open_editor(result)

    def show_editor_picker(self) -> QWidget:
        from screenpilot.ui.editor_picker import EditorPicker

        return self._show_single("editor_picker", lambda: EditorPicker(self))

    def open_image_file(self, parent: QWidget | None = None) -> CaptureResult | None:
        from PIL import Image

        from screenpilot.capture.image import CapturedImage

        path, _ = QFileDialog.getOpenFileName(
            parent, self.t("editor.pick.file"), str(self.app.output_dir), "Images (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if not path:
            return None
        try:
            with Image.open(path) as img:
                image = CapturedImage.from_pil(img)
        except OSError as exc:
            self.notify.emit(self.t("notify.failed.title"), str(exc), True)
            return None
        return CaptureResult(image, "region", Rect(0, 0, image.width, image.height), metadata={"source": path})

    def clipboard_capture(self) -> CaptureResult | None:
        """The image currently on the clipboard (copied from any app), if any."""
        from PySide6.QtGui import QGuiApplication, QImage

        from screenpilot.capture.image import CapturedImage

        qimg = QGuiApplication.clipboard().image()
        if qimg.isNull():
            return None
        qimg = qimg.convertToFormat(QImage.Format.Format_RGB32)
        w, h = qimg.width(), qimg.height()
        data = bytes(qimg.constBits())[: qimg.bytesPerLine() * h]
        if qimg.bytesPerLine() != w * 4:  # defensive: drop row padding
            data = b"".join(data[y * qimg.bytesPerLine() : y * qimg.bytesPerLine() + w * 4] for y in range(h))
        image = CapturedImage(w, h, data)
        return CaptureResult(image, "region", Rect(0, 0, w, h), metadata={"source": "clipboard"})

    def load_history_capture(self, entry) -> CaptureResult | None:
        from PIL import Image

        from screenpilot.capture.image import CapturedImage

        try:
            with Image.open(entry.path) as img:
                image = CapturedImage.from_pil(img)
        except OSError as exc:
            self.notify.emit(self.t("notify.failed.title"), str(exc), True)
            return None
        return CaptureResult(image, entry.mode, Rect(0, 0, image.width, image.height), metadata={"source": entry.path})

    def ocr_engine(self):
        if self.ocr_engine_factory is not None:
            return self.ocr_engine_factory()
        from screenpilot.ocr import default_engine

        return default_engine(self.app.settings.get("ocr.engine"))

    def run_ocr(self, result: CaptureResult, language: str | None = None, window=None) -> QWidget | None:
        """Recognize text in the background; copy it and/or show the OCR window."""
        from screenpilot.ui.ocr_window import OcrWindow

        language = language or self.app.settings.get("ocr.language")
        if window is None and self.app.settings.get("ocr.show_window"):
            window = OcrWindow(
                result, self.app.translator, copy_text=self.copy_text, save_dir=self.app.output_dir, language=language
            )
            window.rerun_requested.connect(lambda lang, w=window: self.run_ocr(result, lang, w))
            key = f"ocr-{id(window)}"
            self._windows[key] = window
            window.destroyed.connect(lambda *_: self._windows.pop(key, None))
            present(window)
        elif window is None:
            self.notify.emit(self.t("ocr.title"), self.t("ocr.running"), False)
        if window is not None:
            window.set_running()

        def work() -> None:
            try:
                engine = self.ocr_engine()
                ocr = engine.recognize(result.image, None if language == "auto" else language)
            except Exception as exc:  # noqa: BLE001
                log.exception("OCR failed")
                message = self.t(getattr(exc, "message_key", "error.ocr"))
                self.ocr_finished.emit(window, None, message)
                return
            self.ocr_finished.emit(window, ocr, None)

        threading.Thread(target=work, name="OCR", daemon=True).start()
        return window

    def _on_ocr_finished(self, window, ocr, error: str | None) -> None:
        alive = window is not None and window in self._windows.values()
        if error:
            if alive:
                window.set_error(error)
            self.notify.emit(self.t("notify.failed.title"), error, True)
            return
        if alive:
            window.set_result(ocr)
        if self.app.settings.get("ocr.auto_copy") and ocr.text.strip():
            self.copy_text(ocr.text)
            self.notify.emit(self.t("ocr.title"), self.t("ocr.copied", count=len(ocr.lines)), False)
        elif not ocr.text.strip() and not alive:
            self.notify.emit(self.t("ocr.title"), self.t("ocr.empty"), False)
        self.last_ocr = ocr

    def copy_text(self, text: str) -> None:
        try:
            self.app.clipboard.set_text(text)
        except Exception as exc:  # noqa: BLE001
            log.exception("Copy text failed")
            self.notify.emit(self.t("notify.failed.title"), str(exc), True)

    def open_steps(self, result: CaptureResult) -> QWidget:
        """Step numbering on a capture; Done runs the usual after-capture actions."""
        from screenpilot.ui.step_editor import StepEditor

        editor = StepEditor(
            result,
            self.app.translator,
            on_done=self.process,
            copy=lambda image: self.copy_result(CaptureResult(image, result.mode, result.region)),
            save_dir=self.app.output_dir,
        )
        self._windows[f"steps-{id(editor)}"] = editor
        editor.destroyed.connect(lambda *_: self._windows.pop(f"steps-{id(editor)}", None))
        present(editor)
        return editor

    def pin(self, result: CaptureResult) -> QWidget:
        from screenpilot.ui.preview import PinWindow
        from screenpilot.ui.qt_helpers import qimage_from_captured

        window = PinWindow(qimage_from_captured(result.image), self.app.translator, copy=lambda: self.copy_result(result))
        self.pins.append(window)
        window.destroyed.connect(lambda *_: self.pins.remove(window) if window in self.pins else None)
        window.move(physical_to_logical(Point(result.region.x, result.region.y), self.app.capture.monitors()))
        present(window)  # Esc closes it right away
        return window

    def _on_event(self, event: Event) -> None:
        if event.name.startswith("recording."):
            self.recording.on_event(event)
        elif event.name == "editor.open_requested":
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
        present(window)
        return window

    def show_hub(self) -> QWidget:
        from screenpilot.ui.hub import CaptureHub

        return self._show_single("hub", lambda: CaptureHub(self))

    def show_settings(self, page: str | None = None) -> QWidget:
        from screenpilot.ui.settings_dialog import SettingsDialog

        dialog = self._show_single("settings", lambda: SettingsDialog(self))
        if page:
            dialog.show_page(page)
        return dialog

    def open_video_editor(self, path: Path | None = None) -> QWidget | None:
        """Edit a recording: trim it and add timed annotations (a new file is saved)."""
        if not self.app.policy.allows("video_edit"):
            self.notify.emit(self.t("app.name"), self.t("policy.feature_disabled"), True)
            return None
        if path is None:
            folder = self.app.recording.output_dir() if self.app.recording else self.app.output_dir
            chosen, _ = QFileDialog.getOpenFileName(
                None, self.t("video.pick"), str(folder), self.t("video.pick.filter") + " (*.mp4 *.mkv *.mov *.avi *.webm *.gif)"
            )
            if not chosen:
                return None
            path = Path(chosen)
        try:
            project = self.app.video.open(Path(path))
        except ScreenPilotError as exc:
            self.notify_action.emit(
                self.t("notify.failed.title"), self.t(exc.message_key), True,
                (lambda: self.show_settings("recording")) if exc.message_key == "recording.error.no_ffmpeg" else None,
            )
            return None
        from screenpilot.ui.video_editor import VideoEditorWindow

        window = VideoEditorWindow(self.app, project, reveal=self.app.shell.reveal)
        window.exported.connect(lambda out: self.notify.emit(self.t("video.title", name=Path(out).name), self.t("video.exported", path=out), False))
        key = f"video:{Path(path).resolve()}"
        self._windows[key] = window
        window.destroyed.connect(lambda *_: self._windows.pop(key, None))
        present(window)
        return window

    def customize_edge_bar(self, parent: QWidget | None = None) -> QWidget:
        """Pick and order the buttons of the capture bar at the top of the screen."""
        from screenpilot.ui.edge_bar_editor import EdgeBarEditor

        dialog = EdgeBarEditor(self.app, parent)
        present(dialog)
        self._windows["edge_bar_editor"] = dialog
        dialog.destroyed.connect(lambda *_: self._windows.pop("edge_bar_editor", None))
        return dialog

    def show_history(self) -> QWidget:
        from screenpilot.ui.history_window import HistoryWindow

        return self._show_single("history", lambda: HistoryWindow(self))

    def show_wizard(self) -> QWidget:
        from screenpilot.ui.wizard import FirstRunWizard

        return self._show_single("wizard", lambda: FirstRunWizard(self))

    def export_diagnostics(self) -> Path | None:
        if not self.app.policy.allows("export_diagnostics"):
            self.notify.emit(self.t("app.name"), self.t("policy.feature_disabled"), True)
            return None
        from datetime import datetime

        suggested = Path.home() / "Desktop" / f"ScreenPilot-diagnostics-{datetime.now():%Y%m%d-%H%M}.zip"
        path, _ = QFileDialog.getSaveFileName(None, self.t("diag.export"), str(suggested), "ZIP (*.zip)")
        if not path:
            return None
        try:
            target = self.app.export_diagnostics(Path(path))
        except OSError as exc:
            self.notify.emit(self.t("notify.failed.title"), str(exc), True)
            return None
        self.notify.emit(self.t("diag.export"), self.t("diag.exported", path=target), False)
        try:
            self.app.shell.reveal(target)
        except OSError:
            pass
        return target

    # -- backup -------------------------------------------------------------------
    def backup_now(self) -> "Path | None":
        """Tray, Settings, shortcut, DevPilot: write a backup now and say where."""
        from screenpilot.app import backup as app_backup
        from screenpilot.core.backup import BackupError

        try:
            result = app_backup.backup_now(self.app)
        except BackupError as exc:
            log.warning("Backup failed: %s", exc)
            self.notify.emit(self.t("backup.failed"), self.t(exc.message_key, details=str(exc)), True)
            return None
        self.notify_action.emit(
            self.t("backup.done"), self.t("backup.done.body", name=result.path.name), False,
            lambda: self._reveal(result.path),
        )
        return result.path

    def auto_backup(self) -> threading.Thread | None:
        """Start-up backup ("once a day" / "every start"), written in the background."""
        from screenpilot.app import backup as app_backup
        from screenpilot.core.backup import BackupError

        if not app_backup.is_due(self.app):
            return None
        settings, hotkeys = self.app.settings.as_dict(), self.app.hotkeys.export_profile()

        def work() -> None:
            from screenpilot.core import backup as core

            try:
                result = core.create_backup(
                    self.app.paths, app_backup.folder(self.app), settings_data=settings,
                    hotkeys_data=hotkeys, keep=app_backup.keep(self.app),
                )
            except BackupError as exc:
                self.backup_done.emit(None, exc)
            except Exception as exc:  # noqa: BLE001 - never crash the start-up
                log.exception("Automatic backup failed")
                self.backup_done.emit(None, BackupError(str(exc)))
            else:
                self.backup_done.emit(result, None)

        worker = threading.Thread(target=work, name="auto-backup", daemon=True)
        worker.start()
        return worker

    def _on_backup_done(self, result, error) -> None:
        from screenpilot.app import backup as app_backup

        if error is not None:
            log.warning("Automatic backup failed: %s", error)
            self.notify.emit(self.t("backup.failed"), self.t(error.message_key, details=str(error)), True)
            return
        app_backup.remember(self.app, result)

    def restore_backup(self, parent: QWidget | None = None, path: "Path | None" = None) -> bool:
        """Pick a backup, show what would change, restore after confirmation."""
        from PySide6.QtWidgets import QMessageBox

        from screenpilot.app import backup as app_backup
        from screenpilot.core.backup import BackupError

        title = self.t("backup.restore.title")
        if path is None:
            start = app_backup.folder(self.app)
            chosen, _ = QFileDialog.getOpenFileName(
                parent, title, str(start if start.is_dir() else Path.home()), self.t("backup.filter") + " (*.zip)"
            )
            if not chosen:
                return False
            path = Path(chosen)
        try:
            plan = app_backup.plan_restore(self.app, Path(path))
        except BackupError as exc:
            QMessageBox.warning(parent, title, self.t(exc.message_key, details=str(exc)))
            return False
        summary, details = self.describe_restore(plan)
        if not plan.has_changes:
            QMessageBox.information(parent, title, summary)
            return False
        if not self.confirm_restore(parent, summary, details):
            return False
        try:
            done = app_backup.restore(self.app, plan)
        except BackupError as exc:
            QMessageBox.warning(parent, title, self.t(exc.message_key, details=str(exc)))
            return False
        if done.hotkeys_changed:
            try:
                self.app.hotkeys.save(self.app.paths.hotkeys_file)
            except OSError:
                log.exception("Could not save restored hotkeys")
            self.hotkeys_changed.emit()
        self.notify.emit(title, self.t("backup.restored", safety=done.safety.name), False)
        return True

    def confirm_restore(self, parent: QWidget | None, summary: str, details: str) -> bool:
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(QMessageBox.Icon.Question, self.t("backup.restore.title"), summary,
                          QMessageBox.StandardButton.NoButton, parent)
        restore = box.addButton(self.t("backup.restore.confirm"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(restore)
        if details:
            box.setDetailedText(details)
        box.exec()
        return box.clickedButton() is restore

    def describe_restore(self, plan) -> tuple[str, str]:
        """Short preview for the confirmation and the full list for "Show details"."""
        t = self.t
        created = plan.contents.created.replace("T", " ")[:16]
        lines = [t("backup.preview.from", name=plan.contents.path.name, date=created,
                   version=plan.contents.manifest.get("app_version", "?"))]
        if not plan.has_changes:
            lines.append(t("backup.preview.nothing"))
        if plan.settings_changes:
            lines.append(t("backup.preview.settings", count=len(plan.settings_changes)))
        if plan.hotkey_changes:
            lines.append(t("backup.preview.hotkeys", count=len(plan.hotkey_changes)))
        if plan.history_added:
            lines.append(t("backup.preview.history", count=len(plan.history_added)))
        if plan.procedures_new:
            lines.append(t("backup.preview.procedures", count=len(plan.procedures_new)))
        if plan.history_missing:
            lines.append(t("backup.preview.missing", count=len(plan.history_missing)))
        if plan.remapped and plan.has_changes:
            old, new = plan.remapped[-1]
            lines.append(t("backup.preview.paths", old=old, new=new))
        if plan.has_changes:
            lines.append(t("backup.preview.safety"))
        details: list[str] = []
        if plan.settings_changes:
            details.append(t("backup.details.settings"))
            details += [f"  {key}" for key in plan.settings_changes]
        if plan.hotkey_changes:
            details.append(t("backup.details.hotkeys"))
            details += [f"  {t('action.' + a) if self.app.translator.has('action.' + a) else a}" for a in plan.hotkey_changes]
        if plan.history_missing:
            details.append(t("backup.details.missing"))
            details += [f"  {p}" for p in plan.history_missing]
        return "\n".join(lines), "\n".join(details)

    def _reveal(self, path: Path) -> None:
        try:
            self.app.shell.reveal(path)
        except OSError:
            log.exception("Could not open %s", path)

    def open_log_folder(self) -> None:
        try:
            self.app.shell.reveal(self.app.paths.log_dir)
        except OSError:
            log.exception("Could not open log folder")

    def open_captures_folder(self) -> None:
        folder = self.app.output_dir
        folder.mkdir(parents=True, exist_ok=True)
        try:
            self.app.shell.reveal(folder)
        except OSError:
            log.exception("Could not open %s", folder)

    # -- lifecycle ------------------------------------------------------------
    # -- commands from other processes ------------------------------------------
    REMOTE_EXTRAS = ("app.show_hub", "app.settings", "app.quit")

    def remote_actions(self) -> list[str]:
        return sorted(set(self.actions) | set(self.REMOTE_EXTRAS))

    def accept_remote(self, message: dict) -> dict:
        """Runs on the instance-server thread: validate, then hand over to the GUI thread."""
        if message.get("cmd") != "action":
            return {"ok": False, "error": "unknown command"}
        action = str(message.get("action", ""))
        if action not in self.remote_actions():
            return {"ok": False, "error": f"unknown action {action!r}", "actions": self.remote_actions()}
        self.remote_command.emit(message)
        return {"ok": True}

    def run_remote(self, message: dict) -> None:
        action = str(message.get("action", ""))
        args = message.get("args") or {}
        log.info("Forwarded action: %s %s", action, args or "")
        if action == "app.show_hub":
            self.show_hub()
        elif action == "app.settings":
            self.show_settings(args.get("page"))
        elif action == "app.quit":
            self.quit()
        elif action == "capture.delayed" and args.get("seconds"):
            self.capture_delayed(max(1, int(float(args["seconds"]))))
        elif action == "app.edit_video" and args.get("path"):
            self.open_video_editor(Path(str(args["path"])))
        else:
            # The caller's window (DevPilot hub, a menu) is closing: let it go before capturing.
            self.trigger(action, from_menu=True)

    def shutdown(self, *, persist: bool = True) -> None:
        log.info("Shutting down UI")
        self._esc_timer.stop()
        self.recording.shutdown()
        for window in list(self._windows.values()) + list(self.pins):
            getattr(window, "force_close", window.close)()  # quitting: no "discard edits?" questions
        if self.edge_bar is not None:
            self.edge_bar.close()
            self.edge_bar = None
        if self._listener is not None:
            self._listener.stop()
        self._worker.shutdown(wait=True)
        self.bridge.close()
        self.retranslator.dispose()
        self.app.shutdown(persist=persist)

    def quit(self) -> None:
        self.shutdown()
        QApplication.quit()
