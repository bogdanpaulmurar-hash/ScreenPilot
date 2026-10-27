"""Interactive region selector: one frameless overlay per monitor.

The desktop is frozen first (``CaptureService.freeze``); each overlay shows
its monitor's part of that image, dimmed, and the user selects on top of it.
Behaviour lives in :class:`SelectionModel`; this module maps Qt input to
physical pixels, paints, and shows a floating action bar next to the
selection (capture, copy only, save as, pin, edit, OCR, cancel) — the
destination is chosen in the same gesture, no extra dialog.

Multi-capture: with Ctrl held when the mouse is released, the selection is
added to a shared :class:`RegionList` and the next one can be drawn right
away, on any monitor. Collected regions stay lit and numbered; Backspace or
Ctrl+Z drops the last one, a right-click drops the one under the cursor,
Enter confirms them all (plus the current selection), Esc cancels everything.

Why one overlay per monitor: on mixed-DPI setups a single window spanning
monitors gets rescaled by Windows; per-monitor overlays stay pixel exact.
A selection is therefore limited to one monitor (use "All screens" for
spanning captures).

The selector chrome is always dark, like other capture tools: it sits on top
of arbitrary screen content and must stay readable on all of it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Sequence

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QFont,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QScreen,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QToolButton, QWidget

from screenpilot.capture.geometry import Monitor, Point, Rect, WindowInfo
from screenpilot.capture.image import CaptureResult
from screenpilot.capture.selection import RegionList, SelectionModel, State
from screenpilot.i18n import Translator
from screenpilot.ui import icons
from screenpilot.ui.qt_helpers import qimage_from_captured
from screenpilot.ui.theme import ui_font
from screenpilot.ui.widgets import theme

log = logging.getLogger(__name__)

# Selector chrome (always dark; accent comes from the theme)
CHROME_BG = QColor(32, 32, 32, 236)
CHROME_TEXT = QColor(255, 255, 255)
CHROME_MUTED = QColor(255, 255, 255, 150)
OUTLINE_DARK = QColor(0, 0, 0, 110)

MAGNIFIER_SOURCE = 15  # physical pixels sampled around the cursor (odd)
MAGNIFIER_ZOOM = 8
BAR_GAP = 10

ACTION_DEFAULT = "default"
ACTION_COPY = "copy"
ACTION_SAVE_AS = "save_as"
ACTION_PIN = "pin"
ACTION_SCROLL = "scroll"
ACTION_STEPS = "steps"
ACTION_EDIT = "edit"
ACTION_OCR = "ocr"
ACTION_COLOR = "color"
ACTION_MEASURE = "measure"
ACTION_INTERVAL = "interval"
ACTION_RECORD = "record"
ACTION_RECORD_GIF = "record_gif"

# What a confirmation of several regions can do (the other actions need one region).
MULTI_ACTIONS = (ACTION_DEFAULT, ACTION_COPY, ACTION_EDIT)

# How several regions go to the clipboard (setting ``capture.multi_clipboard``).
CLIP_SEQUENCE = "sequence"
CLIP_COMBINED = "combined"

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


def selector_accent(color: str = "") -> QColor:
    """Border colour that reads well on a dimmed screenshot (theme accent if empty)."""
    if color:
        custom = QColor(color)
        if custom.isValid():
            return custom
    accent = QColor(theme().accent)
    return accent.lighter(160) if accent.lightness() < 140 else accent


@dataclass(frozen=True)
class SelectorStyle:
    color: str = ""  # '' = accent
    thickness: int = 2
    dim: int = 47  # percent
    magnifier: bool = True
    mode_label: str = ""  # shown next to the hint for non-default modes
    pick_color: bool = False  # colour picker: a click picks the pixel, no selection
    measure: bool = False  # ruler: size pill shows the diagonal, no action bar
    action_bar: bool = True  # False: the selection is captured as soon as the mouse is released
    multi: bool = True  # Ctrl + release collects several regions (off for scroll, record...)


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


def physical_to_logical(point: Point, monitors: Sequence[Monitor]) -> QPoint:
    """Map a physical desktop pixel to Qt's logical global coordinates."""
    for monitor, screen in match_screens(monitors, QGuiApplication.screens()):
        if monitor.bounds.contains(point):
            geo = screen.geometry()
            rx = monitor.bounds.width / max(1, geo.width())
            ry = monitor.bounds.height / max(1, geo.height())
            return QPoint(
                geo.x() + round((point.x - monitor.bounds.x) / rx), geo.y() + round((point.y - monitor.bounds.y) / ry)
            )
    return QPoint(point.x, point.y)


