"""Video editor window: trim a recording and add timed annotations.

Layout: tools + colours on top, the frame preview in the middle, transport
(play, ±1 s, start/end here) and the timeline below, export at the bottom.

* Preview frames are read in a worker thread (latest request wins), so
  scrubbing never blocks the window. Annotations are drawn on top with the
  same Pillow renderer as the export, from cached patches.
* Timeline: drag the grey handles to trim, drag a coloured bar to move an
  annotation in time, drag its ends to change when it appears / disappears,
  click anywhere else to jump there.
* Export runs in a worker thread with a progress bar and Cancel; the result
  is a new file next to the source (``<name>_edited``), the source is kept.

Keyboard: Space play/pause · ←/→ 0.1 s (Shift: 1 s) · Home/End · I / O set
start / end at the playhead · V select · N step · A arrow · R rectangle ·
H highlight · T text · B blur · Del delete · Ctrl+Z / Ctrl+Y · Ctrl+S save
video · Esc deselect.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QMouseEvent, QPainter, QPaintEvent, QPen, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from screenpilot.capture.geometry import Point, Rect
from screenpilot.core.errors import ScreenPilotError
from screenpilot.editor.document import COLORS, hit_shape, step_diameter, text_bounds
from screenpilot.i18n import Translator
from screenpilot.ui import icons
from screenpilot.ui.qt_helpers import Retranslator, app_icon, fit_target
from screenpilot.ui.widgets import IconButton, label, on_theme_changed, theme
from screenpilot.videoedit.media import AnnotationPainter, blur_region
from screenpilot.videoedit.model import TimedAnnotation, VideoProject, assign_lanes, format_time

if TYPE_CHECKING:  # pragma: no cover
    from screenpilot.app import ScreenPilotApp

log = logging.getLogger(__name__)

TOOLS = (
    ("select", "select", "V"),
    ("step", "steps", "N"),
    ("arrow", "arrow", "A"),
    ("rect", "rect", "R"),
    ("highlight", "highlight", "H"),
    ("text", "text", "T"),
    ("blur", "blur", "B"),
)
DRAG_TOOLS = {"arrow", "rect", "highlight", "blur"}
PLAY_MS = 100
HANDLE_PX = 7
LANE_H = 16
EDGE_PX = 6


def pil_to_qimage(image) -> QImage:
    rgb = image.convert("RGB")
    data = rgb.tobytes()
    return QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888).copy()


def rgba_to_qimage(image) -> QImage:
    data = image.tobytes("raw", "RGBA")
    return QImage(data, image.width, image.height, image.width * 4, QImage.Format.Format_RGBA8888).copy()


class FrameLoader(QObject):
    """Reads preview frames in a background thread; only the newest request is served."""

    loaded = Signal(float, object)  # source time, Pillow RGB image
    failed = Signal(str)

    def __init__(self, reader) -> None:
        super().__init__()
        self.reader = reader
        self._wanted: float | None = None
        self._cv = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="video-frames", daemon=True)
        self._thread.start()

    def request(self, t: float) -> None:
        with self._cv:
            self._wanted = t
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._wanted is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                t, self._wanted = self._wanted, None
            try:
                frame = self.reader.frame(t)
            except Exception as exc:  # noqa: BLE001 - shown in the window, never crashes it
                log.warning("Preview frame at %.2fs failed: %s", t, exc)
                self.failed.emit(str(exc))
                continue
            self.loaded.emit(t, frame)

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()
        close = getattr(self.reader, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


class VideoCanvas(QWidget):
    """The frame at the playhead with the annotations visible at that time."""

    selection_changed = Signal(object)  # annotation id | None
    changed = Signal()

    def __init__(self, project: VideoProject, translator: Translator) -> None:
        super().__init__()
        self.project = project
        self.t = translator.t
        self.tool = "step"
        self.color = COLORS["red"]
        self.duration_s = 3.0
        self.time = 0.0
        self.selected: int | None = None
        self._frame = None  # Pillow image at self.time
        self._pixmap = QImage()
        self._painter = AnnotationPainter(project.info.width, project.info.height)
        self._qpatches: dict[tuple, QImage] = {}
        self._press: Point | None = None
        self._origin: TimedAnnotation | None = None
        self._draft = None
        self.setMinimumSize(320, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    # -- frame ------------------------------------------------------------------
    def set_frame(self, t: float, frame) -> None:
        self._frame = frame
        self._frame_time = t
        self._pixmap = pil_to_qimage(frame)
        self.update()

    def set_time(self, t: float) -> None:
        self.time = t
        self.update()

    # -- mapping ------------------------------------------------------------------
    def target(self) -> QRectF:
        return fit_target(self, self.project.info.width, self.project.info.height, margin=6)

    def to_video(self, pos: QPointF) -> Point:
        r = self.target()
        sx = self.project.info.width / max(1.0, r.width())
        return Point(round((pos.x() - r.left()) * sx), round((pos.y() - r.top()) * sx))

    def to_view(self, rect: Rect) -> QRectF:
        r = self.target()
        s = r.width() / self.project.info.width
        return QRectF(r.left() + rect.x * s, r.top() + rect.y * s, rect.width * s, rect.height * s)

    # -- tools ------------------------------------------------------------------
    def set_tool(self, tool: str) -> None:
        self.tool = tool
        if tool != "select":
            self.select(None)
        self.setCursor(Qt.CursorShape.ArrowCursor if tool == "select" else Qt.CursorShape.CrossCursor)

    def select(self, ann_id: int | None) -> None:
        if ann_id != self.selected:
            self.selected = ann_id
            self.selection_changed.emit(ann_id)
            self.update()

    def visible(self) -> list[TimedAnnotation]:
        return self.project.active_at(self.time)

    def hit(self, p: Point) -> TimedAnnotation | None:
        for ann in reversed(self.visible()):
            if hit_shape(ann.shape, p, slack=8):
                return ann
        return None

    def delete_selected(self) -> None:
        if self.selected is not None and self.project.remove(self.selected):
            self.select(None)
            self.changed.emit()

    def _new_times(self) -> tuple[float, float]:
        start = self.time
        return start, min(self.project.duration, start + self.duration_s)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        p = self.to_video(event.position())
        if self.tool == "select":
            ann = self.hit(p)
            self.select(ann.id if ann else None)
            if ann is not None:
                self.project.begin_change()
                self._press, self._origin = p, ann
            return
        if self.tool == "step":
            ann = self.project.add(self.project.make_shape("step", p, color=self.color), *self._new_times())
            self.select(ann.id)
            self.changed.emit()
            return
        if self.tool == "text":
            text, ok = QInputDialog.getText(self, self.t("video.text.title"), self.t("video.text.prompt"))
            if ok and text.strip():
                ann = self.project.add(self.project.make_shape("text", p, text=text.strip(), color=self.color), *self._new_times())
                self.select(ann.id)
                self.changed.emit()
            return
        self._press = p

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press is None:
            return
        p = self.to_video(event.position())
        if self._origin is not None:  # moving a selected annotation
            dx, dy = p.x - self._press.x, p.y - self._press.y
            self.project.update(self._origin.id, shape=self._origin.shape.moved(dx, dy), record=False)
            self.changed.emit()
        else:
            self._draft = self.project.make_shape(self.tool, self._press, p, color=self.color)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press is None:
            return
        p = self.to_video(event.position())
        press, self._press, origin, self._origin, self._draft = self._press, None, self._origin, None, None
        if origin is not None:
            self.project.end_change()  # a plain click moved nothing: no empty undo step
            self.changed.emit()
            return
        if max(abs(p.x - press.x), abs(p.y - press.y)) >= 6:
            ann = self.project.add(self.project.make_shape(self.tool, press, p, color=self.color), *self._new_times())
            self.select(ann.id)
        self.changed.emit()
        self.update()

    # -- painting -----------------------------------------------------------------
    def _patch_image(self, ann: TimedAnnotation, numbers: dict[int, int]) -> tuple[QImage, Rect] | None:
        if ann.kind == "blur":
            if self._frame is None:
                return None
            b = ann.shape.bounds.intersect(Rect(0, 0, *self._frame.size))
            if b.width < 2 or b.height < 2:
                return None
            key = ("blur", id(self._frame), ann.shape)
            if key not in self._qpatches:
                region = self._frame.crop((b.x, b.y, b.right, b.bottom))
                blur_region(region, ann.shape.moved(-b.x, -b.y))
                self._qpatches[key] = pil_to_qimage(region)
            return self._qpatches[key], b
        number = numbers.get(ann.id)
        key = (ann.shape, number)
        patch = self._painter.patch(ann.shape, number)
        if patch is None:
            return None
        image, rect = patch
        if key not in self._qpatches:
            self._qpatches[key] = rgba_to_qimage(image)
        return self._qpatches[key], rect

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(t.surface_alt))
        target = self.target()
        if not self._pixmap.isNull():
            p.drawImage(target, self._pixmap)
        if len(self._qpatches) > 400:
            self._qpatches.clear()
        numbers = self.project.step_numbers()
        for ann in self.visible():
            patch = self._patch_image(ann, numbers)
            if patch is not None:
                image, rect = patch
                p.drawImage(self.to_view(rect), image)
        if self._draft is not None:
            from screenpilot.videoedit.model import TimedAnnotation as _T

            patch = self._patch_image(_T(0, self._draft, 0, 0), {})
            if patch is not None:
                p.drawImage(self.to_view(patch[1]), patch[0])
        ann = self.project.get(self.selected) if self.selected is not None else None
        if ann is not None and ann.active(self.time):
            s = ann.shape
            if s.kind == "text":
                b = text_bounds(s)
            elif s.kind == "step":
                d = step_diameter(s)
                b = Rect(s.p1.x - d // 2, s.p1.y - d // 2, d, d)
            else:
                b = s.bounds
            p.setPen(QPen(QColor(t.accent), 1, Qt.PenStyle.DashLine))
            p.drawRect(self.to_view(b).adjusted(-4, -4, 4, 4))
        p.end()


class Timeline(QWidget):
    """Trim handles, one bar per annotation (rows for overlaps) and the playhead."""

    seek = Signal(float)
    selected = Signal(object)  # annotation id | None
    changed = Signal()

    def __init__(self, project: VideoProject) -> None:
        super().__init__()
        self.project = project
        self.time = 0.0
        self.selected_id: int | None = None
        self._drag: tuple[str, int | None, float, float, float] | None = None  # mode, id, press t, start, end
        self.setMouseTracking(True)
        self.setMinimumHeight(76)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._refresh_height()

    def _refresh_height(self) -> None:
        lanes = assign_lanes(self.project.annotations)
        rows = (max(lanes.values()) + 1) if lanes else 1
        self.setFixedHeight(46 + rows * (LANE_H + 3))

    # -- mapping ------------------------------------------------------------------
    def _x(self, t: float) -> float:
        span = max(1e-6, self.project.duration)
        return HANDLE_PX + (self.width() - 2 * HANDLE_PX) * (t / span)

    def _t(self, x: float) -> float:
        span = self.project.duration
        return self.project.clamp_time((x - HANDLE_PX) / max(1.0, self.width() - 2 * HANDLE_PX) * span)

    def _bar_rect(self, ann: TimedAnnotation, lane: int) -> QRectF:
        y = 38 + lane * (LANE_H + 3)
        x1, x2 = self._x(ann.start), self._x(ann.end)
        return QRectF(x1, y, max(4.0, x2 - x1), LANE_H)

    def set_time(self, t: float) -> None:
        self.time = t
        self.update()

    def refresh(self) -> None:
        self._refresh_height()
        self.update()

    # -- input ----------------------------------------------------------------------
    def _hit(self, pos: QPointF) -> tuple[str, int | None]:
        x, y = pos.x(), pos.y()
        if y < 34:
            if abs(x - self._x(self.project.start)) <= HANDLE_PX + 2:
                return "trim_start", None
            if abs(x - self._x(self.project.end)) <= HANDLE_PX + 2:
                return "trim_end", None
            return "seek", None
        lanes = assign_lanes(self.project.annotations)
        for ann in self.project.annotations:
            r = self._bar_rect(ann, lanes.get(ann.id, 0))
            if r.adjusted(-EDGE_PX / 2, 0, EDGE_PX / 2, 0).contains(pos):
                if abs(x - r.left()) <= EDGE_PX:
                    return "ann_start", ann.id
                if abs(x - r.right()) <= EDGE_PX:
                    return "ann_end", ann.id
                return "ann_move", ann.id
        return "seek", None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        mode, ann_id = self._hit(event.position())
        t = self._t(event.position().x())
        if mode.startswith("ann"):
            ann = self.project.get(ann_id)
            self.selected_id = ann_id
            self.selected.emit(ann_id)
            self.project.begin_change()
            self._drag = (mode, ann_id, t, ann.start, ann.end)
        elif mode.startswith("trim"):
            self.project.begin_change()
            self._drag = (mode, None, t, self.project.start, self.project.end)
        else:
            self._drag = ("seek", None, t, 0, 0)
            self.seek.emit(t)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is None:
            mode, _ = self._hit(event.position())
            shapes = {"trim_start": Qt.CursorShape.SizeHorCursor, "trim_end": Qt.CursorShape.SizeHorCursor,
                      "ann_start": Qt.CursorShape.SizeHorCursor, "ann_end": Qt.CursorShape.SizeHorCursor,
                      "ann_move": Qt.CursorShape.OpenHandCursor}
            self.setCursor(shapes.get(mode, Qt.CursorShape.ArrowCursor))
            return
        mode, ann_id, t0, start, end = self._drag
        t = self._t(event.position().x())
        if mode == "seek":
            self.seek.emit(t)
            return
        if mode == "trim_start":
            self.project.set_trim(start=t, record=False)
            self.seek.emit(self.project.start)
        elif mode == "trim_end":
            self.project.set_trim(end=t, record=False)
            self.seek.emit(self.project.end)
        elif mode == "ann_move":
            length = end - start
            new_start = min(max(0.0, start + (t - t0)), self.project.duration - length)
            self.project.update(ann_id, start=new_start, end=new_start + length, record=False)
        elif mode == "ann_start":
            self.project.update(ann_id, start=min(t, end - 0.2), record=False)
            self.seek.emit(self.project.get(ann_id).start)
        elif mode == "ann_end":
            self.project.update(ann_id, end=max(t, start + 0.2), record=False)
            self.seek.emit(max(self.project.get(ann_id).start, self.project.get(ann_id).end - 0.05))
        self.changed.emit()
        self.refresh()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is not None and self._drag[0] != "seek":
            self.project.end_change()
        self._drag = None

    # -- painting ---------------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        th = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        track = QRectF(HANDLE_PX, 10, w - 2 * HANDLE_PX, 20)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(th.surface_alt))
        p.drawRoundedRect(track, 4, 4)
        x1, x2 = self._x(self.project.start), self._x(self.project.end)
        keep = QColor(th.accent)
        keep.setAlpha(70)
        p.setBrush(keep)
        p.drawRect(QRectF(x1, 10, x2 - x1, 20))
        # time ticks
        p.setPen(QPen(QColor(th.text_secondary), 1))
        span = self.project.duration
        step = next((s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if span / s <= max(4, w / 70)), 600)
        tick = 0.0
        font = p.font()
        font.setPointSizeF(7.5)
        p.setFont(font)
        while tick <= span + 1e-6:
            x = self._x(tick)
            p.drawLine(QPointF(x, 30), QPointF(x, 34))
            tick += step
        # trim handles
        for x in (x1, x2):
            p.setPen(QPen(QColor(th.text), 1))
            p.setBrush(QColor(th.surface))
            p.drawRoundedRect(QRectF(x - HANDLE_PX / 2, 6, HANDLE_PX, 28), 2, 2)
        # annotation bars
        lanes = assign_lanes(self.project.annotations)
        numbers = self.project.step_numbers()
        for ann in self.project.annotations:
            r = self._bar_rect(ann, lanes.get(ann.id, 0))
            color = QColor(ann.shape.color if ann.kind != "blur" else th.text_secondary)
            color.setAlpha(230 if ann.id == self.selected_id else 160)
            p.setPen(QPen(QColor(th.accent), 2) if ann.id == self.selected_id else Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(r, 4, 4)
            caption = str(numbers[ann.id]) if ann.id in numbers else (ann.shape.text[:18] if ann.kind == "text" else "")
            if caption and r.width() > 14:
                p.setPen(QColor("#ffffff"))
                p.drawText(r.adjusted(4, 0, -2, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, caption)
        # playhead
        x = self._x(self.time)
        p.setPen(QPen(QColor("#d13438"), 2))
        p.drawLine(QPointF(x, 2), QPointF(x, self.height() - 2))
        p.end()


class VideoEditorWindow(QWidget):
    exported = Signal(object)  # Path
    progress_changed = Signal(float)
    export_done = Signal(object, object)  # Path | None, error message | None

    def __init__(
        self,
        app: "ScreenPilotApp",
        project: VideoProject,
        *,
        reveal: Callable[[Path], None] | None = None,
        reader=None,
    ) -> None:
        super().__init__(None)
        self.app = app
        self.project = project
        self.t = app.translator.t
        self.reveal = reveal
        self.rt = rt = Retranslator(app.translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setProperty("page", True)
        self.setWindowIcon(app_icon())
        rt.bind(self.setWindowTitle, "video.title", name=project.source.name)
        self.resize(1180, 820)
        self._exported_path: Path | None = None
        self._cancel: threading.Event | None = None
        self._playing = False
        self._play_started = 0.0
        self._play_from = 0.0

        self.canvas = VideoCanvas(project, app.translator)
        self.canvas.color = (app.settings.get("recording.ink_color") if app.settings else None) or COLORS["red"]
        self.canvas.duration_s = float(app.settings.get("video.annotation_seconds") or 3) if app.settings else 3.0
        self.timeline = Timeline(project)

        # Tools and colours
        top = QHBoxLayout()
        top.setSpacing(4)
        self.tool_group = QButtonGroup(self)
        self.tool_buttons: dict[str, QToolButton] = {}
        for tool, icon_name, key in TOOLS:
            button = QToolButton()
            button.setCheckable(True)
            button.setProperty("icon_name", icon_name)
            button.setIconSize(QSize(20, 20))
            button.setFixedSize(38, 38)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            rt.bind(button.setToolTip, f"video.tool.{tool}", shortcut=key)
            button.clicked.connect(lambda _=False, tl=tool: self.set_tool(tl))
            self.tool_group.addButton(button)
            top.addWidget(button)
            self.tool_buttons[tool] = button
            QShortcut(QKeySequence(key), self, activated=lambda tl=tool: self.set_tool(tl))
        top.addSpacing(12)
        self.color_group = QButtonGroup(self)
        self.color_buttons: dict[str, QToolButton] = {}
        for name, hex_color in COLORS.items():
            if name == "white":
                continue
            swatch = QToolButton()
            swatch.setCheckable(True)
            swatch.setFixedSize(24, 24)
            swatch.setChecked(hex_color == self.canvas.color)
            swatch.clicked.connect(lambda _=False, c=hex_color: self.set_color(c))
            self.color_group.addButton(swatch)
            top.addWidget(swatch)
            self.color_buttons[hex_color] = swatch
        top.addStretch(1)
        self.undo_button = self._icon_button("undo", "editor.undo", self.undo)
        self.redo_button = self._icon_button("redo", "editor.redo", self.redo)
        self.delete_button = self._icon_button("trash", "video.delete", self.delete_selected)
        for b in (self.undo_button, self.redo_button, self.delete_button):
            top.addWidget(b)

        # Transport
        transport = QHBoxLayout()
        transport.setSpacing(4)
        self.play_button = self._icon_button("play", "video.play", self.toggle_play)
        back = self._icon_button("undo", "video.back", lambda: self.seek(self.canvas.time - 1))
        fwd = self._icon_button("redo", "video.forward", lambda: self.seek(self.canvas.time + 1))
        for b in (back, self.play_button, fwd):
            transport.addWidget(b)
        self.time_label = label(role="subtitle")
        self.time_label.setMinimumWidth(170)
        transport.addWidget(self.time_label)
        transport.addStretch(1)
        self.trim_label = label(role="caption")
        transport.addWidget(self.trim_label)
        for key, tip, slot in (
            ("video.trim_start", "video.trim_start.tip", self.trim_start_here),
            ("video.trim_end", "video.trim_end.tip", self.trim_end_here),
            ("video.trim_reset", None, self.reset_trim),
        ):
            button = QPushButton()
            rt.bind(button.setText, key)
            if tip:
                rt.bind(button.setToolTip, tip)
            button.clicked.connect(slot)
            transport.addWidget(button)

        # Selected annotation times
        details = QHBoxLayout()
        self.hint = label(role="caption")
        details.addWidget(self.hint, 1)
        self.from_label = label(role="secondary")
        rt.bind(self.from_label.setText, "video.from")
        self.from_spin = self._time_spin(lambda v: self._set_selected_times(start=v))
        self.to_label = label(role="secondary")
        rt.bind(self.to_label.setText, "video.to")
        self.to_spin = self._time_spin(lambda v: self._set_selected_times(end=v))
        for w in (self.from_label, self.from_spin, self.to_label, self.to_spin):
            details.addWidget(w)
        details.addSpacing(16)
        length_label = label(role="secondary")
        rt.bind(length_label.setText, "video.new_length")
        details.addWidget(length_label)
        self.length_spin = QDoubleSpinBox()
        self.length_spin.setRange(0.5, 600)
        self.length_spin.setSingleStep(0.5)
        self.length_spin.setDecimals(1)
        self.length_spin.setSuffix(" s")
        self.length_spin.setValue(self.canvas.duration_s)
        self.length_spin.valueChanged.connect(self._set_new_length)
        details.addWidget(self.length_spin)

        # Export row
        bottom = QHBoxLayout()
        self.status = label(role="caption", wrap=True)
        bottom.addWidget(self.status, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(220)
        self.progress.hide()
        bottom.addWidget(self.progress)
        self.buttons: dict[str, QPushButton] = {}
        for name, key, slot, accent, tip in (
            ("project", "video.save_project", self.save_project, False, "video.save_project.tip"),
            ("folder", "video.show_folder", self.show_folder, False, None),
            ("cancel", "video.cancel_export", self.cancel_export, False, None),
            ("close", "video.close", self.close, False, None),
            ("export", "video.export", self.export, True, "video.export.tip"),
        ):
            button = QPushButton()
            button.setProperty("accent", accent)
            rt.bind(button.setText, key)
            if tip:
                rt.bind(button.setToolTip, tip)
            button.clicked.connect(slot)
            bottom.addWidget(button)
            self.buttons[name] = button
        self.buttons["cancel"].hide()
        self.buttons["folder"].hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(transport)
        layout.addWidget(self.timeline)
        layout.addLayout(details)
        layout.addLayout(bottom)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.toggle_play)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=lambda: self.seek(self.canvas.time - 0.1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=lambda: self.seek(self.canvas.time + 0.1))
        QShortcut(QKeySequence("Shift+Left"), self, activated=lambda: self.seek(self.canvas.time - 1))
        QShortcut(QKeySequence("Shift+Right"), self, activated=lambda: self.seek(self.canvas.time + 1))
        QShortcut(QKeySequence(Qt.Key.Key_Home), self, activated=lambda: self.seek(self.project.start))
        QShortcut(QKeySequence(Qt.Key.Key_End), self, activated=lambda: self.seek(self.project.end - 0.05))
        QShortcut(QKeySequence("I"), self, activated=self.trim_start_here)
        QShortcut(QKeySequence("O"), self, activated=self.trim_end_here)
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence.StandardKey.Delete, self, activated=self.delete_selected)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.export)
        from screenpilot.ui.qt_helpers import on_escape

        on_escape(self, lambda: self.canvas.select(None) if self.canvas.selected is not None else self.close())

        self.canvas.changed.connect(self._project_changed)
        self.canvas.selection_changed.connect(self._on_selection)
        self.timeline.seek.connect(self.seek)
        self.timeline.selected.connect(self._select_from_timeline)
        self.timeline.changed.connect(self._project_changed)
        self.progress_changed.connect(lambda v: self.progress.setValue(int(v * 1000)))
        self.export_done.connect(self._export_finished)
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(PLAY_MS)
        self._play_timer.timeout.connect(self._play_tick)

        self.loader = FrameLoader(reader or app.video.reader(project))
        self.loader.loaded.connect(self._frame_loaded)
        self.loader.failed.connect(lambda message: self.status.setText(self.t("video.error.frame")))
        on_theme_changed(self, self._restyle)
        rt.on_change(self._refresh_labels)
        self.set_tool("step")
        self.seek(0.0)
        self._on_selection(None)

    # -- helpers ------------------------------------------------------------------------
    def _icon_button(self, icon_name: str, tip_key: str, slot) -> QToolButton:
        button = IconButton(icon_name, size=18)
        self.rt.bind(button.setToolTip, tip_key)
        button.clicked.connect(slot)
        return button

    def _time_spin(self, on_change) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0, max(0.2, self.project.duration))
        spin.setDecimals(1)
        spin.setSingleStep(0.1)
        spin.setSuffix(" s")
        spin.valueChanged.connect(lambda v: None if getattr(self, "_syncing", False) else on_change(float(v)))
        return spin

    def _restyle(self, t) -> None:
        for button in self.tool_buttons.values():
            button.setIcon(icons.icon(button.property("icon_name"), t.text, 20))
        for hex_color, swatch in self.color_buttons.items():
            border = t.text if swatch.isChecked() else t.border_strong
            swatch.setStyleSheet(
                f"QToolButton {{ background: {hex_color}; border: 2px solid {border}; border-radius: 12px; }}"
                f" QToolButton:checked {{ border: 3px solid {t.accent}; }}"
            )

    def _refresh_labels(self) -> None:
        p = self.project
        self.time_label.setText(f"{format_time(self.canvas.time)} / {format_time(p.duration)}")
        self.trim_label.setText(
            self.t("video.trim_label", start=format_time(p.start), end=format_time(p.end), length=format_time(p.output_duration))
        )
        self.undo_button.setEnabled(p.can_undo)
        self.redo_button.setEnabled(p.can_redo)
        self.delete_button.setEnabled(self.canvas.selected is not None)
        if not self.progress.isVisible() and self._exported_path is None:
            self.status.setText(self.t("video.hint", count=len(p.annotations)))

    # -- state ------------------------------------------------------------------------------
    def _set_new_length(self, value: float) -> None:
        self.canvas.duration_s = float(value)
        if self.app.settings is not None:
            self.app.settings.set("video.annotation_seconds", float(value))

    def set_tool(self, tool: str) -> None:
        self.canvas.set_tool(tool)
        self.tool_buttons[tool].setChecked(True)
        self.hint.setText(self.t(f"video.hint.{tool}"))

    def set_color(self, color: str) -> None:
        self.canvas.color = color
        self._restyle(theme())
        ann = self.project.get(self.canvas.selected) if self.canvas.selected is not None else None
        if ann is not None:
            from dataclasses import replace

            self.project.update(ann.id, shape=replace(ann.shape, color=color))
            self._project_changed()

    def seek(self, t: float) -> None:
        t = self.project.clamp_time(t)
        self.canvas.set_time(t)
        self.timeline.set_time(t)
        self.loader.request(t)
        self._refresh_labels()

    def _frame_loaded(self, t: float, frame) -> None:
        self.canvas.set_frame(t, frame)

    def toggle_play(self) -> None:
        if self._playing:
            self._playing = False
            self._play_timer.stop()
            self.play_button.set_icon("play")
            return
        start = self.canvas.time
        if start >= self.project.end - 0.05 or start < self.project.start:
            start = self.project.start
        self._playing = True
        self._play_from, self._play_started = start, time.monotonic()
        self.play_button.set_icon("pause")
        self._play_timer.start()

    def _play_tick(self) -> None:
        t = self._play_from + (time.monotonic() - self._play_started)
        if t >= self.project.end:
            self.toggle_play()
            t = self.project.end - 0.05
        self.seek(t)

    def _project_changed(self) -> None:
        self._exported_path = None
        self.buttons["folder"].hide()
        self.timeline.refresh()
        self.canvas.update()
        self._on_selection(self.canvas.selected)
        self._refresh_labels()

    def _select_from_timeline(self, ann_id) -> None:
        self.set_tool("select")
        self.canvas.select(ann_id)
        ann = self.project.get(ann_id) if ann_id is not None else None
        if ann is not None and not ann.active(self.canvas.time):
            self.seek(ann.start)

    def _on_selection(self, ann_id) -> None:
        ann = self.project.get(ann_id) if ann_id is not None else None
        self.timeline.selected_id = ann_id
        self.timeline.update()
        self._syncing = True
        try:
            for spin, value in ((self.from_spin, ann.start if ann else 0.0), (self.to_spin, ann.end if ann else 0.0)):
                spin.setValue(value)
                spin.setEnabled(ann is not None)
        finally:
            self._syncing = False
        self.delete_button.setEnabled(ann is not None)

    def _set_selected_times(self, *, start: float | None = None, end: float | None = None) -> None:
        if self.canvas.selected is not None:
            self.project.update(self.canvas.selected, start=start, end=end)
            self._project_changed()

    def trim_start_here(self) -> None:
        self.project.set_trim(start=self.canvas.time)
        self._project_changed()

    def trim_end_here(self) -> None:
        self.project.set_trim(end=self.canvas.time)
        self._project_changed()

    def reset_trim(self) -> None:
        self.project.reset_trim()
        self._project_changed()

    def undo(self) -> None:
        if self.project.undo():
            if self.canvas.selected is not None and self.project.get(self.canvas.selected) is None:
                self.canvas.select(None)
            self._project_changed()

    def redo(self) -> None:
        if self.project.redo():
            self._project_changed()

    def delete_selected(self) -> None:
        self.canvas.delete_selected()
        self._project_changed()

    # -- files ------------------------------------------------------------------------------------
    def save_project(self) -> Path | None:
        suggested = self.project.source.with_suffix(".screenpilot.json")
        path, _ = QFileDialog.getSaveFileName(self, self.t("video.save_project"), str(suggested), "JSON (*.json)")
        if not path:
            return None
        saved = self.project.save(Path(path))
        self.status.setText(self.t("video.project_saved", path=saved))
        return saved

    def export(self, output: Path | None = None) -> None:
        if self._cancel is not None:
            return
        if self._playing:
            self.toggle_play()
        output = output or self.app.video.default_output(self.project)
        self._cancel = threading.Event()
        self.progress.setValue(0)
        self.progress.show()
        self.buttons["cancel"].show()
        self.buttons["export"].setEnabled(False)
        self.buttons["folder"].hide()
        self.status.setText(self.t("video.exporting", name=output.name))
        cancel = self._cancel

        def work() -> None:
            try:
                path = self.app.video.export(self.project, output, progress=self.progress_changed.emit, cancel=cancel)
            except ScreenPilotError as exc:
                self.export_done.emit(None, exc.message_key or "video.error.export")
            except Exception as exc:  # noqa: BLE001
                log.exception("Video export failed")
                self.export_done.emit(None, str(exc))
            else:
                self.export_done.emit(path, None)

        threading.Thread(target=work, name="video-export", daemon=True).start()

    def cancel_export(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _export_finished(self, path: Path | None, error: str | None) -> None:
        self._cancel = None
        self.progress.hide()
        self.buttons["cancel"].hide()
        self.buttons["export"].setEnabled(True)
        if path is None:
            if error == "video.export.cancelled":
                self.status.setText(self.t(error))
            elif error and error.startswith(("video.", "recording.", "policy.")):
                self.status.setText(self.t("video.export_failed", reason=self.t(error)))
            else:
                self.status.setText(self.t("video.export_failed", reason=error or ""))
            return
        self._exported_path = Path(path)
        self.status.setText(self.t("video.exported", path=path))
        self.buttons["folder"].show()
        self.exported.emit(Path(path))

    def show_folder(self) -> None:
        if self._exported_path is not None and self.reveal is not None:
            self.reveal(self._exported_path)

    # -- closing -------------------------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        if self._cancel is not None:
            self._cancel.set()
        if self.project.edited and self._exported_path is None and not getattr(self, "_force_close", False):
            answer = QMessageBox.question(
                self, self.t("video.title", name=self.project.source.name), self.t("video.discard.question"),
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Discard:
                event.ignore()
                return
        self._play_timer.stop()
        self.loader.stop()
        super().closeEvent(event)

    def force_close(self) -> None:
        self._force_close = True
        self.close()
