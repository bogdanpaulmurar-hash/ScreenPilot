"""Recording UI: area frame, floating control bar, result card and the flow glue.

Flow: hotkey / hub / edge bar → region selector (red border, Enter = record)
→ countdown → red frame around the area + control bar (timer, pause, zoom,
stop, cancel) → encoder finishes in the background → result card (open,
folder, copy path, convert to GIF).

The frame and the bar are excluded from screen capture on Windows 10 2004+
(``WDA_EXCLUDEFROMCAPTURE``) and are placed outside the recorded area when
possible, so they never end up in the video either way.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from screenpilot.capture.geometry import Point, Rect
from screenpilot.core.errors import RecordingError, ScreenPilotError
from screenpilot.core.events import Event
from screenpilot.i18n import Translator
from screenpilot.ui import icons
from screenpilot.ui.countdown import CountdownOverlay
from screenpilot.ui.qt_helpers import Retranslator
from screenpilot.ui.region_selector import physical_to_logical
from screenpilot.ui.theme import ui_font
from screenpilot.ui.widgets import IconBadge, IconButton, label, theme

if TYPE_CHECKING:  # pragma: no cover
    from screenpilot.recording import Recorder, RecordingResult
    from screenpilot.ui.ink_overlay import InkOverlay
    from screenpilot.ui.controller import AppController

log = logging.getLogger(__name__)

RECORD_COLOR = QColor("#d13438")
PAUSED_COLOR = QColor("#f7630c")
FRAME_GAP = 3  # px between the recorded area and the drawn border
TICK_MS = 250
RESULT_AUTOCLOSE_MS = 15000
WDA_EXCLUDEFROMCAPTURE = 0x11


def exclude_from_capture(widget: QWidget) -> bool:
    """Hide ``widget`` from screenshots and recordings (Windows 10 2004+)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ok = bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), WDA_EXCLUDEFROMCAPTURE))
    except Exception:  # noqa: BLE001 - cosmetic; the widget sits outside the area anyway
        ok = False
    if not ok:
        log.info("SetWindowDisplayAffinity unavailable; relying on placement outside the recorded area")
    return ok


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_duration(seconds: float) -> str:
    """Short clips read better with tenths: "4.2 s"; longer ones as a clock."""
    return f"{seconds:.1f} s" if seconds < 60 else format_elapsed(seconds)


def format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def logical_rect(rect: Rect, monitors) -> QRect:
    top_left = physical_to_logical(Point(rect.x, rect.y), monitors)
    bottom_right = physical_to_logical(Point(rect.right - 1, rect.bottom - 1), monitors)
    return QRect(top_left, bottom_right)


class RecordingFrame(QWidget):
    """Click-through border drawn just outside the recorded area."""

    def __init__(self, area: QRect) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        pad = FRAME_GAP + 3
        self.setGeometry(area.adjusted(-pad, -pad, pad, pad))
        self.paused = False
        self.waiting = True  # dashed while counting down

    def set_state(self, *, paused: bool = False, waiting: bool = False) -> None:
        if (paused, waiting) != (self.paused, self.waiting):
            self.paused, self.waiting = paused, waiting
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        color = PAUSED_COLOR if self.paused else RECORD_COLOR
        pen = QPen(color, 2)
        if self.waiting or self.paused:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRect(self.rect().adjusted(2, 2, -3, -3))


