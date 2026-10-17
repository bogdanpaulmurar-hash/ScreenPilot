"""Image editor window (Editor v1).

Tools: select/move, arrow, line, rectangle, ellipse, highlighter, text,
blur, pixelate, step numbers (attached to detected elements), spotlight and
crop. Rendering always goes through :func:`screenpilot.editor.document.render`
(Pillow), so what you see is exactly what is saved, copied or exported.

Keyboard: V select · A arrow · L line · R rectangle · O ellipse · H highlight
· T text · B blur · X pixelate · N steps · S spotlight · C crop ·
Del delete · Ctrl+Z / Ctrl+Y undo/redo · Ctrl+C copy · Ctrl+S save as ·
Enter done · Esc deselect.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
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
from screenpilot.editor.document import COLORS, EditorDocument, Shape, render
from screenpilot.i18n import Translator
from screenpilot.ui import icons
from screenpilot.ui.qt_helpers import Retranslator, app_icon, qimage_from_captured
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
    ("blur", "blur", "B"),
    ("pixelate", "pixelate", "X"),
    ("spotlight", "spotlight", "S"),
    ("crop", "crop", "C"),
)
DRAG_TOOLS = {"arrow", "line", "rect", "ellipse", "highlight", "blur", "pixelate", "spotlight"}
RENDER_THROTTLE_MS = 16


class EditorCanvas(QWidget):
    changed = Signal()
    selection_changed = Signal()

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
        self.selected: int | None = None
        self._detector: EdgeDetector | None = None
        self._pixmap = QPixmap()
        self._press: Point | None = None
        self._draft: Shape | None = None
        self._move_origin: Shape | None = None
        self._moved = False
        self._crop_draft: Rect | None = None
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._render_now)
        self.setMouseTracking(True)
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
            temp = EditorDocument(doc.width, doc.height, list(doc.shapes) + [self._draft], None, doc.step_start)
            out = render(self.image, temp, apply_crop=False)
        else:
            out = render(self.image, doc, apply_crop=False)
        self._pixmap = QPixmap.fromImage(qimage_from_captured(out))
        self.update()

    def refresh(self) -> None:
        self._render_now()
        self.changed.emit()

    # -- mapping --------------------------------------------------------------
    def target(self) -> QRectF:
        w, h = self.image.width, self.image.height
        scale = min((self.width() - 24) / w, (self.height() - 24) / h, 1.0)
        tw, th = w * scale, h * scale
        return QRectF((self.width() - tw) / 2, (self.height() - th) / 2, tw, th)

    @property
    def scale(self) -> float:
        return self.target().width() / self.image.width

    def to_image(self, pos: QPointF) -> Point:
        t = self.target()
        x = round((pos.x() - t.left()) / self.scale)
        y = round((pos.y() - t.top()) / self.scale)
        return Point(min(max(x, 0), self.image.width - 1), min(max(y, 0), self.image.height - 1))

    def to_view(self, r: Rect) -> QRectF:
        t, s = self.target(), self.scale
        return QRectF(t.left() + r.x * s, t.top() + r.y * s, r.width * s, r.height * s)

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

    def restyle_selected(self, **changes) -> None:
        if self.selected is None:
            return
        from dataclasses import replace

        shape = self.doc.shapes[self.selected]
        allowed = {k: v for k, v in changes.items() if hasattr(shape, k)}
        self.doc.replace(self.selected, replace(shape, **allowed))
        self.refresh()

    def delete_selected(self) -> None:
        if self.selected is not None:
            self.doc.remove(self.selected)
            self.select(None)
            self.refresh()

    def _new_shape(self, kind: str, p1: Point, p2: Point, **extra) -> Shape:
        return Shape(
            kind,
            p1,
            p2,
            color=self.color,
            width=self.width_px,
            font_size=self.font_size,
            fill=self.fill,
            strength=max(6, self.width_px * 3),
            **extra,
        )

    @property
    def detector(self) -> EdgeDetector:
        if self._detector is None:
            self._detector = EdgeDetector(self.image)
        return self._detector

    # -- mouse ----------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            if event.button() == Qt.MouseButton.RightButton:
                hit = self.doc.hit(self.to_image(event.position()))
                if hit is not None:
                    self.doc.remove(hit)
                    self.select(None)
                    self.refresh()
            return
        self.setFocus()
        p = self.to_image(event.position())
        self._press = p
        if self.tool == "select":
            hit = self.doc.hit(p)
            self.select(hit)
            if hit is not None:
                self._move_origin = self.doc.shapes[hit]
                self._moved = False
            return
        if self.tool == "text":
            text, ok = QInputDialog.getMultiLineText(self, self.t("editor.tool.text"), self.t("editor.text.prompt"))
            if ok and text.strip():
                self.doc.add(self._new_shape("text", p, p, text=text.rstrip()))
                self.refresh()
            self._press = None
            return
        if self.tool == "step":
            frame = None
            if self.snap_steps:
                boxes = [b for b in self.detector.boxes_at(p) if b.width < self.image.width * 0.95]
                frame = boxes[0] if boxes else None
            if frame is not None:
                self.doc.add(self._new_shape("rect", Point(frame.x - 2, frame.y - 2), Point(frame.right + 1, frame.bottom + 1), width=3))
                self.doc.add(self._new_shape("step", Point(frame.x, frame.y), Point(frame.x, frame.y)))
            else:
                self.doc.add(self._new_shape("step", p, p))
            self.refresh()
            self._press = None
            return
        if self.tool == "crop":
            self._crop_draft = Rect(p.x, p.y, 1, 1)
            return
        self._draft = self._new_shape(self.tool, p, p)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        p = self.to_image(event.position())
        if self._press is None:
            if self.tool == "select":
                self.setCursor(Qt.CursorShape.SizeAllCursor if self.doc.hit(p) is not None else Qt.CursorShape.ArrowCursor)
            return
        if self.tool == "select" and self.selected is not None and self._move_origin is not None:
            dx, dy = p.x - self._press.x, p.y - self._press.y
            self.doc.replace(self.selected, self._move_origin.moved(dx, dy), record=not self._moved)
            self._moved = True
            self.schedule_render()
            return
        if self.tool == "crop" and self._crop_draft is not None:
            self._crop_draft = Rect.from_points(self._press, p)
            self.update()
            return
        if self._draft is not None:
            end = p
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                end = _constrain(self._press, p, self._draft.kind)
            from dataclasses import replace

            self._draft = replace(self._draft, p2=end)
            self.schedule_render()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
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
                self.doc.add(draft)
            self.refresh()
        elif self._moved:
            self.refresh()
        self._press = None
        self._move_origin = None
        self._moved = False

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        index = self.doc.hit(self.to_image(event.position()))
        if index is not None and self.doc.shapes[index].kind == "text":
            shape = self.doc.shapes[index]
            text, ok = QInputDialog.getMultiLineText(self, self.t("editor.tool.text"), self.t("editor.text.prompt"), shape.text)
            if ok:
                from dataclasses import replace

                self.doc.replace(index, replace(shape, text=text.rstrip()))
                self.refresh()

    # -- painting -------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(t.surface_alt))
        target = self.target()
        p.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
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
        if self.selected is not None and self.selected < len(self.doc.shapes):
            from screenpilot.editor.document import step_diameter, text_bounds

            s = self.doc.shapes[self.selected]
            if s.kind == "text":
                b = text_bounds(s)
            elif s.kind == "step":
                d = step_diameter(s)
                b = Rect(s.p1.x - d // 2, s.p1.y - d // 2, d, d)
            else:
                b = s.bounds
            p.setPen(QPen(QColor(t.accent), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.to_view(b).adjusted(-4, -4, 4, 4))
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
    ) -> None:
        super().__init__(None)
        self.result = result
        self.t = translator.t
        self.on_done = on_done
        self._copy = copy
        self.save_dir = save_dir
        self.settings = settings
        self.doc = EditorDocument(result.image.width, result.image.height)
        self.rt = rt = Retranslator(translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setProperty("page", True)
        self.setWindowIcon(app_icon())
        rt.bind(self.setWindowTitle, "editor.title")
        self.resize(min(1400, result.image.width + 160), min(940, result.image.height + 170))

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
            button = QToolButton()
            button.setCheckable(True)
            button.setProperty("icon_name", icon_name)
            button.setIconSize(QSize(22, 22))
            button.setFixedSize(42, 42)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            rt.bind(button.setToolTip, f"editor.tool.{tool}", params_fn=lambda k=key: {"shortcut": k})
            button.clicked.connect(lambda _=False, tl=tool: self.set_tool(tl))
            self.tool_group.addButton(button)
            tools_col.addWidget(button)
            self.tool_buttons[tool] = button
            QShortcut(QKeySequence(key), self, activated=lambda tl=tool: self.set_tool(tl))
        tools_col.addStretch(1)

        # Top: properties
        props = QHBoxLayout()
        props.setSpacing(6)
        self.color_group = QButtonGroup(self)
        self.color_buttons: dict[str, QToolButton] = {}
        for name, hex_color in COLORS.items():
            swatch = QToolButton()
            swatch.setCheckable(True)
            swatch.setFixedSize(26, 26)
            swatch.setProperty("hex", hex_color)
            swatch.setChecked(hex_color == self.canvas.color)
            swatch.clicked.connect(lambda _=False, c=hex_color: self.set_color(c))
            self.color_group.addButton(swatch)
            props.addWidget(swatch)
            self.color_buttons[hex_color] = swatch
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
        self.font_spin.setRange(8, 96)
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
        props.addStretch(1)
        self.undo_button = self._icon_button("undo", "editor.undo", self.undo)
        self.redo_button = self._icon_button("redo", "editor.redo", self.redo)
        self.delete_button = self._icon_button("trash", "editor.delete", self.canvas.delete_selected)
        for b in (self.undo_button, self.redo_button, self.delete_button):
            props.addWidget(b)

        # Bottom: actions
        bottom = QHBoxLayout()
        self.hint = label(role="caption")
        bottom.addWidget(self.hint, 1)
        for key, slot, accent in (
            ("editor.copy", self.copy, False),
            ("editor.save_as", self.save_as, False),
            ("editor.done", self.done, True),
        ):
            button = QPushButton()
            button.setProperty("accent", accent)
            rt.bind(button.setText, key)
            button.clicked.connect(slot)
            bottom.addWidget(button)

        body = QHBoxLayout()
        body.addLayout(tools_col)
        body.addWidget(self.canvas, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addLayout(props)
        layout.addLayout(body, 1)
        layout.addLayout(bottom)

        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence.StandardKey.Delete, self, activated=self.canvas.delete_selected)
        QShortcut(QKeySequence.StandardKey.Copy, self, activated=self.copy)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save_as)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self, activated=self.done)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=lambda: self.canvas.select(None))
        self.canvas.selection_changed.connect(self._on_selection)
        self.canvas.changed.connect(self._update_hint)
        on_theme_changed(self, self._restyle)
        rt.on_change(self._update_hint)
        self.set_tool("arrow")

    def _icon_button(self, icon_name: str, tip_key: str, slot) -> QToolButton:
        from screenpilot.ui.widgets import IconButton

        button = IconButton(icon_name, size=18)
        self.rt.bind(button.setToolTip, tip_key)
        button.clicked.connect(slot)
        return button

    def _restyle(self, t) -> None:
        for button in self.tool_buttons.values():
            button.setIcon(icons.icon(button.property("icon_name"), t.text, 22))
        for hex_color, swatch in self.color_buttons.items():
            border = t.text if swatch.isChecked() else t.border_strong
            swatch.setStyleSheet(
                f"QToolButton {{ background: {hex_color}; border: 2px solid {border}; border-radius: 13px; }}"
                f" QToolButton:checked {{ border: 3px solid {t.accent}; }}"
            )

    # -- state ----------------------------------------------------------------
    def set_tool(self, tool: str) -> None:
        self.canvas.set_tool(tool)
        self.tool_buttons[tool].setChecked(True)
        self._update_hint()

    def set_color(self, color: str) -> None:
        self.canvas.color = color
        self.color_buttons[color].setChecked(True)
        self.canvas.restyle_selected(color=color)
        if self.settings is not None:
            self.settings.set("editor.color", color)

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
        index = self.canvas.selected
        self.delete_button.setEnabled(index is not None)
        if index is None:
            return
        shape = self.doc.shapes[index]
        for widget, value in ((self.width_spin, shape.width), (self.font_spin, shape.font_size)):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self.fill_chip.blockSignals(True)
        self.fill_chip.setChecked(shape.fill)
        self.fill_chip.blockSignals(False)

    def _update_hint(self) -> None:
        tool = self.canvas.tool
        self.hint.setText(self.t(f"editor.hint.{tool}") if self.rt.translator.has(f"editor.hint.{tool}") else self.t("editor.hint.draw"))
        self.undo_button.setEnabled(self.doc.can_undo)

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

    def save_as(self) -> Path | None:
        path, _ = QFileDialog.getSaveFileName(
            self, self.t("editor.save_as"), str(self.save_dir / "edited.png"), "PNG (*.png);;JPEG (*.jpg)"
        )
        if not path:
            return None
        target = Path(path)
        return self.rendered().save(target, image_format=target.suffix.lstrip(".") or "png")

    def done(self) -> None:
        image = self.rendered()
        region = self.result.region
        if self.doc.crop is not None:
            c = self.doc.crop
            region = Rect(region.x + c.x, region.y + c.y, c.width, c.height)
        result = CaptureResult(
            image=image,
            mode=self.result.mode,
            region=region,
            metadata={**self.result.metadata, "edited": True, "shapes": len(self.doc.shapes)},
        )
        self.close()
        self.on_done(result)
