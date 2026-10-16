"""Interactive region selector: one frameless overlay per monitor.

The desktop is frozen first (``CaptureService.freeze``); each overlay shows
its monitor's part of that image, dimmed, and the user selects on top of it.
Behaviour lives in :class:`SelectionModel`; this module only maps Qt input to
physical pixels and paints.

Why one overlay per monitor: on mixed-DPI setups a single window spanning
monitors gets rescaled by Windows; per-monitor overlays stay pixel exact.
A selection is therefore limited to one monitor (use "All monitors" for
spanning captures).
"""

from __future__ import annotations

import logging
import math
from typing import Sequence

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal, QObject
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap, QScreen
from PySide6.QtWidgets import QWidget

from screenpilot.capture.geometry import Monitor, Point, Rect, WindowInfo
from screenpilot.capture.image import CaptureResult
from screenpilot.capture.selection import SelectionModel, State
from screenpilot.i18n import Translator
from screenpilot.ui.qt_helpers import ACCENT, qimage_from_captured, ui_font

log = logging.getLogger(__name__)

DIM = QColor(0, 0, 0, 115)
MAGNIFIER_SOURCE = 15  # physical pixels sampled around the cursor (odd)
MAGNIFIER_ZOOM = 8
HANDLE_CURSORS = {
    "nw": Qt.CursorShape.SizeFDiagCursor,
    "se": Qt.CursorShape.SizeFDiagCursor,
    "ne": Qt.CursorShape.SizeBDiagCursor,
    "sw": Qt.CursorShape.SizeBDiagCursor,
    "n": Qt.CursorShape.SizeVerCursor,
    "s": Qt.CursorShape.SizeVerCursor,
    "e": Qt.CursorShape.SizeHorCursor,
    "w": Qt.CursorShape.SizeHorCursor,
    "inside": Qt.CursorShape.SizeAllCursor,
}


def match_screens(monitors: Sequence[Monitor], screens: Sequence[QScreen]) -> list[tuple[Monitor, QScreen]]:
    """Pair our physical monitors with Qt screens (by device name, then by order of position)."""
    by_name = {s.name(): s for s in screens}
    pairs: list[tuple[Monitor, QScreen]] = []
    unmatched_monitors: list[Monitor] = []
    used: set[str] = set()
    for monitor in monitors:
        screen = by_name.get(monitor.device_name)
        if screen is not None:
            pairs.append((monitor, screen))
            used.add(screen.name())
        else:
            unmatched_monitors.append(monitor)
    remaining = sorted((s for s in screens if s.name() not in used), key=lambda s: (s.geometry().x(), s.geometry().y()))
    unmatched_monitors.sort(key=lambda m: (m.bounds.x, m.bounds.y))
    pairs.extend(zip(unmatched_monitors, remaining))
    return pairs