class RecordingBar(QFrame):
    """Floating controls: ● 00:12  ⏸  🔍  ■  ✕ (draggable)."""

    pause_clicked = Signal()
    zoom_clicked = Signal()
    draw_clicked = Signal()
    tool_clicked = Signal(str)
    undo_clicked = Signal()
    clear_clicked = Signal()
    stop_clicked = Signal()
    cancel_clicked = Signal()

    def __init__(self, translator: Translator, *, ink: bool = False) -> None:
        super().__init__(None)
        self.t = translator.t
        self.rt = rt = Retranslator(translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("RecordingBar")
        self.setStyleSheet(
            "QFrame#RecordingBar { background: rgba(32,32,32,242); border: 1px solid rgba(255,255,255,40); border-radius: 10px; }"
            " QToolButton { background: transparent; border: none; border-radius: 6px; padding: 5px; }"
            " QToolButton:hover { background: rgba(255,255,255,30); }"
            " QToolButton:checked { background: rgba(255,255,255,45); }"
            " QToolButton#Stop { background: #d13438; } QToolButton#Stop:hover { background: #e04a4e; }"
            " QLabel { color: white; }"
            " QToolTip { background: #2b2b2b; color: white; border: 1px solid #444; border-radius: 6px; padding: 5px 8px; }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 5, 5, 5)
        row.setSpacing(4)
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        row.addWidget(self.dot)
        self.time_label = QLabel("00:00")
        self.time_label.setFont(ui_font(10.5, weight=QFont.Weight.DemiBold))
        self.time_label.setMinimumWidth(58)
        row.addWidget(self.time_label)
        self.status = QLabel()
        self.status.setFont(ui_font(8.5))
        self.status.hide()
        row.addWidget(self.status)
        self.buttons: dict[str, QToolButton] = {}
        for name, icon_name, key, signal, checkable in (
            ("pause", "pause", "recording.pause", self.pause_clicked, False),
            ("zoom", "zoom", "recording.zoom", self.zoom_clicked, True),
            ("draw", "pen", "ink.draw", self.draw_clicked, True),
            ("stop", "stop", "recording.stop", self.stop_clicked, False),
            ("cancel", "close", "recording.cancel", self.cancel_clicked, False),
        ):
            button = QToolButton(self)
            button.setIcon(icons.icon(icon_name, "#ffffff", 18))
            button.setIconSize(QSize(18, 18))
            button.setCheckable(checkable)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if name == "stop":
                button.setObjectName("Stop")
            if name != "pause":  # the pause tooltip follows the paused state (set_state)
                rt.bind(button.setToolTip, key)
            button.clicked.connect(signal.emit)
            row.addWidget(button)
            self.buttons[name] = button
            if name == "draw":
                button.setVisible(ink)
                self.tools = self._tool_strip(row)
        self._blink = True
        self._paused: bool | None = None
        self._drag: QPoint | None = None
        self.set_state(0.0, paused=False)
        self.adjustSize()

    def _tool_strip(self, row: QHBoxLayout) -> QFrame:
        """Ink tools, shown only while drawing: step, arrow, rectangle, highlight, text, undo, clear."""
        strip = QFrame(self)
        strip.setObjectName("InkTools")
        strip.setStyleSheet("QFrame#InkTools { border-left: 1px solid rgba(255,255,255,50); border-right: 1px solid rgba(255,255,255,50); }")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(2)
        self.tool_buttons: dict[str, QToolButton] = {}
        entries = (
            ("step", "steps", "ink.tool.step"),
            ("arrow", "arrow", "ink.tool.arrow"),
            ("rect", "rect", "ink.tool.rect"),
            ("highlight", "highlight", "ink.tool.highlight"),
            ("text", "text", "ink.tool.text"),
            ("undo", "undo", "ink.undo"),
            ("clear", "trash", "ink.clear"),
        )
        for index, (name, icon_name, key) in enumerate(entries, start=1):
            button = QToolButton(strip)
            button.setIcon(icons.icon(icon_name, "#ffffff", 16))
            button.setIconSize(QSize(16, 16))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if name in ("undo", "clear"):
                self.rt.bind(button.setToolTip, key)
                button.clicked.connect(self.undo_clicked.emit if name == "undo" else self.clear_clicked.emit)
            else:
                button.setCheckable(True)
                self.rt.bind(button.setToolTip, key, shortcut=str(index))
                button.clicked.connect(lambda _c=False, n=name: self.tool_clicked.emit(n))
                self.tool_buttons[name] = button
            layout.addWidget(button)
        row.addWidget(strip)
        strip.hide()
        return strip

    def set_drawing(self, on: bool, tool: str = "step") -> None:
        self.buttons["draw"].setChecked(on)
        self.tools.setVisible(on)
        self.set_tool(tool)
        self.adjustSize()

    def set_tool(self, tool: str) -> None:
        for name, button in getattr(self, "tool_buttons", {}).items():
            button.setChecked(name == tool)

    def set_state(self, elapsed: float, *, paused: bool, finishing: bool = False, zoom: bool | None = None) -> None:
        self.time_label.setText(format_elapsed(elapsed))
        self._blink = not self._blink if not paused else True
        color = PAUSED_COLOR if paused else (RECORD_COLOR if self._blink else QColor(RECORD_COLOR).darker(160))
        self.dot.setStyleSheet(f"background: {color.name()}; border-radius: 5px;")
        if paused != self._paused:
            self._paused = paused
            pause = self.buttons["pause"]
            pause.setIcon(icons.icon("play" if paused else "pause", "#ffffff", 18))
            pause.setToolTip(self.t("recording.resume" if paused else "recording.pause"))
        if zoom is not None:
            self.buttons["zoom"].setChecked(zoom)
        if finishing and not self.status.isVisible():
            self.status.setText(self.t("recording.saving"))
            self.status.show()
            for button in self.buttons.values():
                button.setEnabled(False)

    def place_near(self, area: QRect) -> None:
        """Below the area, else above, else inside its bottom edge."""
        screen = QGuiApplication.screenAt(area.center()) or QGuiApplication.primaryScreen()
        bounds = screen.availableGeometry()
        self.adjustSize()
        x = min(max(area.center().x() - self.width() // 2, bounds.left() + 8), bounds.right() - self.width() - 8)
        gap = FRAME_GAP + 10
        if area.bottom() + gap + self.height() <= bounds.bottom():
            y = area.bottom() + gap
        elif area.top() - gap - self.height() >= bounds.top():
            y = area.top() - gap - self.height()
        else:
            y = area.bottom() - self.height() - 12
        self.move(x, y)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag = None


class RecordingResultCard(QWidget):
    """Bottom-right card shown when a recording is saved."""

    gif_requested = Signal(object)  # Path
    edit_requested = Signal(object)  # Path

    def __init__(self, result: "RecordingResult", translator: Translator, *, can_convert: bool, reveal: Callable[[Path], None], copy_text: Callable[[str], None], can_edit: bool = False) -> None:
        super().__init__(None)
        self.result = result
        self.t = translator.t
        self.rt = rt = Retranslator(translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        t = theme()
        card = QFrame(self)
        card.setObjectName("ResultCard")
        card.setStyleSheet(
            f"QFrame#ResultCard {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: 12px; }}"
        )
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        card.setGraphicsEffect(shadow)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 12, 10)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.addWidget(IconBadge("film" if result.format == "gif" else "video", 18, token="accent"))
        self.title = label(role="subtitle")
        rt.bind(self.title.setText, "recording.saved")
        head.addWidget(self.title, 1)
        close = IconButton("close", size=14, token="text_secondary")
        rt.bind(close.setToolTip, "preview.close")
        close.clicked.connect(self.close)
        head.addWidget(close)
        layout.addLayout(head)
        name = label(result.path.name, "body")
        name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(name)
        self.details = label(role="caption")
        self.details.setText(
            f"{format_duration(result.duration)} · {format_size(result.size_bytes)} · {result.format.upper()}"
            f" · {result.rect.width}×{result.rect.height}"
        )
        layout.addWidget(self.details)

        actions = QHBoxLayout()
        actions.setSpacing(2)
        self.buttons: dict[str, QWidget] = {}
        for key, icon_name, tip, slot in (
            ("open", "play", "recording.open", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.path)))),
            ("folder", "folder", "preview.folder", lambda: reveal(result.path)),
            ("copy_path", "copy", "recording.copy_path", lambda: copy_text(str(result.path))),
            *((("edit", "edit", "recording.edit", lambda: (self.edit_requested.emit(result.path), self.close())),) if can_edit else ()),
        ):
            button = IconButton(icon_name, size=18)
            rt.bind(button.setToolTip, tip)
            button.clicked.connect(slot)
            actions.addWidget(button)
            self.buttons[key] = button
        actions.addStretch(1)
        if result.format != "gif" and can_convert:
            gif = QPushButton()
            gif.setCursor(Qt.CursorShape.PointingHandCursor)
            rt.bind(gif.setText, "recording.to_gif")
            gif.clicked.connect(lambda: (self.gif_requested.emit(result.path), gif.setEnabled(False)))
            actions.addWidget(gif)
            self.buttons["gif"] = gif
        layout.addLayout(actions)
        self.setMinimumWidth(340)
        self.adjustSize()
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self.close)

    def show_near_corner(self) -> None:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(area.right() - self.width() - 4, area.bottom() - self.height() - 4)
        self.show()
        self._close_timer.start(RESULT_AUTOCLOSE_MS)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._close_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._close_timer.start(RESULT_AUTOCLOSE_MS // 2)
        super().leaveEvent(event)


class RecordingController(QObject):
    """Owns one recording at a time; lives on the GUI thread."""

    changed = Signal()
    gif_done = Signal(object, object)  # Path | None, error str | None

    def __init__(self, controller: "AppController") -> None:
        super().__init__(controller)
        self.ctrl = controller
        self.app = controller.app
        self.t = controller.t
        self.recorder: "Recorder | None" = None
        self.frame: RecordingFrame | None = None
        self.bar: RecordingBar | None = None
        self.ink: "InkOverlay | None" = None
        self.result_card: RecordingResultCard | None = None
        self._pending: tuple["Recorder", Rect] | None = None
        self._countdown: CountdownOverlay | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.gif_done.connect(self._on_gif_done)

    # -- state ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._pending is not None or (self.recorder is not None and self.recorder.active)

    @property
    def recording(self) -> bool:
        return self.recorder is not None and self.recorder.active

    def toggle(self, fmt: str = "mp4") -> None:
        if self.recording:
            self.stop()
        elif self._pending is not None and self._countdown is not None:
            self._countdown.cancel()
        else:
            self.ctrl.capture_region("record.gif" if fmt == "gif" else "record.video")

    # -- start --------------------------------------------------------------------
    def start(self, rect: Rect, fmt: str = "mp4") -> bool:
        if self.active:
            return False
        service = self.app.recording
        options = service.options().with_format(fmt)
        try:
            recorder = service.prepare(options)
        except RecordingError as exc:
            if exc.message_key != "recording.error.no_ffmpeg":
                self._error(exc)
                return False
            log.info("ffmpeg missing: falling back to GIF")
            if self.app.settings.get("recording.gif_fallback_notice"):
                # Clicking the message opens Settings › Recording (FFmpeg status, path, install hint).
                self.ctrl.notify_action.emit(
                    self.t("recording.no_ffmpeg.title"),
                    self.t("recording.no_ffmpeg.body"),
                    False,
                    lambda: self.ctrl.show_settings("recording"),
                )
            try:
                recorder = service.prepare(options.with_format("gif"))
            except ScreenPilotError as again:
                self._error(again)
                return False
        except ScreenPilotError as exc:
            self._error(exc)
            return False
        self._pending = (recorder, rect)
        self._show_frame(rect)
        self.changed.emit()
        self.ctrl.watch_escape()
        seconds = recorder.options.countdown
        if seconds > 0:
            self._countdown = CountdownOverlay(seconds, self.app.translator)
            self._countdown.elapsed.connect(self._begin)
            self._countdown.cancelled.connect(self._abort_pending)
            self._countdown.start()
        else:
            QTimer.singleShot(0, self._begin)
        return True

    def _show_frame(self, rect: Rect) -> None:
        monitors = self.app.capture.monitors()
        self._area = logical_rect(rect, monitors)
        self.frame = RecordingFrame(self._area)
        self.frame.show()
        exclude_from_capture(self.frame)

    def _begin(self) -> None:
        self._countdown = None
        if self._pending is None:
            return
        recorder, rect = self._pending
        self._pending = None
        try:
            recorder.start(rect)
        except ScreenPilotError as exc:
            self.app.recording.current = None
            self._teardown()
            self._error(exc)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Recording could not start")
            self.app.recording.current = None
            self._teardown()
            self._error(RecordingError(str(exc), message_key="recording.error.generic"))
            return
        self.recorder = recorder
        if self.frame is not None:
            self.frame.set_state(waiting=False)
        ink_allowed = self.app.policy.allows("record_annotations") and recorder.ink is not None
        if ink_allowed:
            from screenpilot.ui.ink_overlay import InkOverlay

            self.ink = InkOverlay(
                self._area, recorder.ink, self.app.translator,
                on_escape=self.ctrl.escape_pressed, exclude=exclude_from_capture,
            )
        self.bar = RecordingBar(self.app.translator, ink=ink_allowed)
        self.bar.pause_clicked.connect(self.toggle_pause)
        self.bar.zoom_clicked.connect(self.toggle_zoom)
        self.bar.draw_clicked.connect(self.toggle_draw)
        self.bar.tool_clicked.connect(self.set_ink_tool)
        self.bar.undo_clicked.connect(lambda: self.ink and self.ink.undo())
        self.bar.clear_clicked.connect(lambda: self.ink and self.ink.clear())
        if self.ink is not None:
            self.ink.drawing_changed.connect(lambda on: self.bar and self.bar.set_drawing(on, self.ink.tool if self.ink else "step"))
            self.ink.tool_changed.connect(lambda tool: self.bar and self.bar.set_tool(tool))
        self.bar.stop_clicked.connect(self.stop)
        self.bar.cancel_clicked.connect(self.cancel)
        self.bar.place_near(self._area)
        self.bar.show()
        exclude_from_capture(self.bar)
        self._timer.start()
        self.changed.emit()

    def _abort_pending(self) -> None:
        self._countdown = None
        if self._pending is not None:
            self._pending = None
            self.app.recording.current = None
        log.info("Recording cancelled during countdown")
        self._teardown()
        self.changed.emit()

    # -- controls -------------------------------------------------------------------
    def toggle_pause(self) -> None:
        if self.recorder is not None:
            self.recorder.toggle_pause()
            self._tick()

    def toggle_zoom(self) -> None:
        if self.recorder is not None:
            on = self.recorder.toggle_zoom()
            if self.bar is not None:
                self.bar.buttons["zoom"].setChecked(on)

    def toggle_draw(self) -> bool:
        """Pen on/off: while on, the mouse draws on the recorded area."""
        if self.ink is None:
            if self.bar is not None:
                self.bar.buttons["draw"].setChecked(False)
            return False
        on = self.ink.toggle_drawing()
        if self.bar is not None:
            self.bar.set_drawing(on, self.ink.tool)
        return on

    def set_ink_tool(self, tool: str) -> None:
        if self.ink is not None:
            self.ink.set_tool(tool)
            if not self.ink.drawing:
                self.toggle_draw()

    def stop(self) -> None:
        if self.ink is not None:
            self.ink.set_drawing(False)
        if self.recorder is not None and self.recorder.active:
            self.recorder.stop()
            if self.bar is not None:
                self.bar.set_state(self.recorder.elapsed, paused=False, finishing=True)

    def cancel(self) -> None:
        if self._countdown is not None:
            self._countdown.cancel()
        elif self.recorder is not None:
            self.recorder.cancel()

    def escape(self) -> str:
        """Esc during a recording: cancel the countdown, or pause and ask
        continue / stop and save / discard (``recording.esc_pauses``)."""
        if self._countdown is not None:
            self._countdown.cancel()
            return "recording.countdown"
        recorder = self.recorder
        if recorder is None or not recorder.active:
            return "none"
        if self.ink is not None and self.ink.drawing:
            self.toggle_draw()  # first Esc only puts the pen away
            return "recording.ink"
        if not self.app.settings.get("recording.esc_pauses"):
            return "recording.ignored"
        from screenpilot.recording import State

        was_paused = recorder.state == State.PAUSED
        if not was_paused:
            recorder.pause()
            self._tick()
        answer = self.ctrl.ask_continue("recording.paused.title", "recording.paused.body", stop_key="recording.paused.stop")
        if self.recorder is not recorder or not recorder.active:
            return "recording.ended"  # it finished on its own meanwhile (time limit)
        if answer == "stop":
            self.stop()
        elif answer == "cancel":
            self.cancel()
        elif not was_paused:
            recorder.resume()
            self._tick()
        return f"recording.{answer}"

    def _tick(self) -> None:
        recorder = self.recorder
        if recorder is None:
            return
        from screenpilot.recording import State

        paused = recorder.state == State.PAUSED
        if self.bar is not None and recorder.state != State.FINISHING:
            self.bar.set_state(recorder.elapsed, paused=paused, zoom=recorder.effects.zoom_on)
        if self.frame is not None:
            self.frame.set_state(paused=paused)

    # -- results --------------------------------------------------------------------
    def on_event(self, event: Event) -> None:
        if event.name == "recording.finished":
            self._finished(event.payload["result"])
        elif event.name == "recording.failed":
            failed = self.recorder
            self._teardown()
            if not self._retry_with_another_encoder(failed, event.payload["error"]):
                self._error(event.payload["error"])
        elif event.name == "recording.cancelled":
            self._teardown()
            self.ctrl.notify.emit(self.t("app.name"), self.t("recording.cancelled"), False)
        elif event.name == "recording.state":
            self._tick()

    def _retry_with_another_encoder(self, failed: "Recorder | None", error: ScreenPilotError) -> bool:
        """An encoder that passed the probe can still fail on real frames (drivers,
        session limits). Within the first seconds, restart once with the next one."""
        if failed is None or getattr(failed, "_retried", False) or failed.options.is_gif:
            return False
        if error.message_key != "recording.error.encoder_failed" or failed.encoder in ("", "libx264"):
            return False
        if failed._frames > failed.options.frame_rate * 5:
            return False  # too late: restarting would silently drop what was recorded
        log.warning("Encoder %s failed at start; retrying with another encoder", failed.encoder)
        self.ctrl.notify.emit(self.t("app.name"), self.t("recording.retrying"), False)
        rect, fmt = failed.rect, failed.options.format
        countdown = failed.options.countdown
        service = self.app.recording
        try:
            from dataclasses import replace

            service._learn_from_last_failure()
            recorder = service.prepare(replace(failed.options, countdown=0))
        except ScreenPilotError as exc:
            self._error(exc)
            return True
        recorder._retried = True
        self._pending = (recorder, rect)
        self._show_frame(rect)
        QTimer.singleShot(0, self._begin)
        log.info("Retrying %s recording (countdown %s skipped)", fmt, countdown)
        return True

    def _finished(self, result: "RecordingResult") -> None:
        self._teardown()
        self.app.bus.publish("recording.saved", path=str(result.path), format=result.format, duration=result.duration)
        if result.reason in ("max_duration", "max_frames"):
            self.ctrl.notify.emit(self.t("recording.saved"), self.t(f"recording.reason.{result.reason}"), False)
        if self.app.settings.get("recording.open_folder_after"):
            self._reveal(result.path)
        self.show_result(result)

    def show_result(self, result: "RecordingResult") -> RecordingResultCard:
        if self.result_card is not None:
            self.result_card.close()
        card = RecordingResultCard(
            result,
            self.app.translator,
            can_convert=self.app.recording.ffmpeg() is not None,
            reveal=self._reveal,
            copy_text=self.ctrl.copy_text,
            can_edit=self.app.policy.allows("video_edit") and (result.format == "gif" or self.app.recording.ffmpeg() is not None),
        )
        card.gif_requested.connect(self.convert_to_gif)
        card.edit_requested.connect(self.ctrl.open_video_editor)
        card.destroyed.connect(lambda *_: setattr(self, "result_card", None) if self.result_card is card else None)
        self.result_card = card
        card.show_near_corner()
        return card

    def convert_to_gif(self, path: Path) -> None:
        self.ctrl.notify.emit(self.t("recording.to_gif"), self.t("recording.gif_working"), False)

        def work() -> None:
            try:
                out = self.app.recording.to_gif(Path(path))
            except ScreenPilotError as exc:
                self.gif_done.emit(None, self.t(exc.message_key))
            except Exception as exc:  # noqa: BLE001
                log.exception("GIF conversion failed")
                self.gif_done.emit(None, str(exc))
            else:
                self.gif_done.emit(out, None)

        threading.Thread(target=work, name="gif-convert", daemon=True).start()

    def _on_gif_done(self, path: Path | None, error: str | None) -> None:
        if error:
            self.ctrl.notify.emit(self.t("notify.failed.title"), error, True)
            return
        self.ctrl.notify.emit(self.t("recording.to_gif"), self.t("recording.gif_saved", path=path), False)
        self._reveal(path)

    def _reveal(self, path: Path) -> None:
        try:
            self.app.shell.reveal(path)
        except OSError:
            log.exception("Could not reveal %s", path)

    def _error(self, exc: ScreenPilotError) -> None:
        log.warning("Recording error: %s", exc)
        self.ctrl.notify.emit(self.t("notify.failed.title"), self.t(exc.message_key), True)

    def _teardown(self) -> None:
        self._timer.stop()
        for widget in (self.ink, self.frame, self.bar):
            if widget is not None:
                widget.close()
                widget.deleteLater()
        self.frame = self.bar = self.ink = None
        self.recorder = None
        self.changed.emit()

    def shutdown(self) -> None:
        if self.recorder is not None and self.recorder.active:
            log.info("Stopping the recording before exit")
            self.recorder.stop(wait=True, timeout=30)
        self._teardown()
