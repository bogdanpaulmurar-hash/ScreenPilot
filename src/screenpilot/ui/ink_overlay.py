"""Transparent drawing surface over the recorded area (live annotations).

The overlay is only an input and preview surface: shapes go into the
recorder's :class:`~screenpilot.recording.ink.InkLayer`, and the recorder burns
them into the frames. The overlay itself is excluded from capture
(``WDA_EXCLUDEFROMCAPTURE``), so the video never contains it twice. When
exclusion is not available (Windows before 10 2004) the overlay is what gets
recorded instead and the layer's ``burn_in`` is turned off.

Two states:

* **viewing** (default): click-through, shows the annotations;
* **drawing** (pen button / hotkey): catches the mouse. Click = step, drag =
  arrow / rectangle / highlight, click with *Text* = a text label.
  Keys: 1–5 tools, Ctrl+Z undo, Delete clear all, right-click undo, Esc ends
  drawing.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QInputDialog, QWidget

from screenpilot.capture.geometry import Point
from screenpilot.i18n import Translator
from screenpilot.recording.ink import InkLayer, InkRenderer

log = logging.getLogger(__name__)

INK_TOOLS = ("step", "arrow", "rect", "highlight", "text")
TOOL_KEYS = {Qt.Key.Key_1: "step", Qt.Key.Key_2: "arrow", Qt.Key.Key_3: "rect", Qt.Key.Key_4: "highlight", Qt.Key.Key_5: "text"}
REFRESH_MS = 60
DRAW_TINT = QColor(0, 120, 212, 16)  # not recorded (the overlay is excluded from capture)
CATCH_ALPHA = QColor(0, 0, 0, 1)  # fully transparent pixels would let clicks through


class InkOverlay(QWidget):
    drawing_changed = Signal(bool)
    tool_changed = Signal(str)

    def __init__(
        self,
        area: QRect,
        layer: InkLayer,
        translator: Translator,
        *,
        on_escape: Callable[[], None] | None = None,
        exclude: Callable[[QWidget], bool] | None = None,
        blocked: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(None)
        self.t = translator.t
        self.layer = layer
        self.on_escape = on_escape
        self.blocked = blocked  # global rect of the control bar: clicks there are never ink
        self.tool = "step"
        self.drawing = False
        self._start: Point | None = None
        self._renderer = InkRenderer()
        self._seen = -1
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        if sys.platform != "win32":  # on Windows the extended style is switched directly (see _native_click_through)
            flags |= Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(area)
        self.setMouseTracking(True)
        self.show()
        excluded = exclude(self) if exclude is not None else False
        if sys.platform == "win32" and not excluded:
            # The overlay will appear in the frames by itself; drawing the ink again would double it.
            layer.burn_in = False
            log.info("Overlay cannot be excluded from capture; annotations are recorded from the overlay")
        self._native_click_through(True)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # -- geometry ---------------------------------------------------------------
    @property
    def scale(self) -> tuple[float, float]:
        """Frame pixels per logical pixel (≈ the monitor's scaling factor)."""
        return self.layer.width / max(1, self.width()), self.layer.height / max(1, self.height())

    def to_frame(self, pos: QPointF) -> Point:
        sx, sy = self.scale
        return Point(round(pos.x() * sx), round(pos.y() * sy))

    # -- state ------------------------------------------------------------------
    def _native_click_through(self, on: bool) -> None:
        if sys.platform == "win32":
            from screenpilot.platform.win32_window import set_click_through

            if set_click_through(int(self.winId()), on):
                return
        # Elsewhere (and as a fallback) through Qt; this re-creates the native window.
        if bool(self.windowFlags() & Qt.WindowType.WindowTransparentForInput) != on:
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, on)
            self.show()

    def set_drawing(self, on: bool) -> None:
        if on == self.drawing:
            return
        self.drawing = on
        self.layer.drawing = on
        if not on:
            self._start = None
            self.layer.set_draft(None)
        self._native_click_through(not on)
        self.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        if on:
            self.raise_()
            self.activateWindow()
            self.setFocus()
            if sys.platform == "win32":
                from screenpilot.platform.win32_window import force_foreground

                force_foreground(int(self.winId()))
        self.update()
        self.drawing_changed.emit(on)

    def toggle_drawing(self) -> bool:
        self.set_drawing(not self.drawing)
        return self.drawing

    def set_tool(self, tool: str) -> None:
        if tool in INK_TOOLS and tool != self.tool:
            self.tool = tool
            self.tool_changed.emit(tool)

    def undo(self) -> None:
        self.layer.undo()
        self._refresh()

    def clear(self) -> None:
        self.layer.clear()
        self._refresh()

    def _refresh(self) -> None:
        # Fading items change opacity without a new version; repaint while any are shown.
        if self.layer.version != self._seen or (self.layer.fade_seconds and not self.layer.empty):
            self._seen = self.layer.version
            self.update()

    # -- input ------------------------------------------------------------------
    def _over_bar(self, event: QMouseEvent) -> bool:
        rect = self.blocked() if self.blocked is not None else None
        return rect is not None and rect.contains(event.globalPosition().toPoint())

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self.drawing or self._over_bar(event):
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.undo()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        p = self.to_frame(event.position())
        if self.tool == "step":
            self.layer.add_step(p)
        elif self.tool == "text":
            self._ask_text(p)
        else:
            self._start = p
        self._refresh()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.drawing and self._start is not None:
            self.layer.set_draft(self.layer.make(self.tool, self._start, self.to_frame(event.position())))
            self._refresh()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._start is not None and event.button() == Qt.MouseButton.LeftButton:
            start, self._start = self._start, None
            self.layer.add(self.layer.make(self.tool, start, self.to_frame(event.position())))
            self._refresh()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self.on_escape is not None:
                self.on_escape()  # one Esc path (the controller debounces its own polling)
            else:
                self.set_drawing(False)
        elif key in TOOL_KEYS:
            self.set_tool(TOOL_KEYS[key])
        elif key == Qt.Key.Key_Z and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.undo()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.clear()
        else:
            super().keyPressEvent(event)

    def _ask_text(self, at: Point) -> None:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(self.t("ink.text.title"))
        dialog.setLabelText(self.t("ink.text.prompt"))
        dialog.setInputMode(QInputDialog.InputMode.TextInput)
        dialog.show()
        from screenpilot.ui.recording import exclude_from_capture

        exclude_from_capture(dialog)  # typing the label is not part of the video
        if dialog.exec() and dialog.textValue().strip():
            self.layer.add(self.layer.make("text", at, text=dialog.textValue().strip()))
        self.setFocus()

    # -- painting -----------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.drawing:
            p.fillRect(self.rect(), DRAW_TINT if self.layer.burn_in else CATCH_ALPHA)
            p.setPen(QPen(QColor(0, 120, 212), 2, Qt.PenStyle.DashLine))
            p.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        sx, sy = self.scale
        for patch in self._renderer.patches(self.layer):
            r = patch.rect
            image = QImage(patch.bgra, r.width, r.height, r.width * 4, QImage.Format.Format_ARGB32)
            p.drawImage(QRectF(r.x / sx, r.y / sy, r.width / sx, r.height / sy), image)
        p.end()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        self.layer.drawing = False
        super().closeEvent(event)