class MonitorOverlay(QWidget):
    pressed = Signal(object)  # self, when a new interaction starts here
    confirmed = Signal(object)  # Rect
    cancelled = Signal()

    def __init__(
        self,
        monitor: Monitor,
        screen: QScreen,
        frozen: QImage,
        frozen_origin: Point,
        windows: Sequence[WindowInfo],
        translator: Translator,
    ) -> None:
        super().__init__(None)
        self.monitor = monitor
        self.translator = translator
        self.model = SelectionModel(monitor.bounds, windows, handle_size=max(8, round(8 * monitor.scale)))
        self._frozen = frozen
        self._origin = frozen_origin
        b = monitor.bounds
        local = QRect(b.x - frozen_origin.x, b.y - frozen_origin.y, b.width, b.height)
        self._background = QPixmap.fromImage(frozen.copy(local))
        self._hovering = False  # mouse is on this monitor: magnifier, hint, snapping

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())

    # -- coordinate mapping ---------------------------------------------------
    @property
    def ratio_x(self) -> float:
        return self.monitor.bounds.width / max(1, self.width())

    @property
    def ratio_y(self) -> float:
        return self.monitor.bounds.height / max(1, self.height())

    def to_physical(self, pos: QPointF) -> Point:
        b = self.monitor.bounds
        return Point(b.x + math.floor(pos.x() * self.ratio_x), b.y + math.floor(pos.y() * self.ratio_y))

    def to_logical(self, rect: Rect) -> QRectF:
        b = self.monitor.bounds
        return QRectF(
            (rect.x - b.x) / self.ratio_x,
            (rect.y - b.y) / self.ratio_y,
            rect.width / self.ratio_x,
            rect.height / self.ratio_y,
        )

    # -- activation -----------------------------------------------------------
    def set_hovering(self, hovering: bool) -> None:
        self._hovering = hovering
        self.update()

    def release_selection(self) -> None:
        """Another monitor started a selection: drop ours."""
        if self.model.state is not State.IDLE:
            self.model.reset()
            self.update()

    # -- input ----------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        p = self.to_physical(event.position())
        if event.button() == Qt.MouseButton.RightButton:
            if self.model.selection is not None:
                self.model.reset()
                self.update()
            else:
                self.cancelled.emit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.pressed.emit(self)
        self.model.press(p)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        p = self.to_physical(event.position())
        self._hovering = True
        self.model.move(p)
        hit = self.model.hit_test(p) if self.model.state is State.ADJUSTING else None
        self.setCursor(HANDLE_CURSORS.get(hit or "", Qt.CursorShape.CrossCursor))
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.model.release(self.to_physical(event.position()))
            self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._confirm()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovering = True
        self.activateWindow()
        self.setFocus()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovering = False
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key, mods = event.key(), event.modifiers()
        step = 10 if mods & Qt.KeyboardModifier.ShiftModifier else 1
        resize = bool(mods & Qt.KeyboardModifier.AltModifier)
        arrows = {
            Qt.Key.Key_Left: (-step, 0),
            Qt.Key.Key_Right: (step, 0),
            Qt.Key.Key_Up: (0, -step),
            Qt.Key.Key_Down: (0, step),
        }
        if key == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm()
        elif key in arrows:
            dx, dy = arrows[key]
            self.model.nudge(dx, dy, resize=resize)
            if self.model.selection is None:
                # move the real mouse too, so pixel-precise picking works with the keyboard
                c = self.to_logical(Rect(self.model.cursor.x, self.model.cursor.y, 1, 1)).topLeft()
                QCursor.setPos(self.mapToGlobal(c.toPoint()))
            self.update()
        elif key == Qt.Key.Key_A and mods & Qt.KeyboardModifier.ControlModifier:
            self.pressed.emit(self)
            self.model.select_all()
            self.update()
        else:
            super().keyPressEvent(event)

    def _confirm(self) -> None:
        rect = self.model.result() or self.model.hover_rect()
        if rect is not None and not rect.is_empty:
            self.confirmed.emit(rect)

    # -- painting -------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.drawPixmap(self.rect(), self._background)
        snap = self.model.snapshot()
        focus = snap.selection or (snap.hover if self._hovering else None)

        # Dim everything except the focused rectangle.
        full = QRectF(self.rect())
        if focus is not None:
            hole = self.to_logical(focus)
            for r in (
                QRectF(full.left(), full.top(), full.width(), hole.top() - full.top()),
                QRectF(full.left(), hole.bottom(), full.width(), full.bottom() - hole.bottom()),
                QRectF(full.left(), hole.top(), hole.left() - full.left(), hole.height()),
                QRectF(hole.right(), hole.top(), full.right() - hole.right(), hole.height()),
            ):
                if r.width() > 0 and r.height() > 0:
                    p.fillRect(r, DIM)
            pen = QPen(ACCENT, 2 if snap.selection else 3)
            p.setPen(pen)
            p.drawRect(hole.adjusted(-1, -1, 0, 0))
            self._paint_size_label(p, focus, hole)
            if snap.selection is not None and snap.state is not State.DRAGGING:
                p.setPen(QPen(QColor("white"), 1))
                p.setBrush(ACCENT)
                for handle in self.model.handle_rects().values():
                    p.drawRect(self.to_logical(handle))
        else:
            p.fillRect(full, DIM)

        if self._hovering and snap.state is not State.ADJUSTING:
            self._paint_crosshair(p, snap.cursor)
            self._paint_magnifier(p, snap.cursor)
        if self._hovering:
            self._paint_hint(p, snap.state)
        p.end()

    def _paint_size_label(self, p: QPainter, rect: Rect, hole: QRectF) -> None:
        text = self.translator.t("selector.size", width=rect.width, height=rect.height)
        p.setFont(ui_font(9))
        metrics = p.fontMetrics()
        w, h = metrics.horizontalAdvance(text) + 12, metrics.height() + 6
        y = hole.top() - h - 4 if hole.top() - h - 4 > 0 else hole.top() + 4
        box = QRectF(hole.left(), y, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(32, 32, 32, 220))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_crosshair(self, p: QPainter, cursor: Point) -> None:
        c = self.to_logical(Rect(cursor.x, cursor.y, 1, 1)).center()
        p.setPen(QPen(QColor(255, 255, 255, 90), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(0, c.y()), QPointF(self.width(), c.y()))
        p.drawLine(QPointF(c.x(), 0), QPointF(c.x(), self.height()))

    def _paint_magnifier(self, p: QPainter, cursor: Point) -> None:
        half = MAGNIFIER_SOURCE // 2
        src = QRect(cursor.x - self._origin.x - half, cursor.y - self._origin.y - half, MAGNIFIER_SOURCE, MAGNIFIER_SOURCE)
        zoom = MAGNIFIER_SOURCE * MAGNIFIER_ZOOM
        sample = self._frozen.copy(src).scaled(
            QSize(zoom, zoom), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation
        )
        color = self._frozen.pixelColor(cursor.x - self._origin.x, cursor.y - self._origin.y)
        info = f"{self.translator.t('selector.position', x=cursor.x, y=cursor.y)}   {color.name().upper()}"
        p.setFont(ui_font(9))
        info_h = p.fontMetrics().height() + 8

        c = self.to_logical(Rect(cursor.x, cursor.y, 1, 1)).center()
        x, y = c.x() + 24, c.y() + 24
        if x + zoom + 4 > self.width():
            x = c.x() - 24 - zoom
        if y + zoom + info_h + 4 > self.height():
            y = c.y() - 24 - zoom - info_h
        frame = QRectF(x, y, zoom, zoom)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(32, 32, 32, 230))
        p.drawRoundedRect(frame.adjusted(-3, -3, 3, 3 + info_h), 6, 6)
        p.drawImage(frame, sample)
        # Centre pixel marker
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(ACCENT, 1))
        p.drawRect(QRectF(x + half * MAGNIFIER_ZOOM, y + half * MAGNIFIER_ZOOM, MAGNIFIER_ZOOM, MAGNIFIER_ZOOM))
        p.setPen(QColor("white"))
        p.drawText(QRectF(x, y + zoom, zoom, info_h), Qt.AlignmentFlag.AlignCenter, info)

    def _paint_hint(self, p: QPainter, state: State) -> None:
        key = "selector.adjust_hint" if state is State.ADJUSTING else "selector.hint"
        text = self.translator.t(key)
        p.setFont(ui_font(10))
        metrics = p.fontMetrics()
        w, h = min(self.width() - 32, metrics.horizontalAdvance(text) + 32), metrics.height() + 16
        box = QRectF((self.width() - w) / 2, self.height() - h - 32, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(32, 32, 32, 220))
        p.drawRoundedRect(box, h / 2, h / 2)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)


