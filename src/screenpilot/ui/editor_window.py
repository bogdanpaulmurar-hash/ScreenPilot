"""Image editor window (Editor v2).

Tools: select/move/resize, arrow, line, rectangle, ellipse, highlighter, text,
step numbers (attached to detected elements), symbols and emoji, blur,
pixelate, spotlight and crop. Pictures can be placed on top of the capture
(from a file, the clipboard, recent captures or by drag and drop) and moved,
resized and stacked. Rendering always goes through
:func:`screenpilot.editor.document.render` (Pillow), so what you see is
exactly what is saved, copied or exported.

Keyboard: V select · A arrow · L line · R rectangle · O ellipse · H highlight
· T text · N steps · E symbols · B blur · X pixelate · S spotlight · C crop ·
Del delete · arrows nudge (Shift ×10) · Ctrl+Z / Ctrl+Y undo/redo ·
Ctrl+C copy · Ctrl+Shift+C or Ctrl+Shift+Enter copy and close · Ctrl+V paste a picture ·
Ctrl+D duplicate · Ctrl+] / Ctrl+[ front/back · Ctrl+S save as ·
Enter done · Esc deselects, then closes (asking when something was drawn).

Zoom: Ctrl+wheel at the cursor (up to 800 %), Ctrl+= / Ctrl+- , Ctrl+0 fit,
Ctrl+1 100 %; when zoomed in the wheel scrolls (Shift: sideways) and dragging
with the wheel button pans.
"""

from __future__ import annotations

import io
import logging
from dataclasses import replace
from pathlib import Path
from typing import Callable

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from screenpilot.capture.detect import EdgeDetector
from screenpilot.capture.geometry import Point, Rect
from screenpilot.capture.image import CapturedImage, CaptureResult
from screenpilot.editor.document import (
    BOX_KINDS,
    COLORS,
    PALETTE,
    SCALED_KINDS,
    SYMBOLS,
    EditorDocument,
    Shape,
    box_handles,
    render,
    resize_box,
    scaled,
    shape_bounds,
)
from screenpilot.i18n import Translator
from screenpilot.ui import icons
from screenpilot.ui.qt_helpers import Retranslator, app_icon, on_escape, qimage_from_captured
from screenpilot.ui.widgets import Chip, label, on_theme_changed, theme

log = logging.getLogger(__name__)

TOOLS = (
    # id, icon, shortcut key
    ("select", "select", "V"),
    ("arrow", "arrow", "A"),
    ("line", "line", "L"),
    ("rect", "rect", "R"),
    ("ellipse", "ellipse", "O"),
    ("highlight", "highlight", "H"),
    ("text", "text", "T"),
    ("step", "steps", "N"),
    ("emoji", "emoji", "E"),
    ("blur", "blur", "B"),
    ("pixelate", "pixelate", "X"),
    ("spotlight", "spotlight", "S"),
    ("crop", "crop", "C"),
)
DRAG_TOOLS = {"arrow", "line", "rect", "ellipse", "highlight", "blur", "pixelate", "spotlight"}
RENDER_THROTTLE_MS = 16
ZOOM_STEP = 1.2  # per wheel notch with Ctrl
MAX_PIXEL_SCALE = 8.0  # up to 800 %
WHEEL_PAN = 0.5  # view pixels per wheel unit (a notch is 120) when zoomed in
HANDLE_PX = 5
MIN_SIZE = 6
RECENT_COLORS = 8
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")
EMOJI_FONT = "Segoe UI Emoji"
HANDLE_CURSORS = {
    "nw": Qt.CursorShape.SizeFDiagCursor,
    "se": Qt.CursorShape.SizeFDiagCursor,
    "ne": Qt.CursorShape.SizeBDiagCursor,
    "sw": Qt.CursorShape.SizeBDiagCursor,
    "n": Qt.CursorShape.SizeVerCursor,
    "s": Qt.CursorShape.SizeVerCursor,
    "e": Qt.CursorShape.SizeHorCursor,
    "w": Qt.CursorShape.SizeHorCursor,
    "p1": Qt.CursorShape.CrossCursor,
    "p2": Qt.CursorShape.CrossCursor,
}


# -- pictures from the clipboard, drops and files ----------------------------
def pil_from_qimage(qimage: QImage) -> Image.Image:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    qimage.save(buffer, "PNG")
    with Image.open(io.BytesIO(bytes(buffer.data()))) as img:
        return img.convert("RGBA")


def load_picture(path: str | Path) -> Image.Image:
    with Image.open(path) as img:
        return img.convert("RGBA")


def pictures_from_mime(mime) -> list[Image.Image]:
    """Image files first (keeps transparency and full size), else the bitmap."""
    pictures = []
    if mime is None:
        return pictures
    for url in mime.urls() if mime.hasUrls() else []:
        path = url.toLocalFile()
        if path and path.lower().endswith(IMAGE_SUFFIXES):
            try:
                pictures.append(load_picture(path))
            except OSError:
                log.warning("Could not read dropped picture %s", path)
    if not pictures and mime.hasImage():
        qimage = mime.imageData()
        if isinstance(qimage, QImage) and not qimage.isNull():
            pictures.append(pil_from_qimage(qimage))
    return pictures


def mime_has_picture(mime) -> bool:
    if mime is None:
        return False
    if mime.hasImage():
        return True
    return any(u.toLocalFile().lower().endswith(IMAGE_SUFFIXES) for u in mime.urls()) if mime.hasUrls() else False