class ActionBar(QFrame):
    """Floating toolbar shown next to a finished selection."""

    triggered = Signal(str)
    cancelled = Signal()
    clip_toggled = Signal()  # several regions: switch "one by one" / "one image"

    def __init__(self, parent: QWidget, translator: Translator, accent: QColor | None = None) -> None:
        super().__init__(parent)
        self.t = translator.t
        accent = accent or selector_accent()
        self.setObjectName("ActionBar")
        self.setStyleSheet(
            f"QFrame#ActionBar {{ background: rgba(32,32,32,240); border: 1px solid rgba(255,255,255,40); border-radius: 10px; }}"
            " QToolButton { background: transparent; border: none; border-radius: 6px; padding: 6px; }"
            " QToolButton:hover { background: rgba(255,255,255,30); }"
            " QToolButton:pressed { background: rgba(255,255,255,18); }"
            f" QToolButton#Primary {{ background: {accent.name()}; }}"
            f" QToolButton#Primary:hover {{ background: {accent.lighter(110).name()}; }}"
            " QToolTip { background: #2b2b2b; color: white; border: 1px solid #444; border-radius: 6px; padding: 5px 8px; }"
        )
        self.setCursor(Qt.CursorShape.ArrowCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(5, 5, 5, 5)
        row.setSpacing(2)
        self.buttons: dict[str, QToolButton] = {}
        spec = (
            ("check", "selector.action.capture", ACTION_DEFAULT, True),
            ("scroll", "selector.action.scroll", ACTION_SCROLL, True),
            ("copy", "selector.action.copy", ACTION_COPY, True),
            ("save_as", "selector.action.save", ACTION_SAVE_AS, True),
            ("pin", "selector.action.pin", ACTION_PIN, True),
            ("steps", "selector.action.steps", ACTION_STEPS, True),
            ("edit", "selector.action.edit", ACTION_EDIT, True),
            ("ocr", "selector.action.ocr", ACTION_OCR, True),
            ("record", "selector.action.record", ACTION_RECORD, True),
            ("close", "selector.action.cancel", "cancel", True),
        )
        for icon_name, key, action, enabled in spec:
            button = QToolButton(self)
            primary = action == ACTION_DEFAULT
            if primary:
                button.setObjectName("Primary")
            color = "#000000" if primary and accent.lightness() > 150 else "#ffffff"
            button.setIcon(icons.icon(icon_name, color, 18, "#6b6b6b"))
            button.setIconSize(QSize(18, 18))
            button.setToolTip(self.t(key))
            button.setEnabled(enabled)
            button.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ForbiddenCursor)
            if action == "cancel":
                button.clicked.connect(self.cancelled.emit)
            elif action is not None:
                button.clicked.connect(lambda _=False, a=action: self.triggered.emit(a))
            if icon_name == "close":
                self.clip_button = self._make_clip_button()
                row.addWidget(self.clip_button)
            if icon_name in ("edit", "close"):
                sep = QFrame(self)
                sep.setFixedSize(1, 20)
                sep.setStyleSheet("background: rgba(255,255,255,45);")
                row.addWidget(sep)
            row.addWidget(button)
            self.buttons[action or icon_name] = button
        self.multi = False
        self.clip_mode = CLIP_SEQUENCE
        self.set_clip_mode(CLIP_SEQUENCE)
        self.clip_button.hide()
        self.adjustSize()

    def _make_clip_button(self) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("ClipMode")
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setIcon(icons.icon("copy", "#ffffff", 18, "#6b6b6b"))
        button.setIconSize(QSize(18, 18))
        button.setStyleSheet("QToolButton#ClipMode { color: white; padding: 6px 8px; }")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(self.clip_toggled.emit)
        return button

    def set_clip_mode(self, mode: str, *, locked: bool = False) -> None:
        """Label of the clipboard switch shown with several regions."""
        self.clip_mode = mode
        self.clip_button.setIcon(icons.icon("image" if mode == CLIP_COMBINED else "copy", "#ffffff", 18, "#6b6b6b"))
        self.clip_button.setText(self.t(f"selector.multi.clipboard.{mode}"))
        self.clip_button.setToolTip(self.t("selector.multi.clipboard.locked" if locked else "selector.multi.clipboard.tooltip"))
        self.clip_button.setEnabled(not locked)
        self.adjustSize()

    def set_multi(self, multi: bool) -> None:
        """Several regions: only the actions that make sense for all of them, plus the clipboard switch."""
        if multi == self.multi:
            return
        self.multi = multi
        for action, button in self.buttons.items():
            button.setVisible(not multi or action in MULTI_ACTIONS or action == "cancel")
        self.clip_button.setVisible(multi)
        self.adjustSize()