class RegionSelector(QObject):
    """Shows overlays on all monitors and reports the chosen rectangle."""

    finished = Signal(object)  # Rect or None

    def __init__(self, translator: Translator, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.translator = translator
        self.overlays: list[MonitorOverlay] = []
        self.frozen: CaptureResult | None = None

    @property
    def active(self) -> bool:
        return bool(self.overlays)

    def start(self, frozen: CaptureResult, monitors: Sequence[Monitor], windows: Sequence[WindowInfo]) -> None:
        if self.overlays:
            return
        self.frozen = frozen
        image = qimage_from_captured(frozen.image)
        origin = Point(frozen.region.x, frozen.region.y)
        cursor = QCursor.pos()
        for monitor, screen in match_screens(monitors, QGuiApplication.screens()):
            overlay = MonitorOverlay(monitor, screen, image, origin, windows, self.translator)
            overlay.pressed.connect(self._on_pressed)
            overlay.confirmed.connect(self._finish)
            overlay.cancelled.connect(lambda: self._finish(None))
            self.overlays.append(overlay)
            overlay.show()
            if screen.geometry().contains(cursor):
                overlay.set_hovering(True)
                overlay.model.move(overlay.to_physical(QPointF(overlay.mapFromGlobal(cursor))))
                overlay.activateWindow()
                overlay.setFocus()
        if not self.overlays:
            log.error("No screen could be matched to a monitor")
            self.finished.emit(None)

    def _on_pressed(self, source: MonitorOverlay) -> None:
        for overlay in self.overlays:
            if overlay is not source:
                overlay.release_selection()

    def _finish(self, rect: Rect | None) -> None:
        overlays, self.overlays = self.overlays, []
        for overlay in overlays:
            overlay.close()
        self.finished.emit(rect)