class EditorCanvas(QWidget):
    changed = Signal()
    zoom_changed = Signal()
    selection_changed = Signal()
    context_requested = Signal(object)  # QPoint (global)
    pictures_dropped = Signal(object, object)  # list[PIL.Image], Point

    def __init__(self, image: CapturedImage, doc: EditorDocument, translator: Translator) -> None:
        super().__init__()
        self.image = image
        self.doc = doc
        self.t = translator.t
        self.tool = "arrow"
        self.color = COLORS["red"]
        self.width_px = 4
        self.font_size = 20
        self.fill = False
        self.snap_steps = True
        self.symbol = "👉"
        self.selected: int | None = None
        self._detector: EdgeDetector | None = None
        self._pixmap = QPixmap()
        self._press: Point | None = None
        self._draft: Shape | None = None
        self._move_origin: Shape | None = None
        self._resize: tuple[str, Shape] | None = None
        self._moved = False
        self._crop_draft: Rect | None = None
        self._crop_resize: tuple[str, Rect] | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._render_now)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(400, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._render_now()

    # -- rendering ------------------------------------------------------------
    def schedule_render(self) -> None:
        if not self._timer.isActive():
            self._timer.start(RENDER_THROTTLE_MS)

    def _render_now(self) -> None:
        doc = self.doc
        if self._draft is not None:
            # render the in-progress shape as if it were committed (exact preview)
            doc = self.doc.copy_with(list(self.doc.shapes) + [self._draft])
        out = render(self.image, doc, apply_crop=False)
        self._pixmap = QPixmap.fromImage(qimage_from_captured(out))
        self._pixmap_extent = doc.extent()
        self.update()

    def refresh(self) -> None:
        self._render_now()
        self.changed.emit()

    # -- mapping --------------------------------------------------------------
    # The view shows the whole canvas (the capture plus whatever was placed beside it), fitted to
    # the window; ``zoom`` magnifies that (Ctrl+wheel at the cursor) and ``_origin`` is where the
    # magnified canvas' top-left sits. While a shape is being dragged the mapping is frozen, so the
    # canvas growing under the cursor does not make the picture jump; it is refitted on release.
    _frozen: tuple[Rect, QRectF] | None = None
    _pixmap_extent: Rect | None = None
    zoom: float = 1.0
    _origin: QPointF | None = None
    _pan_from: QPointF | None = None

    @property
    def extent(self) -> Rect:
        return self._frozen[0] if self._frozen is not None else self.doc.extent()

    def device_ratio(self) -> float:
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        return dpr if dpr and dpr > 0 else 1.0

    def fit_scale(self) -> float:
        # Never enlarge: one image pixel is one physical screen pixel at most, so text stays sharp
        # at 125 % / 150 % Windows scaling (large captures are still fitted to the window).
        ext = self.doc.extent()
        return max(0.01, min((self.width() - 24) / ext.width, (self.height() - 24) / ext.height, 1.0 / self.device_ratio()))

    @staticmethod
    def _clamp_axis(start: float, size: float, avail: float, margin: float = 12.0) -> float:
        if size + 2 * margin <= avail:
            return (avail - size) / 2
        return min(margin, max(avail - size - margin, start))

    def target(self) -> QRectF:
        if self._frozen is not None:
            return self._frozen[1]
        ext = self.doc.extent()
        scale = self.fit_scale() * self.zoom
        tw, th = ext.width * scale, ext.height * scale
        if self.zoom <= 1.0 or self._origin is None:
            return QRectF((self.width() - tw) / 2, (self.height() - th) / 2, tw, th)
        x = self._clamp_axis(self._origin.x(), tw, self.width())
        y = self._clamp_axis(self._origin.y(), th, self.height())
        return QRectF(x, y, tw, th)

    # -- zoom and pan ---------------------------------------------------------
    def zoom_at(self, pos: QPointF, factor: float) -> None:
        """Magnify by ``factor`` keeping the point under ``pos`` where it is (Ctrl+wheel)."""
        old, old_scale = self.target(), self.scale
        ix, iy = (pos.x() - old.left()) / old_scale, (pos.y() - old.top()) / old_scale
        fit = self.fit_scale()
        self.zoom = min(max(1.0, self.zoom * factor), max(1.0, MAX_PIXEL_SCALE / fit))
        if self.zoom <= 1.0001:
            self.zoom, self._origin = 1.0, None
        else:
            scale = fit * self.zoom
            self._origin = QPointF(pos.x() - ix * scale, pos.y() - iy * scale)
            self._origin = self.target().topLeft()
        self.zoom_changed.emit()
        self.update()

    def zoom_to_pixels(self, pixel_scale: float | None = None) -> None:
        """Ctrl+1: show the capture at 100% (one physical screen pixel per image pixel)."""
        if pixel_scale is None:
            pixel_scale = 1.0 / self.device_ratio()
        self.zoom_at(QPointF(self.width() / 2, self.height() / 2), pixel_scale / self.scale)

    def fit_view(self) -> None:
        self.zoom, self._origin = 1.0, None
        self.zoom_changed.emit()
        self.update()

    def pan_by(self, dx: float, dy: float) -> None:
        if self.zoom <= 1.0:
            return
        self._origin = self.target().topLeft() + QPointF(dx, dy)
        self._origin = self.target().topLeft()
        self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            notches = delta.y() / 120 if delta.y() else delta.x() / 120
            if notches:
                self.zoom_at(event.position(), ZOOM_STEP**notches)
            event.accept()
            return
        if self.zoom > 1.0:
            dx, dy = delta.x(), delta.y()
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and dx == 0:
                dx, dy = dy, 0
            self.pan_by(dx * WHEEL_PAN, dy * WHEEL_PAN)
            event.accept()
            return
        super().wheelEvent(event)

    def freeze_view(self, on: bool) -> None:
        self._frozen = None
        if on:
            self._frozen = (self.doc.extent(), self.target())

    @property
    def scale(self) -> float:
        return self.target().width() / self.extent.width

    def to_image(self, pos: QPointF, clamp: bool = True) -> Point:
        t, ext = self.target(), self.extent
        x = round((pos.x() - t.left()) / self.scale) + ext.x
        y = round((pos.y() - t.top()) / self.scale) + ext.y
        if not clamp:
            return Point(x, y)
        return Point(min(max(x, ext.x), ext.right - 1), min(max(y, ext.y), ext.bottom - 1))

    def to_view(self, r: Rect) -> QRectF:
        t, s, ext = self.target(), self.scale, self.extent
        return QRectF(t.left() + (r.x - ext.x) * s, t.top() + (r.y - ext.y) * s, r.width * s, r.height * s)

    def view_point(self, p: Point) -> QPointF:
        t, s, ext = self.target(), self.scale, self.extent
        return QPointF(t.left() + (p.x - ext.x) * s, t.top() + (p.y - ext.y) * s)

    # -- tools ----------------------------------------------------------------
    def set_tool(self, tool: str) -> None:
        self.tool = tool
        if tool != "select":
            self.select(None)
        self.setCursor(Qt.CursorShape.ArrowCursor if tool == "select" else Qt.CursorShape.CrossCursor)

    def select(self, index: int | None) -> None:
        self.selected = index
        self.selection_changed.emit()
        self.update()

    @property
    def selected_shape(self) -> Shape | None:
        if self.selected is None or self.selected >= len(self.doc.shapes):
            return None
        return self.doc.shapes[self.selected]

    def restyle_selected(self, **changes) -> None:
        shape = self.selected_shape
        if shape is None:
            return
        allowed = {k: v for k, v in changes.items() if hasattr(shape, k)}
        if shape.kind == "emoji" and "font_size" in allowed:
            allowed["font_size"] *= 2  # symbols are drawn at twice the text size
        if all(getattr(shape, k) == v for k, v in allowed.items()):
            return
        self.doc.replace(self.selected, replace(shape, **allowed))
        self.refresh()

    def delete_selected(self) -> None:
        if self.selected is not None:
            self.doc.remove(self.selected)
            self.select(None)
            self.refresh()

    def duplicate_selected(self) -> None:
        shape = self.selected_shape
        if shape is None:
            return
        offset = max(12, round(16 / max(self.scale, 0.01) * 0.5))
        self.select(self.doc.add(shape.moved(offset, offset)))
        self.refresh()

    def raise_selected(self, front: bool = True) -> None:
        if self.selected is None:
            return
        index = self.doc.bring_to_front(self.selected) if front else self.doc.send_to_back(self.selected)
        self.select(index)
        self.refresh()

    def place_selected(self, side: str, gap: int = 16) -> None:
        """Put the selected shape beside the capture ("below" or "right"); the canvas grows to fit."""
        shape = self.selected_shape
        if shape is None:
            return
        b = shape_bounds(shape)
        if side == "below":
            dx, dy = -b.x, self.image.height + gap - b.y
        else:
            dx, dy = self.image.width + gap - b.x, -b.y
        self.doc.replace(self.selected, shape.moved(dx, dy))
        self.refresh()

    def nudge_selected(self, dx: int, dy: int) -> None:
        shape = self.selected_shape
        if shape is not None:
            self.doc.replace(self.selected, shape.moved(dx, dy))
            self.refresh()

    def _new_shape(self, kind: str, p1: Point, p2: Point, **extra) -> Shape:
        values = dict(
            color=self.color,
            width=self.width_px,
            font_size=self.font_size,
            fill=self.fill,
            strength=max(6, self.width_px * 3),
        )
        values.update(extra)
        return Shape(kind, p1, p2, **values)

    @property
    def detector(self) -> EdgeDetector:
        if self._detector is None:
            self._detector = EdgeDetector(self.image)
        return self._detector

    # -- handles --------------------------------------------------------------
    def handles(self, index: int | None = None) -> dict[str, Point]:
        index = self.selected if index is None else index
        if index is None or index >= len(self.doc.shapes):
            return {}
        s = self.doc.shapes[index]
        if s.kind in ("line", "arrow"):
            return {"p1": s.p1, "p2": s.p2}
        if s.kind in SCALED_KINDS:  # text, symbols, step badges: the corners scale the size
            corners = box_handles(shape_bounds(s))
            return {k: v for k, v in corners.items() if len(k) == 2}
        if s.kind not in BOX_KINDS:
            return {}
        return box_handles(s.bounds)

    def crop_handles(self) -> dict[str, Point]:
        """With the Crop tool, the kept area can be adjusted by its 8 handles."""
        crop = self._crop_draft if self._crop_resize is not None else self.doc.crop
        if self.tool != "crop" or crop is None or self.selected is not None:
            return {}
        return box_handles(crop)

    def _handle_near(self, handles: dict[str, Point], pos: QPointF) -> str | None:
        reach = HANDLE_PX + 3
        for name, point in handles.items():
            v = self.view_point(point)
            if abs(v.x() - pos.x()) <= reach and abs(v.y() - pos.y()) <= reach:
                return name
        return None

    def handle_at(self, pos: QPointF) -> str | None:
        return self._handle_near(self.handles(), pos)

    def crop_handle_at(self, pos: QPointF) -> str | None:
        return self._handle_near(self.crop_handles(), pos)

    @staticmethod
    def resized(origin: Shape, handle: str, p: Point, keep_ratio: bool) -> Shape:
        if handle == "p1":
            return replace(origin, p1=p)
        if handle == "p2":
            return replace(origin, p2=p)
        if origin.kind in SCALED_KINDS:
            return scaled(origin, handle, p)
        l, t, r, bm = resize_box(origin.bounds, handle, p, keep_ratio, MIN_SIZE)
        return replace(origin, p1=Point(l, t), p2=Point(r, bm))

    # -- mouse ----------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:  # drag with the wheel button: pan
            self._pan_from = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            if event.button() == Qt.MouseButton.RightButton:
                hit = self.doc.hit(self.to_image(event.position()))
                if hit is not None:
                    self.select(hit)
                    self.context_requested.emit(event.globalPosition().toPoint())
            return
        self.setFocus()
        p = self.to_image(event.position())
        self._press = p
        self._moved = False
        # The selected shape's handles work with every tool, so a shape can be adjusted right after
        # it was drawn without switching to Select first.
        crop_handle = self.crop_handle_at(event.position())
        if crop_handle is not None:
            self._crop_resize = (crop_handle, self.doc.crop)
            self._crop_draft = self.doc.crop
            return
        handle = self.handle_at(event.position()) if self.selected_shape is not None else None
        if handle is not None:
            self._press = self.to_image(event.position(), clamp=False)  # resizing may go past the edges
            self._resize = (handle, self.selected_shape)
            self.freeze_view(True)
            return
        if self.tool == "select":
            # moving may go past the edges: the canvas grows to fit
            self._press = self.to_image(event.position(), clamp=False)
            hit = self.doc.hit(p)
            self.select(hit)
            if hit is not None:
                self._move_origin = self.doc.shapes[hit]
                self.freeze_view(True)
            return
        if self.tool in ("text", "emoji", "step"):
            # a click on a shape of the same kind selects it (handles, Delete, style) instead of adding one
            hit = self.doc.hit(p)
            if hit is not None and self.doc.shapes[hit].kind == self.tool:
                self.select(hit)
                self._press = None
                return
        if self.tool == "text":
            text, ok = QInputDialog.getMultiLineText(self, self.t("editor.tool.text"), self.t("editor.text.prompt"))
            if ok and text.strip():
                self.select(self.doc.add(self._new_shape("text", p, p, text=text.rstrip())))
                self.refresh()
            self._press = None
            return
        if self.tool == "emoji":
            self.select(self.doc.add(self._new_shape("emoji", p, p, text=self.symbol, font_size=self.font_size * 2)))
            self.refresh()
            self._press = None
            return
        if self.tool == "step":
            frame = None
            on_capture = 0 <= p.x < self.image.width and 0 <= p.y < self.image.height
            if self.snap_steps and on_capture:
                boxes = [b for b in self.detector.boxes_at(p) if b.width < self.image.width * 0.95]
                frame = boxes[0] if boxes else None
            if frame is not None:
                self.doc.add(self._new_shape("rect", Point(frame.x - 2, frame.y - 2), Point(frame.right + 1, frame.bottom + 1), width=3))
                self.select(self.doc.add(self._new_shape("step", Point(frame.x, frame.y), Point(frame.x, frame.y))))
            else:
                self.select(self.doc.add(self._new_shape("step", p, p)))
            self.refresh()
            self._press = None
            return
        if self.tool == "crop":
            self._crop_draft = Rect(p.x, p.y, 1, 1)
            return
        self._draft = self._new_shape(self.tool, p, p)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._pan_from is not None:
            delta = event.position() - self._pan_from
            self._pan_from = event.position()
            self.pan_by(delta.x(), delta.y())
            return
        p = self.to_image(event.position())
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if self._press is None:
            handle = self.handle_at(event.position()) if self.selected_shape is not None else None
            handle = handle or self.crop_handle_at(event.position())
            if handle is not None:
                self.setCursor(HANDLE_CURSORS[handle])
            elif self.tool == "select":
                self.setCursor(Qt.CursorShape.SizeAllCursor if self.doc.hit(p) is not None else Qt.CursorShape.ArrowCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
            return
        if self.tool == "select" or self._resize is not None:
            p = self.to_image(event.position(), clamp=False)
        if self._resize is not None and self.selected is not None:
            handle, origin = self._resize
            keep_ratio = (origin.kind == "image") != shift  # pictures keep their proportions unless Shift
            self.doc.replace(self.selected, self.resized(origin, handle, p, keep_ratio), record=not self._moved)
            self._moved = True
            self.schedule_render()
            return
        if self.tool == "select" and self.selected is not None and self._move_origin is not None:
            dx, dy = p.x - self._press.x, p.y - self._press.y
            self.doc.replace(self.selected, self._move_origin.moved(dx, dy), record=not self._moved)
            self._moved = True
            self.schedule_render()
            return
        if self._crop_resize is not None:
            handle, origin = self._crop_resize
            l, t, r, bm = resize_box(origin, handle, p, shift, MIN_SIZE)
            self._crop_draft = Rect(l, t, r - l + 1, bm - t + 1)
            self.update()
            return
        if self.tool == "crop" and self._crop_draft is not None:
            self._crop_draft = Rect.from_points(self._press, p)
            self.update()
            return
        if self._draft is not None:
            end = _constrain(self._press, p, self._draft.kind) if shift else p
            self._draft = replace(self._draft, p2=end)
            self.schedule_render()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_from is not None:
            self._pan_from = None
            self.setCursor(Qt.CursorShape.ArrowCursor if self.tool == "select" else Qt.CursorShape.CrossCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        if self.tool == "crop" and self._crop_draft is not None:
            if self._crop_draft.width > 4 and self._crop_draft.height > 4:
                self.doc.set_crop(self._crop_draft)
            self._crop_draft = None
            self.refresh()
        elif self._draft is not None:
            draft, self._draft = self._draft, None
            if max(abs(draft.p2.x - draft.p1.x), abs(draft.p2.y - draft.p1.y)) >= 3:
                self.select(self.doc.add(draft))  # stays selected: its handles adjust it right away
            else:
                self.select(self.doc.hit(draft.p1))  # a plain click selects the shape under it
            self.refresh()
        elif self._moved:
            self.refresh()
        if self._frozen is not None:
            self.freeze_view(False)  # refit the (possibly larger) canvas
            self.update()
        self._press = None
        self._move_origin = None
        self._resize = None
        self._crop_resize = None
        self._moved = False

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        index = self.doc.hit(self.to_image(event.position()))
        if index is not None and self.doc.shapes[index].kind == "text":
            shape = self.doc.shapes[index]
            text, ok = QInputDialog.getMultiLineText(self, self.t("editor.tool.text"), self.t("editor.text.prompt"), shape.text)
            if ok:
                self.doc.replace(index, replace(shape, text=text.rstrip()))
                self.refresh()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        steps = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }
        if event.key() in steps and self.selected is not None:
            dx, dy = steps[event.key()]
            factor = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            self.nudge_selected(dx * factor, dy * factor)
            return
        super().keyPressEvent(event)

    # -- drag and drop ---------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if mime_has_picture(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if mime_has_picture(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        pictures = pictures_from_mime(event.mimeData())
        if pictures:
            event.acceptProposedAction()
            self.pictures_dropped.emit(pictures, self.to_image(event.position()))

    # -- painting -------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        t = theme()
        p = QPainter(self)
        # smooth up to 400 %; beyond that show the real pixels for pixel-precise placement
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.scale <= 4.0)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(t.surface_alt))
        target = self.target()
        sheet = self.to_view(self._pixmap_extent) if self._pixmap_extent is not None else target
        # soft frame so the capture reads as a sheet on the workspace
        p.setPen(Qt.PenStyle.NoPen)
        for i, alpha in enumerate((26, 16, 8)):
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(sheet.adjusted(-1 - i, -1 - i + 1, 1 + i, 1 + i + 1), 2 + i, 2 + i)
        visible = sheet.intersected(QRectF(self.rect()))
        if not visible.isEmpty() and sheet.width() > 0 and sheet.height() > 0:
            # only scale the part that is on screen (matters when zoomed in on a large capture)
            kx, ky = self._pixmap.width() / sheet.width(), self._pixmap.height() / sheet.height()
            source = QRectF(
                (visible.left() - sheet.left()) * kx, (visible.top() - sheet.top()) * ky, visible.width() * kx, visible.height() * ky
            )
            p.drawPixmap(visible, self._pixmap, source)
        crop = self._crop_draft or self.doc.crop
        if crop is not None:
            hole = self.to_view(crop)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            for r in (
                QRectF(target.left(), target.top(), target.width(), hole.top() - target.top()),
                QRectF(target.left(), hole.bottom(), target.width(), target.bottom() - hole.bottom()),
                QRectF(target.left(), hole.top(), hole.left() - target.left(), hole.height()),
                QRectF(hole.right(), hole.top(), target.right() - hole.right(), hole.height()),
            ):
                if r.width() > 0 and r.height() > 0:
                    p.drawRect(r)
            p.setPen(QPen(QColor(t.accent), 2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(hole)
            p.setPen(QPen(QColor(t.accent), 1.6))
            p.setBrush(QColor("#ffffff"))
            for point in self.crop_handles().values():
                p.drawEllipse(self.view_point(point), HANDLE_PX, HANDLE_PX)
        shape = self.selected_shape
        if shape is not None:
            accent = QColor(t.accent)
            handles = self.handles()
            if shape.kind not in ("line", "arrow"):
                frame = self.to_view(shape_bounds(shape))
                if not handles:
                    frame = frame.adjusted(-4, -4, 4, 4)
                pen = QPen(accent, 1.2, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(frame)
            p.setPen(QPen(accent, 1.6))
            p.setBrush(QColor("#ffffff"))
            for point in handles.values():
                v = self.view_point(point)
                p.drawEllipse(v, HANDLE_PX, HANDLE_PX)
        p.end()


def _constrain(a: Point, b: Point, kind: str) -> Point:
    dx, dy = b.x - a.x, b.y - a.y
    if kind in ("line", "arrow"):  # 45° steps
        if abs(dx) > 2 * abs(dy):
            return Point(b.x, a.y)
        if abs(dy) > 2 * abs(dx):
            return Point(a.x, b.y)
        d = max(abs(dx), abs(dy))
        return Point(a.x + d * (1 if dx >= 0 else -1), a.y + d * (1 if dy >= 0 else -1))
    d = max(abs(dx), abs(dy))  # squares and circles
    return Point(a.x + d * (1 if dx >= 0 else -1), a.y + d * (1 if dy >= 0 else -1))


# -- popups ----------------------------------------------------------------------
def confirm_discard(parent, t) -> bool:
    from PySide6.QtWidgets import QMessageBox

    answer = QMessageBox.question(
        parent, t("editor.discard.title"), t("editor.discard.question"),
        QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
    )
    return answer == QMessageBox.StandardButton.Discard


class _Popup(QFrame):
    """Rounded flyout that closes when you click elsewhere."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("EditorPopup")
        t = theme()
        self.setStyleSheet(
            f"QFrame#EditorPopup {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: 10px; }}"
            f" QToolButton {{ border: 1px solid transparent; border-radius: 6px; background: transparent; }}"
            f" QToolButton:hover {{ background: {t.surface_strong}; border-color: {t.border_strong}; }}"
        )

    def show_below(self, anchor: QWidget) -> None:
        self.adjustSize()
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(pos.x(), area.left()), area.right() - self.width())
        y = pos.y() if pos.y() + self.height() <= area.bottom() else anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4
        self.move(x, y)
        self.show()


class ColorPopup(_Popup):
    picked = Signal(str)
    custom_requested = Signal()

    def __init__(self, parent: QWidget, translator: Translator, current: str, recent: list[str]) -> None:
        super().__init__(parent)
        t = theme()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        grid = QGridLayout()
        grid.setSpacing(4)
        self.swatches: dict[str, QToolButton] = {}
        for i, color in enumerate(PALETTE):
            grid.addWidget(self._swatch(color, current, t), i // 8, i % 8)
        layout.addLayout(grid)
        if recent:
            caption = label(translator.t("editor.colors.recent"), "caption")
            layout.addWidget(caption)
            row = QHBoxLayout()
            row.setSpacing(4)
            for color in recent[:RECENT_COLORS]:
                row.addWidget(self._swatch(color, current, t))
            row.addStretch(1)
            layout.addLayout(row)
        more = QPushButton(translator.t("editor.colors.custom"))
        more.setIcon(icons.icon("palette", t.text, 16))
        more.clicked.connect(lambda: (self.close(), self.custom_requested.emit()))
        layout.addWidget(more)

    def _swatch(self, color: str, current: str, t) -> QToolButton:
        button = QToolButton()
        button.setFixedSize(26, 26)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(color.upper())
        ring = t.accent if color.lower() == current.lower() else t.border_strong
        width = 3 if color.lower() == current.lower() else 1
        button.setStyleSheet(
            f"QToolButton {{ background: {color}; border: {width}px solid {ring}; border-radius: 13px; }}"
            f" QToolButton:hover {{ border: 2px solid {t.text}; }}"
        )
        button.clicked.connect(lambda _=False, c=color: (self.close(), self.picked.emit(c)))
        self.swatches.setdefault(color, button)
        return button


class SymbolPopup(_Popup):
    picked = Signal(str)

    def __init__(self, parent: QWidget, translator: Translator) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(4)
        emoji_font = QFont(EMOJI_FONT)
        emoji_font.setPointSizeF(15)
        self.buttons: dict[str, QToolButton] = {}
        for group, symbols in SYMBOLS:
            layout.addWidget(label(translator.t(f"editor.symbols.{group}"), "caption"))
            row = QHBoxLayout()
            row.setSpacing(2)
            for symbol in symbols:
                button = QToolButton()
                button.setText(symbol)
                button.setFont(emoji_font)
                button.setFixedSize(36, 36)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda _=False, s=symbol: (self.close(), self.picked.emit(s)))
                row.addWidget(button)
                self.buttons[symbol] = button
            layout.addLayout(row)


class EditorWindow(QWidget):
    def __init__(
        self,
        result: CaptureResult,
        translator: Translator,
        *,
        on_done: Callable[[CaptureResult], None],
        copy: Callable[[CapturedImage], None],
        save_dir: Path,
        settings=None,
        ocr_engine: Callable[[], object] | None = None,
        recent_images: Callable[[], list[tuple[str, str, str]]] | None = None,
        on_copy_close: Callable[[CaptureResult], None] | None = None,
    ) -> None:
        super().__init__(None)
        self.result = result
        self.translator = translator
        self.t = translator.t
        self.on_done = on_done
        self._copy = copy
        self.save_dir = save_dir
        self.settings = settings
        self._ocr_engine = ocr_engine
        self._recent_images = recent_images
        # Called with the edited result by "Copy & close" when the edit must be
        # kept somewhere (a procedure step); a plain capture is only copied.
        self.on_copy_close = on_copy_close
        self.doc = EditorDocument(result.image.width, result.image.height)
        self.rt = rt = Retranslator(translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setProperty("page", True)
        self.setWindowIcon(app_icon())
        rt.bind(self.setWindowTitle, "editor.title")
        self.resize(min(1400, max(980, result.image.width + 200)), min(940, max(640, result.image.height + 190)))

        self.canvas = EditorCanvas(result.image, self.doc, translator)
        if settings is not None:
            self.canvas.color = settings.get("editor.color") or COLORS["red"]
            self.canvas.width_px = int(settings.get("editor.width") or 4)

        # Left: tools
        tools_col = QVBoxLayout()
        tools_col.setSpacing(2)
        self.tool_group = QButtonGroup(self)
        self.tool_buttons: dict[str, QToolButton] = {}
        for tool, icon_name, key in TOOLS:
            button = self._tool_button(icon_name)
            button.setCheckable(True)
            rt.bind(button.setToolTip, f"editor.tool.{tool}", params_fn=lambda k=key: {"shortcut": k})
            if tool == "emoji":
                button.clicked.connect(self.pick_symbol)
            else:
                button.clicked.connect(lambda _=False, tl=tool: self.set_tool(tl))
            self.tool_group.addButton(button)
            tools_col.addWidget(button)
            self.tool_buttons[tool] = button
            QShortcut(QKeySequence(key), self, activated=lambda tl=tool: self.set_tool(tl))
        separator = QFrame()
        separator.setProperty("divider", True)
        separator.setFixedHeight(1)
        tools_col.addSpacing(4)
        tools_col.addWidget(separator)
        tools_col.addSpacing(4)
        self.image_button = self._tool_button("image_add")
        rt.bind(self.image_button.setToolTip, "editor.insert_image")
        self.image_button.clicked.connect(self.show_insert_menu)
        tools_col.addWidget(self.image_button)
        tools_col.addStretch(1)

        # Top: properties
        props = QHBoxLayout()
        props.setSpacing(6)
        self.color_group = QButtonGroup(self)
        self.color_buttons: dict[str, QToolButton] = {}
        for name, hex_color in COLORS.items():
            swatch = QToolButton()
            swatch.setCheckable(True)
            swatch.setFixedSize(24, 24)
            swatch.setCursor(Qt.CursorShape.PointingHandCursor)
            swatch.setProperty("hex", hex_color)
            swatch.setToolTip(hex_color.upper())
            swatch.clicked.connect(lambda _=False, c=hex_color: self.set_color(c))
            self.color_group.addButton(swatch)
            props.addWidget(swatch)
            self.color_buttons[hex_color] = swatch
        self.more_colors = QToolButton()
        self.more_colors.setObjectName("ColorMore")
        self.more_colors.setFixedSize(30, 30)
        self.more_colors.setIconSize(QSize(16, 16))
        self.more_colors.setCursor(Qt.CursorShape.PointingHandCursor)
        rt.bind(self.more_colors.setToolTip, "editor.colors.more")
        self.more_colors.clicked.connect(self.show_color_popup)
        props.addWidget(self.more_colors)
        props.addSpacing(10)
        width_label = label(role="secondary")
        rt.bind(width_label.setText, "editor.width")
        props.addWidget(width_label)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 30)
        self.width_spin.setValue(self.canvas.width_px)
        self.width_spin.valueChanged.connect(self.set_width)
        props.addWidget(self.width_spin)
        font_label = label(role="secondary")
        rt.bind(font_label.setText, "editor.font_size")
        props.addWidget(font_label)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 120)
        self.font_spin.setValue(self.canvas.font_size)
        self.font_spin.valueChanged.connect(self.set_font_size)
        props.addWidget(self.font_spin)
        self.fill_chip = Chip("rect")
        self.fill_chip.setCheckable(True)
        rt.bind(self.fill_chip.setText, "editor.fill")
        rt.bind(self.fill_chip.setToolTip, "editor.fill.tip")
        self.fill_chip.toggled.connect(self.set_fill)
        props.addWidget(self.fill_chip)
        self.snap_chip = Chip("region")
        self.snap_chip.setCheckable(True)
        self.snap_chip.setChecked(True)
        rt.bind(self.snap_chip.setText, "steps.snap")
        rt.bind(self.snap_chip.setToolTip, "steps.snap.tip")
        self.snap_chip.toggled.connect(lambda on: setattr(self.canvas, "snap_steps", on))
        props.addWidget(self.snap_chip)
        self.autoblur_chip = Chip("blur")
        rt.bind(self.autoblur_chip.setText, "editor.autoblur")
        rt.bind(self.autoblur_chip.setToolTip, "editor.autoblur.tip")
        self.autoblur_chip.clicked.connect(self.auto_blur)
        self.autoblur_chip.setEnabled(ocr_engine is not None)
        props.addWidget(self.autoblur_chip)
        props.addStretch(1)
        self.front_button = self._icon_button("front", "editor.front", lambda: self.canvas.raise_selected(True))
        self.back_button = self._icon_button("back", "editor.back", lambda: self.canvas.raise_selected(False))
        self.duplicate_button = self._icon_button("duplicate", "editor.duplicate", self.canvas.duplicate_selected)
        self.undo_button = self._icon_button("undo", "editor.undo", self.undo)
        self.redo_button = self._icon_button("redo", "editor.redo", self.redo)
        self.delete_button = self._icon_button("trash", "editor.delete", self.canvas.delete_selected)
        for b in (self.front_button, self.back_button, self.duplicate_button):
            props.addWidget(b)
        props.addSpacing(6)
        for b in (self.undo_button, self.redo_button, self.delete_button):
            props.addWidget(b)

        # Bottom: actions
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.hint = label(role="caption")
        bottom.addWidget(self.hint, 1)
        self.zoom_button = QPushButton()
        self.zoom_button.setProperty("flat", True)
        self.zoom_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zoom_button.setMinimumWidth(64)
        rt.bind(self.zoom_button.setToolTip, "editor.zoom.tip")
        self.zoom_button.clicked.connect(self.canvas.fit_view)
        bottom.addWidget(self.zoom_button)
        self.action_buttons: dict[str, QPushButton] = {}
        self.buttons: dict[str, QPushButton] = {}
        for key, slot, accent in (
            ("editor.copy", self.copy, False),
            ("editor.copy_close", self.copy_and_close, False),
            ("editor.save_as", self.save_as, False),
            ("editor.done", self.done, True),
        ):
            button = QPushButton()
            button.setProperty("accent", accent)
            rt.bind(button.setText, key)
            if self.translator.has(f"{key}.tip"):
                rt.bind(button.setToolTip, f"{key}.tip")
            button.clicked.connect(slot)
            bottom.addWidget(button)
            self.action_buttons[key] = button
            self.buttons[key.split(".", 1)[1]] = button

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addLayout(tools_col)
        body.addWidget(self.canvas, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addLayout(props)
        layout.addLayout(body, 1)
        layout.addLayout(bottom)

        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence.StandardKey.Delete, self, activated=self.canvas.delete_selected)
        QShortcut(QKeySequence.StandardKey.Copy, self, activated=self.copy)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self.copy_and_close)
        QShortcut(QKeySequence("Ctrl+Shift+Return"), self, activated=self.copy_and_close)
        QShortcut(QKeySequence.StandardKey.Paste, self, activated=self.paste)
        QShortcut(QKeySequence("Ctrl+D"), self, activated=self.canvas.duplicate_selected)
        QShortcut(QKeySequence("Ctrl+]"), self, activated=lambda: self.canvas.raise_selected(True))
        QShortcut(QKeySequence("Ctrl+["), self, activated=lambda: self.canvas.raise_selected(False))
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save_as)
        centre = lambda: QPointF(self.canvas.width() / 2, self.canvas.height() / 2)  # noqa: E731
        for keys in (QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")):
            QShortcut(keys, self, activated=lambda: self.canvas.zoom_at(centre(), ZOOM_STEP))
        QShortcut(QKeySequence.StandardKey.ZoomOut, self, activated=lambda: self.canvas.zoom_at(centre(), 1 / ZOOM_STEP))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self.canvas.fit_view)
        QShortcut(QKeySequence("Ctrl+1"), self, activated=self.canvas.zoom_to_pixels)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self, activated=self.done)
        on_escape(self, self.escape)
        self.canvas.selection_changed.connect(self._on_selection)
        self.canvas.changed.connect(self._update_hint)
        self.canvas.context_requested.connect(self._context_menu)
        self.canvas.zoom_changed.connect(self._update_zoom)
        self.canvas.changed.connect(self._update_zoom)  # the canvas may have grown
        self.canvas.pictures_dropped.connect(lambda pictures, at: self.insert_pictures(pictures, at))
        on_theme_changed(self, self._restyle)
        rt.on_change(self._update_hint)
        self.set_tool("arrow")
        self._on_selection()

    def _tool_button(self, icon_name: str) -> QToolButton:
        button = QToolButton()
        button.setProperty("icon_name", icon_name)
        button.setIconSize(QSize(22, 22))
        button.setFixedSize(42, 42)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _icon_button(self, icon_name: str, tip_key: str, slot) -> QToolButton:
        from screenpilot.ui.widgets import IconButton

        button = IconButton(icon_name, size=18)
        self.rt.bind(button.setToolTip, tip_key)
        button.clicked.connect(slot)
        return button

    def _restyle(self, t) -> None:
        for button in (*self.tool_buttons.values(), self.image_button):
            button.setIcon(icons.icon(button.property("icon_name"), t.text, 22))
        for hex_color, swatch in self.color_buttons.items():
            swatch.setStyleSheet(
                f"QToolButton {{ background: {hex_color}; border: 1px solid {t.border_strong}; border-radius: 12px; }}"
                f" QToolButton:hover {{ border: 2px solid {t.text}; }}"
                f" QToolButton:checked {{ border: 3px solid {t.accent}; }}"
            )
        self._update_color_button()

    def _update_color_button(self) -> None:
        t = theme()
        self.more_colors.setIcon(icons.icon("palette", t.text, 16))
        self.more_colors.setStyleSheet(
            f"QToolButton#ColorMore {{ background: {t.surface}; border: 3px solid {self.canvas.color}; border-radius: 15px; padding: 0; }}"
            f" QToolButton#ColorMore:hover {{ background: {t.surface_strong}; }}"
        )

    # -- state ----------------------------------------------------------------
    def set_tool(self, tool: str) -> None:
        self.canvas.set_tool(tool)
        self.tool_buttons[tool].setChecked(True)
        self._update_hint()

    def set_symbol(self, symbol: str) -> None:
        self.canvas.symbol = symbol
        self.set_tool("emoji")

    def pick_symbol(self) -> SymbolPopup:
        popup = SymbolPopup(self, self.translator)
        popup.picked.connect(self.set_symbol)
        popup.show_below(self.tool_buttons["emoji"])
        self.tool_buttons[self.canvas.tool].setChecked(True)  # until a symbol is picked
        return popup

    def _show_color(self, color: str) -> None:
        self.canvas.color = color
        self.color_group.setExclusive(False)
        for hex_color, swatch in self.color_buttons.items():
            swatch.setChecked(hex_color.lower() == color.lower())
        self.color_group.setExclusive(True)
        self._update_color_button()

    def set_color(self, color: str) -> None:
        self._show_color(color)
        self.canvas.restyle_selected(color=color)
        if self.settings is not None:
            self.settings.set("editor.color", color)

    def recent_colors(self) -> list[str]:
        if self.settings is None:
            return []
        stored = self.settings.get("editor.recent_colors") or []
        return [c for c in stored if isinstance(c, str)]

    def remember_color(self, color: str) -> None:
        if self.settings is None:
            return
        colors = [color] + [c for c in self.recent_colors() if c.lower() != color.lower()]
        self.settings.set("editor.recent_colors", colors[:RECENT_COLORS])

    def show_color_popup(self) -> ColorPopup:
        popup = ColorPopup(self, self.translator, self.canvas.color, self.recent_colors())
        popup.picked.connect(self.set_color)
        popup.custom_requested.connect(self.pick_custom_color)
        popup.show_below(self.more_colors)
        return popup

    def pick_custom_color(self) -> None:
        color = QColorDialog.getColor(QColor(self.canvas.color), self, self.t("editor.colors.custom"))
        if color.isValid():
            hex_color = color.name(QColor.NameFormat.HexRgb)
            self.remember_color(hex_color)
            self.set_color(hex_color)

    def set_width(self, value: int) -> None:
        self.canvas.width_px = value
        self.canvas.restyle_selected(width=value, strength=max(6, value * 3))
        if self.settings is not None:
            self.settings.set("editor.width", value)

    def set_font_size(self, value: int) -> None:
        self.canvas.font_size = value
        self.canvas.restyle_selected(font_size=value)

    def set_fill(self, on: bool) -> None:
        self.canvas.fill = on
        self.canvas.restyle_selected(fill=on)

    def _on_selection(self) -> None:
        shape = self.canvas.selected_shape
        for button in (self.delete_button, self.front_button, self.back_button, self.duplicate_button):
            button.setEnabled(shape is not None)
        if shape is None:
            self._update_hint()
            return
        font_size = shape.font_size // 2 if shape.kind == "emoji" else shape.font_size
        for widget, value in ((self.width_spin, shape.width), (self.font_spin, font_size)):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self.fill_chip.blockSignals(True)
        self.fill_chip.setChecked(shape.fill)
        self.fill_chip.blockSignals(False)
        if shape.kind not in ("blur", "pixelate", "spotlight"):
            self._show_color(shape.color)  # the colour button shows the selected shape's colour
        self._update_hint()

    def _update_hint(self) -> None:
        tool = self.canvas.tool
        shape = self.canvas.selected_shape
        if tool == "select" and shape is not None and shape.kind == "image":
            text = self.t("editor.hint.image")
        elif tool == "emoji":
            text = self.t("editor.hint.emoji", symbol=self.canvas.symbol)
        elif self.rt.translator.has(f"editor.hint.{tool}"):
            text = self.t(f"editor.hint.{tool}")
        else:
            text = self.t("editor.hint.draw")
        self.hint.setText(text)
        self.undo_button.setEnabled(self.doc.can_undo)

    def _update_zoom(self) -> None:
        self.zoom_button.setText(f"{round(self.canvas.scale * 100)} %")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_zoom()  # the fitted size depends on the window

    def _context_menu(self, global_pos: QPoint) -> None:
        shape = self.canvas.selected_shape
        if shape is None:
            return
        menu = QMenu(self)
        t = theme()
        if shape.kind in ("image", "text", "emoji"):
            menu.addAction(icons.icon("down", t.text, 16), self.t("editor.place.below"), lambda: self.canvas.place_selected("below"))
            menu.addAction(icons.icon("arrow", t.text, 16), self.t("editor.place.right"), lambda: self.canvas.place_selected("right"))
            menu.addSeparator()
        for icon_name, key, slot in (
            ("front", "editor.front", lambda: self.canvas.raise_selected(True)),
            ("back", "editor.back", lambda: self.canvas.raise_selected(False)),
            ("duplicate", "editor.duplicate", self.canvas.duplicate_selected),
            ("trash", "editor.delete", self.canvas.delete_selected),
        ):
            menu.addAction(icons.icon(icon_name, t.text, 16), self.t(key), slot)
        menu.exec(global_pos)

    # -- pictures -------------------------------------------------------------
    def _drop_point(self) -> Point:
        area = self.doc.crop or Rect(0, 0, self.doc.width, self.doc.height)
        return Point(area.x + area.width // 2, area.y + area.height // 2)

    def insert_pictures(self, pictures: list[Image.Image], at: Point | None = None) -> list[int]:
        """Place pictures on top of the capture; the last one is selected for moving and resizing."""
        at = at or self._drop_point()
        indexes = []
        for i, picture in enumerate(pictures):
            offset = 24 * i
            indexes.append(
                self.doc.add_image(picture, Point(at.x + offset, at.y + offset), color=self.canvas.color, width=max(2, self.canvas.width_px // 2))
            )
        if indexes:
            self.set_tool("select")
            self.canvas.select(indexes[-1])
            self.canvas.refresh()
            self.canvas.setFocus()
        return indexes

    def attach_below(self, images: list[CapturedImage], gap: int = 16) -> list[int]:
        """Pictures at their real size, one below the other under the capture (multi-capture).

        The capture stays the fixed background; each picture can be moved, resized or deleted."""
        from screenpilot.capture.image import stack_offsets

        top = self.canvas.image.height + gap
        indexes = []
        style = dict(color=self.canvas.color, width=max(2, self.canvas.width_px // 2))
        for image, offset in zip(images, stack_offsets([i.height for i in images], gap)):
            w, h = image.width, image.height
            at = Point(w // 2, top + offset + h // 2)
            indexes.append(self.doc.add_image(image, at, max_size=(w, h), **style))
        if indexes:
            self.set_tool("select")
            self.canvas.select(indexes[-1])
            self.canvas.refresh()
            self.canvas.fit_view()
            self.canvas.setFocus()
        return indexes

    def insert_picture_file(self, path: str | Path) -> int | None:
        try:
            picture = load_picture(path)
        except OSError as exc:
            self.hint.setText(self.t("editor.insert.failed", reason=str(exc)))
            return None
        return self.insert_pictures([picture])[0]

    def paste(self) -> list[int]:
        pictures = pictures_from_mime(QGuiApplication.clipboard().mimeData())
        if not pictures:
            self.hint.setText(self.t("editor.insert.no_image"))
            return []
        return self.insert_pictures(pictures)

    def choose_picture_file(self) -> int | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.t("editor.insert.file"),
            str(self.save_dir),
            f"{self.t('editor.insert.filter')} (" + " ".join(f"*{s}" for s in IMAGE_SUFFIXES) + ")",
        )
        return self.insert_picture_file(path) if path else None

    def build_insert_menu(self) -> QMenu:
        t = theme()
        menu = QMenu(self)
        menu.addAction(icons.icon("folder", t.text, 16), self.t("editor.insert.file"), self.choose_picture_file)
        paste = menu.addAction(icons.icon("copy", t.text, 16), self.t("editor.insert.clipboard"), self.paste)
        paste.setEnabled(mime_has_picture(QGuiApplication.clipboard().mimeData()))
        recent_menu = menu.addMenu(icons.icon("history", t.text, 16), self.t("editor.insert.recent"))
        entries = self._recent_images() if self._recent_images is not None else []
        source = str(self.result.metadata.get("source", ""))
        entries = [e for e in entries if e[1] != source]
        if not entries:
            empty = recent_menu.addAction(self.t("editor.insert.none"))
            empty.setEnabled(False)
        for text, path, thumbnail in entries:
            action = recent_menu.addAction(text, lambda p=path: self.insert_picture_file(p))
            if thumbnail and Path(thumbnail).exists():
                action.setIcon(QIcon(QPixmap(thumbnail)))
        return menu

    def show_insert_menu(self) -> None:
        menu = self.build_insert_menu()
        menu.exec(self.image_button.mapToGlobal(QPoint(self.image_button.width() + 4, 0)))

    # -- effects --------------------------------------------------------------
    def auto_blur(self) -> int:
        """OCR the image, find sensitive data and add blur shapes over it."""
        from PySide6.QtWidgets import QApplication

        from screenpilot.ocr import blur_rects, find_sensitive

        if self._ocr_engine is None:
            return 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ocr = self._ocr_engine().recognize(self.result.image)
        except Exception as exc:  # noqa: BLE001
            log.exception("Auto blur OCR failed")
            self.hint.setText(str(exc))
            return 0
        finally:
            QApplication.restoreOverrideCursor()
        matches = find_sensitive(ocr)
        for rect in blur_rects(matches):
            self.doc.add(Shape("blur", Point(rect.x, rect.y), Point(rect.right, rect.bottom), strength=10))
        self.canvas.refresh()
        if matches:
            kinds = ", ".join(sorted({self.t(f"sensitive.{m.kind}") for m in matches}))
            self.hint.setText(self.t("editor.autoblur.done", count=len(matches), kinds=kinds))
        else:
            self.hint.setText(self.t("editor.autoblur.none"))
        return len(matches)

    def undo(self) -> None:
        if self.doc.undo():
            self.canvas.select(None)
            self.canvas.refresh()

    def redo(self) -> None:
        if self.doc.redo():
            self.canvas.refresh()

    # -- output ---------------------------------------------------------------
    def rendered(self) -> CapturedImage:
        return render(self.result.image, self.doc)

    def copy(self) -> None:
        self._copy(self.rendered())

    def edited_result(self) -> CaptureResult:
        image = self.rendered()
        region = self.result.region
        if self.doc.crop is not None:
            c = self.doc.crop
            region = Rect(region.x + c.x, region.y + c.y, c.width, c.height)
        return CaptureResult(
            image=image,
            mode=self.result.mode,
            region=region,
            metadata={**self.result.metadata, "edited": True, "shapes": len(self.doc.shapes)},
        )

    def copy_and_close(self) -> None:
        """Copy the result and get out of the way, ready for Ctrl+V in the target app (nothing is saved)."""
        result = self.edited_result()
        self._copy(result.image)
        self.close()
        if self.on_copy_close is not None:
            self.on_copy_close(result)

    def escape(self) -> None:
        """Esc: drop the selection first; then close (asking when there are annotations)."""
        if self.canvas.selected is not None:
            self.canvas.select(None)
            return
        if (self.doc.shapes or self.doc.crop is not None) and not confirm_discard(self, self.t):
            return
        self.close()

    def save_as(self) -> Path | None:
        path, _ = QFileDialog.getSaveFileName(
            self, self.t("editor.save_as"), str(self.save_dir / "edited.png"), "PNG (*.png);;JPEG (*.jpg)"
        )
        if not path:
            return None
        target = Path(path)
        return self.rendered().save(target, image_format=target.suffix.lstrip(".") or "png")

    def done(self) -> None:
        result = self.edited_result()
        self.close()
        self.on_done(result)