class MonitorOverlay(QWidget):
    pressed = Signal(object, bool)  # self, Ctrl held — when a new interaction starts here
    confirmed = Signal(object, str)  # Rect, or list[Rect] for several regions; action
    cancelled = Signal()
    regions_changed = Signal()
    clip_toggled = Signal()

    def __init__(
        self,
        monitor: Monitor,
        screen: QScreen,
        frozen: QImage,
        frozen_origin: Point,
        windows: Sequence[WindowInfo],
        translator: Translator,
        frames: Callable[[Point, WindowInfo | None], Sequence[Rect]] | None = None,
        style: SelectorStyle | None = None,
        regions: RegionList | None = None,
    ) -> None:
        super().__init__(None)
        self.style = style or SelectorStyle()
        self.regions = regions if regions is not None else RegionList()  # shared by all monitors
        self.monitor = monitor
        self.translator = translator
        self.model = SelectionModel(
            monitor.bounds,
            windows,
            handle_size=max(10, round(10 * monitor.scale)),
            frames=None if self.style.pick_color else frames,
            snap=not self.style.pick_color,
        )
        self._frozen = frozen
        self._origin = frozen_origin
        b = monitor.bounds
        local = QRect(b.x - frozen_origin.x, b.y - frozen_origin.y, b.width, b.height)
        self._background = QPixmap.fromImage(frozen.copy(local))
        self._hovering = False  # mouse is on this monitor: magnifier, hint, snapping
        self._accent = selector_accent(self.style.color)
        self._dim = QColor(0, 0, 0, round(255 * max(0, min(80, self.style.dim)) / 100))

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())

        self.action_bar = ActionBar(self, translator, self._accent)
        self.action_bar.hide()
        self.action_bar.triggered.connect(self._confirm)
        self.action_bar.cancelled.connect(self.cancelled.emit)
        self.action_bar.clip_toggled.connect(self.clip_toggled.emit)

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

    @property
    def multi_enabled(self) -> bool:
        return self.style.multi and not self.style.pick_color and not self.style.measure

    def release_selection(self, keep: bool = False) -> None:
        """Another monitor started a selection: drop ours (``keep``: add it to the regions first)."""
        if self.model.state is not State.IDLE:
            if keep and self.multi_enabled:
                self.regions.add(self.model.result())
            self.model.reset()
            self._changed()

    def _collect(self) -> None:
        """Ctrl + release: the selection joins the regions and the next one can start."""
        if self.regions.add(self.model.result()):
            self.model.reset()
            self.regions_changed.emit()
        self._changed()

    def _local_regions(self) -> list[tuple[int, Rect]]:
        """(number, region) of the collected regions on this monitor."""
        b = self.monitor.bounds
        return [(i + 1, r) for i, r in enumerate(self.regions) if not r.intersect(b).is_empty]

    def _changed(self) -> None:
        self._place_action_bar()
        self.update()

    def _place_action_bar(self) -> None:
        snap = self.model.snapshot()
        anchor = snap.selection if snap.state is State.ADJUSTING else None
        if anchor is None and snap.state is State.IDLE and len(self.regions):
            last = list(self.regions)[-1]
            if not last.intersect(self.monitor.bounds).is_empty:
                anchor = last  # several regions, none being drawn: the bar sits by the last one
        if anchor is None or self.style.measure or not self.style.action_bar:
            self.action_bar.hide()
            return
        self.action_bar.set_multi(len(self.regions.final(snap.selection)) > 1)
        sel = self.to_logical(anchor)
        bar = self.action_bar.sizeHint()
        x = min(max(4.0, sel.right() - bar.width()), self.width() - bar.width() - 4.0)
        y = sel.bottom() + BAR_GAP
        if y + bar.height() > self.height() - 4:  # no room below: above, else inside
            y = sel.top() - BAR_GAP - bar.height()
            if y < 4:
                y = sel.bottom() - bar.height() - BAR_GAP
        self.action_bar.move(QPoint(round(x), round(y)))
        self.action_bar.show()
        self.action_bar.raise_()

    # -- input ----------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        p = self.to_physical(event.position())
        if event.button() == Qt.MouseButton.RightButton:
            if self.model.selection is not None and self.model.selection.contains(p):
                self.model.reset()
                self._changed()
            elif self.regions.remove_at(p) is not None:
                self.regions_changed.emit()
            elif self.model.selection is not None:
                self.model.reset()
                self._changed()
            elif not len(self.regions):
                self.cancelled.emit()  # with collected regions only Esc cancels, so they are not lost by accident
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.style.pick_color:
            self.confirmed.emit(Rect(p.x, p.y, 1, 1), ACTION_COLOR)
            return
        ctrl = self.multi_enabled and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self.pressed.emit(self, ctrl)
        if ctrl and self.model.state is State.ADJUSTING:
            # Ctrl always starts a new region: the current selection is kept, not moved
            self.regions.add(self.model.result())
            self.model.reset()
            self.regions_changed.emit()
        self.model.press(p)
        self._changed()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        p = self.to_physical(event.position())
        self._hovering = True
        self.model.move(p)
        hit = self.model.hit_test(p) if self.model.state is State.ADJUSTING else None
        self.setCursor(HANDLE_CURSORS.get(hit or "", Qt.CursorShape.CrossCursor))
        if self.model.state in (State.DRAGGING, State.MOVING, State.RESIZING):
            self.action_bar.hide()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            new_selection = self.model.state is State.DRAGGING
            self.model.release(self.to_physical(event.position()))
            ctrl = self.multi_enabled and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if new_selection and ctrl and self.model.state is State.ADJUSTING:
                self._collect()
                return
            self._changed()
            if new_selection and not self.style.action_bar and self.model.state is State.ADJUSTING:
                self._confirm(ACTION_DEFAULT)  # no action bar: the mode's Enter action, right away

    def focusNextPrevChild(self, next: bool) -> bool:  # noqa: N802, A002 - Tab cycles frames instead
        return False

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self.model.state is State.IDLE:
            self.model.cycle(1 if event.angleDelta().y() > 0 else -1)
            self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self.multi_enabled and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.mousePressEvent(event)  # quick Ctrl+clicks on windows: each one is a region
                return
            self._confirm(ACTION_DEFAULT)

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
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
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
            self._confirm(ACTION_DEFAULT)
        elif key == Qt.Key.Key_C and ctrl:
            self._confirm(ACTION_COPY)
        elif key == Qt.Key.Key_S and ctrl:
            self._confirm(ACTION_SAVE_AS)
        elif key == Qt.Key.Key_P and not ctrl:
            self._confirm(ACTION_PIN)
        elif key == Qt.Key.Key_S and not ctrl:
            self._confirm(ACTION_SCROLL)
        elif key == Qt.Key.Key_N and not ctrl:
            self._confirm(ACTION_STEPS)
        elif key == Qt.Key.Key_E and not ctrl:
            self._confirm(ACTION_EDIT)
        elif key == Qt.Key.Key_T and not ctrl:
            self._confirm(ACTION_OCR)
        elif key == Qt.Key.Key_R and not ctrl:
            self._confirm(ACTION_RECORD)
        elif key == Qt.Key.Key_M and not ctrl and self.action_bar.multi:
            self.clip_toggled.emit()
        elif key in arrows:
            dx, dy = arrows[key]
            self.model.nudge(dx, dy, resize=resize)
            if self.model.selection is None:
                # move the real mouse too, so pixel-precise picking works with the keyboard
                c = self.to_logical(Rect(self.model.cursor.x, self.model.cursor.y, 1, 1)).topLeft()
                QCursor.setPos(self.mapToGlobal(c.toPoint()))
            self._changed()
        elif key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and self.model.state is State.IDLE:
            self.model.cycle(-1 if key == Qt.Key.Key_Backtab else 1)
            self.update()
        elif key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) or (key == Qt.Key.Key_Z and ctrl):
            if self.regions.pop() is not None:
                self.regions_changed.emit()
        elif key == Qt.Key.Key_A and ctrl:
            self.pressed.emit(self, False)
            self.model.select_all()
            self._changed()
        else:
            super().keyPressEvent(event)

    def _confirm(self, action: str = ACTION_DEFAULT) -> None:
        if self.style.pick_color:
            c = self.model.cursor
            self.confirmed.emit(Rect(c.x, c.y, 1, 1), ACTION_COLOR)
            return
        if self.style.measure and action == ACTION_DEFAULT:
            action = ACTION_MEASURE
        current = self.model.result()
        if not len(self.regions):
            current = current or self.model.hover_rect()
        rects = self.regions.final(current)
        if len(rects) > 1:
            if action in MULTI_ACTIONS:
                self.confirmed.emit(rects, action)
            return  # pin, OCR, scroll... need a single region
        if rects:
            self.confirmed.emit(rects[0], action)

    # -- painting -------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(self.rect(), self._background)
        snap = self.model.snapshot()
        focus = snap.selection or (snap.hover if self._hovering else None)

        full = QRectF(self.rect())
        collected = self._local_regions()
        if collected:
            dim = QPainterPath()
            dim.addRect(full)
            cut = QPainterPath()
            for _n, r in collected:
                cut.addRect(self.to_logical(r))
            if focus is not None:
                cut.addRect(self.to_logical(focus))
            p.fillPath(dim.subtracted(cut.simplified()), self._dim)
            self._paint_collected(p, collected)
        if focus is not None:
            hole = self.to_logical(focus)
            if not collected:
                dim = QPainterPath()
                dim.addRect(full)
                cut = QPainterPath()
                cut.addRect(hole)
                p.fillPath(dim.subtracted(cut), self._dim)
            # Two-tone outline: readable on light and dark content alike.
            p.setBrush(Qt.BrushStyle.NoBrush)
            width = max(1, self.style.thickness)
            p.setPen(QPen(OUTLINE_DARK, width + 2))
            p.drawRect(hole.adjusted(-1, -1, 1, 1))
            p.setPen(QPen(self._accent, width))
            p.drawRect(hole.adjusted(-1, -1, 1, 1))
            self._paint_size_pill(p, focus, hole, is_window=snap.selection is None)
            if snap.selection is not None and snap.state is not State.DRAGGING:
                self._paint_handles(p)
        elif not collected:
            p.fillRect(full, self._dim)

        if len(self.regions) and self._hovering:
            self._paint_multi_status(p)
        if self._hovering and snap.state not in (State.ADJUSTING, State.MOVING):
            self._paint_crosshair(p, snap.cursor)
            if self.style.magnifier:
                self._paint_magnifier(p, snap.cursor)
        if self._hovering and snap.state is State.IDLE and not len(self.regions):
            self._paint_hint(p)
        p.end()

    def _paint_collected(self, p: QPainter, collected: list[tuple[int, Rect]]) -> None:
        """Regions collected with Ctrl: outlined and numbered in selection order."""
        width = max(1, self.style.thickness)
        p.setFont(ui_font(10, weight=QFont.Weight.DemiBold))
        ink = QColor("#000000") if self._accent.lightness() > 150 else QColor("#ffffff")
        for number, rect in collected:
            hole = self.to_logical(rect)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(OUTLINE_DARK, width + 2))
            p.drawRect(hole.adjusted(-1, -1, 1, 1))
            p.setPen(QPen(self._accent, width, Qt.PenStyle.DashLine))
            p.drawRect(hole.adjusted(-1, -1, 1, 1))
            d = 24.0
            badge = QRectF(hole.left() + 6, hole.top() + 6, d, d)
            p.setPen(QPen(OUTLINE_DARK, 1))
            p.setBrush(self._accent)
            p.drawEllipse(badge)
            p.setPen(ink)
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(number))

    def _paint_multi_status(self, p: QPainter) -> None:
        text = self.translator.t("selector.multi.status", count=len(self.regions))
        p.setFont(ui_font(10))
        metrics = p.fontMetrics()
        w, h = min(self.width() - 32, metrics.horizontalAdvance(text) + 40), metrics.height() + 18
        self._pill(p, QRectF((self.width() - w) / 2, 24, w, h), text)
        mode = self.translator.t(f"selector.multi.clipboard.{self.action_bar.clip_mode}")
        clip = self.translator.t("selector.multi.clipboard.status", mode=mode)
        w2 = min(self.width() - 32, metrics.horizontalAdvance(clip) + 40)
        self._pill(p, QRectF((self.width() - w2) / 2, 24 + h + 6, w2, h), clip)

    def _paint_handles(self, p: QPainter) -> None:
        p.setPen(QPen(self._accent, 2))
        p.setBrush(QColor("white"))
        for handle in self.model.handle_rects().values():
            c = self.to_logical(handle).center()
            p.drawEllipse(c, 5, 5)

    def _pill(self, p: QPainter, rect: QRectF, text: str, *, color: QColor = CHROME_TEXT) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(CHROME_BG)
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        p.setPen(color)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_size_pill(self, p: QPainter, rect: Rect, hole: QRectF, *, is_window: bool) -> None:
        if self.style.measure:
            diagonal = round((rect.width**2 + rect.height**2) ** 0.5)
            text = self.translator.t("measure.size", width=rect.width, height=rect.height, diagonal=diagonal)
        else:
            text = self.translator.t("selector.size", width=rect.width, height=rect.height)
        p.setFont(ui_font(9))
        metrics = p.fontMetrics()
        w, h = metrics.horizontalAdvance(text) + 20, metrics.height() + 8
        y = hole.top() - h - 8 if hole.top() - h - 8 > 0 else hole.top() + 8
        self._pill(p, QRectF(hole.left(), y, w, h), text)

    def _paint_crosshair(self, p: QPainter, cursor: Point) -> None:
        c = self.to_logical(Rect(cursor.x, cursor.y, 1, 1)).center()
        pen = QPen(QColor(255, 255, 255, 70), 1, Qt.PenStyle.DashLine)
        p.setPen(pen)
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
        coords = self.translator.t("selector.position", x=cursor.x, y=cursor.y)
        p.setFont(ui_font(9))
        info_h = p.fontMetrics().height() + 12

        c = self.to_logical(Rect(cursor.x, cursor.y, 1, 1)).center()
        x, y = c.x() + 28, c.y() + 28
        if x + zoom + 8 > self.width():
            x = c.x() - 28 - zoom
        if y + zoom + info_h + 8 > self.height():
            y = c.y() - 28 - zoom - info_h
        panel = QRectF(x - 4, y - 4, zoom + 8, zoom + info_h + 8)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(CHROME_BG)
        p.drawRoundedRect(panel, 10, 10)

        frame = QRectF(x, y, zoom, zoom)
        clip = QPainterPath()
        clip.addRoundedRect(frame, 7, 7)
        p.save()
        p.setClipPath(clip)
        p.drawImage(frame, sample)
        p.setPen(QPen(QColor(255, 255, 255, 28), 1))  # pixel grid
        for i in range(1, MAGNIFIER_SOURCE):
            o = i * MAGNIFIER_ZOOM
            p.drawLine(QPointF(x + o, y), QPointF(x + o, y + zoom))
            p.drawLine(QPointF(x, y + o), QPointF(x + zoom, y + o))
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self._accent, 2))
        p.drawRect(QRectF(x + half * MAGNIFIER_ZOOM, y + half * MAGNIFIER_ZOOM, MAGNIFIER_ZOOM, MAGNIFIER_ZOOM))

        info = QRectF(x, y + zoom + 2, zoom, info_h - 4)
        swatch = QRectF(info.left() + 6, info.center().y() - 6, 12, 12)
        p.setPen(QPen(QColor(255, 255, 255, 120), 1))
        p.setBrush(color)
        p.drawRoundedRect(swatch, 3, 3)
        p.setPen(CHROME_TEXT)
        p.drawText(info.adjusted(24, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, color.name().upper())
        p.setPen(CHROME_MUTED)
        p.drawText(info.adjusted(0, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, coords)

    def _paint_hint(self, p: QPainter) -> None:
        text = self.translator.t("selector.hint")
        p.setFont(ui_font(10))
        metrics = p.fontMetrics()
        w, h = min(self.width() - 32, metrics.horizontalAdvance(text) + 40), metrics.height() + 18
        y = self.height() - h - 36
        self._pill(p, QRectF((self.width() - w) / 2, y, w, h), text)
        if self.style.mode_label:
            label_text = self.style.mode_label
            p.setFont(ui_font(10.5, weight=QFont.Weight.DemiBold))
            lw = p.fontMetrics().horizontalAdvance(label_text) + 36
            rect = QRectF((self.width() - lw) / 2, y - h - 10, lw, h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._accent)
            p.drawRoundedRect(rect, h / 2, h / 2)
            p.setPen(QColor("#000000") if self._accent.lightness() > 150 else QColor("#ffffff"))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label_text)


class RegionSelector(QObject):
    """Shows overlays on all monitors and reports the chosen rectangle and action."""

    finished = Signal(object)  # (Rect, action), (list[Rect], action) for several regions, or None
    clip_mode_changed = Signal(str)  # the user switched how several regions are copied

    def __init__(self, translator: Translator, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.translator = translator
        self.overlays: list[MonitorOverlay] = []
        self.frozen: CaptureResult | None = None
        self.regions = RegionList()
        self.clip_mode = CLIP_SEQUENCE
        self.clip_locked = False

    @property
    def active(self) -> bool:
        return bool(self.overlays)

    def start(
        self,
        frozen: CaptureResult,
        monitors: Sequence[Monitor],
        windows: Sequence[WindowInfo],
        frames: Callable[[Point, WindowInfo | None], Sequence[Rect]] | None = None,
        style: SelectorStyle | None = None,
        *,
        clip_mode: str = CLIP_SEQUENCE,
        clip_locked: bool = False,
    ) -> None:
        if self.overlays:
            return
        self.frozen = frozen
        self.regions = RegionList()
        self.clip_mode = CLIP_COMBINED if clip_mode == CLIP_COMBINED else CLIP_SEQUENCE
        self.clip_locked = clip_locked
        image = qimage_from_captured(frozen.image)
        origin = Point(frozen.region.x, frozen.region.y)
        cursor = QCursor.pos()
        for monitor, screen in match_screens(monitors, QGuiApplication.screens()):
            overlay = MonitorOverlay(monitor, screen, image, origin, windows, self.translator, frames, style, self.regions)
            overlay.pressed.connect(self._on_pressed)
            overlay.regions_changed.connect(self._on_regions_changed)
            overlay.confirmed.connect(lambda rect, action: self._finish((rect, action)))
            overlay.cancelled.connect(lambda: self._finish(None))
            overlay.clip_toggled.connect(self.toggle_clip_mode)
            overlay.action_bar.set_clip_mode(self.clip_mode, locked=clip_locked)
            self.overlays.append(overlay)
            overlay.show()
            if screen.geometry().contains(cursor):
                overlay.set_hovering(True)
                overlay.model.move(overlay.to_physical(QPointF(overlay.mapFromGlobal(cursor))))
                self._focus(overlay)
                QTimer.singleShot(0, lambda o=overlay: self._focus(o) if any(o is x for x in self.overlays) else None)
        if not self.overlays:
            log.error("No screen could be matched to a monitor")
            self.finished.emit(None)

    @staticmethod
    def _focus(overlay: "MonitorOverlay") -> None:
        """Take keyboard focus from the app in front (Excel, a browser...), so
        arrows, Enter and Esc reach the selector even when Windows refused the
        plain activation request."""
        from screenpilot.platform.win32_window import force_foreground

        overlay.raise_()
        overlay.activateWindow()
        force_foreground(int(overlay.winId()))
        overlay.setFocus()

    def cancel(self) -> None:
        if self.overlays:
            self._finish(None)

    def toggle_clip_mode(self) -> None:
        """Several regions: switch between one by one and one image, before Enter."""
        if self.clip_locked:
            return
        self.clip_mode = CLIP_SEQUENCE if self.clip_mode == CLIP_COMBINED else CLIP_COMBINED
        for overlay in self.overlays:
            overlay.action_bar.set_clip_mode(self.clip_mode)
            overlay._changed()
        self.clip_mode_changed.emit(self.clip_mode)

    def _on_pressed(self, source: MonitorOverlay, keep: bool = False) -> None:
        for overlay in self.overlays:
            if overlay is not source:
                overlay.release_selection(keep)
        if keep:
            self._on_regions_changed()

    def _on_regions_changed(self) -> None:
        for overlay in self.overlays:
            overlay._changed()

    def _finish(self, outcome: tuple[Rect, str] | None) -> None:
        overlays, self.overlays = self.overlays, []
        for overlay in overlays:
            overlay.close()
        self.finished.emit(outcome)
