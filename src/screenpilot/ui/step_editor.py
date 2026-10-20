"""Step numbering window: click to place 1, 2, 3… on a capture.

With "Attach to elements" on, a click on a field, button or panel snaps the
step to that element (badge on its corner, element highlighted) using the
same frame detection as the region selector — a procedure on one screen is
documented in a few clicks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from screenpilot.capture.detect import EdgeDetector
from screenpilot.capture.geometry import Point, Rect
from screenpilot.capture.image import CaptureResult, CapturedImage
from screenpilot.editor import PALETTE, StepDocument, render_steps
from screenpilot.i18n import Translator
from screenpilot.ui.qt_helpers import Retranslator, app_icon, qimage_from_captured
from screenpilot.ui.widgets import Chip, IconButton, label, theme

log = logging.getLogger(__name__)


class StepCanvas(QWidget):
    changed = Signal()

    def __init__(self, image: CapturedImage, doc: StepDocument) -> None:
        super().__init__()
        self.image = image
        self.doc = doc
        self.snap = True
        self._detector: EdgeDetector | None = None
        self._rendered = QPixmap()
        self._drag: int | None = None
        self._drag_moved = False
        self._hover_frame: Rect | None = None
        self.setMouseTracking(True)
        self.setMinimumSize(320, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.refresh()

    @property
    def detector(self) -> EdgeDetector:
        if self._detector is None:
            self._detector = EdgeDetector(self.image)
        return self._detector

    def refresh(self) -> None:
        self._rendered = QPixmap.fromImage(qimage_from_captured(render_steps(self.image, self.doc)))
        self.update()
        self.changed.emit()

    # -- mapping --------------------------------------------------------------
    def _target(self) -> QRectF:
        w, h = self.image.width, self.image.height
        scale = min(self.width() / w, self.height() / h, 1.0)
        tw, th = w * scale, h * scale
        return QRectF((self.width() - tw) / 2, (self.height() - th) / 2, tw, th)

    def to_image(self, pos: QPointF) -> Point:
        t = self._target()
        scale = t.width() / self.image.width
        x = int((pos.x() - t.left()) / scale)
        y = int((pos.y() - t.top()) / scale)
        return Point(min(max(x, 0), self.image.width - 1), min(max(y, 0), self.image.height - 1))

    def _frame_at(self, p: Point) -> Rect | None:
        if not self.snap:
            return None
        boxes = [b for b in self.detector.boxes_at(p) if b.width < self.image.width * 0.95]
        return boxes[0] if boxes else None

    # -- input ----------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        p = self.to_image(event.position())
        hit = self.doc.hit(p)
        if event.button() == Qt.MouseButton.RightButton:
            if hit is not None:
                self.doc.remove(hit)
                self.refresh()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if hit is not None:
            self._drag, self._drag_moved = hit, False
            return
        frame = self._frame_at(p)
        if frame is not None:
            self.doc.add_for_frame(frame)
        else:
            self.doc.add(p)
        self.refresh()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        p = self.to_image(event.position())
        if self._drag is not None:
            self.doc.move(self._drag, p, record=not self._drag_moved)
            self._drag_moved = True
            self.refresh()
            return
        hit = self.doc.hit(p)
        self.setCursor(Qt.CursorShape.SizeAllCursor if hit is not None else Qt.CursorShape.CrossCursor)
        frame = self._frame_at(p) if hit is None else None
        if frame != self._hover_frame:
            self._hover_frame = frame
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag = None

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_frame = None
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        t = self._target()
        p.fillRect(self.rect(), QColor(theme().surface_alt))
        p.drawPixmap(t, self._rendered, QRectF(self._rendered.rect()))
        if self._hover_frame is not None:
            scale = t.width() / self.image.width
            f = self._hover_frame
            r = QRectF(t.left() + f.x * scale, t.top() + f.y * scale, f.width * scale, f.height * scale)
            pen = QPen(QColor(*PALETTE[self.doc.style.color]), 2, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(-2, -2, 2, 2))
        p.end()


class StepEditor(QWidget):
    def __init__(
        self,
        result: CaptureResult,
        translator: Translator,
        *,
        on_done: Callable[[CaptureResult], None],
        copy: Callable[[CapturedImage], None],
        save_dir: Path,
    ) -> None:
        super().__init__(None)
        self.result = result
        self.t = translator.t
        self.on_done = on_done
        self._copy = copy
        self.save_dir = save_dir
        self.doc = StepDocument()
        self.rt = rt = Retranslator(translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setProperty("page", True)
        self.setWindowIcon(app_icon())
        rt.bind(self.setWindowTitle, "steps.title")
        self.resize(min(1200, result.image.width + 80), min(860, result.image.height + 160))

        self.canvas = StepCanvas(result.image, self.doc)
        self.count_label = label(role="caption")

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.snap_chip = Chip("region")
        self.snap_chip.setCheckable(True)
        self.snap_chip.setChecked(True)
        rt.bind(self.snap_chip.setText, "steps.snap")
        rt.bind(self.snap_chip.setToolTip, "steps.snap.tip")
        self.snap_chip.toggled.connect(lambda on: setattr(self.canvas, "snap", on))
        bar.addWidget(self.snap_chip)

        self.colors = QButtonGroup(self)
        for name, rgb in PALETTE.items():
            swatch = QToolButton()
            swatch.setCheckable(True)
            swatch.setFixedSize(26, 26)
            swatch.setProperty("color", name)
            swatch.setStyleSheet(
                f"QToolButton {{ background: rgb{rgb}; border: 2px solid transparent; border-radius: 13px; }}"
                f" QToolButton:checked {{ border: 2px solid {theme().text}; }}"
            )
            swatch.setChecked(name == self.doc.style.color)
            self.colors.addButton(swatch)
            swatch.clicked.connect(lambda _=False, n=name: self._style(color=n))
            bar.addWidget(swatch)

        self.size_combo = QComboBox()
        for size in ("s", "m", "l"):
            self.size_combo.addItem("", size)
        self.size_combo.setCurrentIndex(1)
        self.size_combo.currentIndexChanged.connect(lambda _i: self._style(size=self.size_combo.currentData()))
        bar.addWidget(self.size_combo)
        self.shape_combo = QComboBox()
        for shape in ("circle", "square"):
            self.shape_combo.addItem("", shape)
        self.shape_combo.currentIndexChanged.connect(lambda _i: self._style(shape=self.shape_combo.currentData()))
        bar.addWidget(self.shape_combo)
        start_label = label(role="secondary")
        rt.bind(start_label.setText, "steps.start")
        bar.addWidget(start_label)
        self.start_spin = QSpinBox()
        self.start_spin.setRange(1, 99)
        self.start_spin.valueChanged.connect(lambda v: self._style(start=v))
        bar.addWidget(self.start_spin)
        rt.on_change(self._retranslate_combos)
        self._retranslate_combos()

        undo = IconButton("undo")
        rt.bind(undo.setToolTip, "steps.undo")
        undo.clicked.connect(self.undo)
        clear = IconButton("trash")
        rt.bind(clear.setToolTip, "steps.clear")
        clear.clicked.connect(self._clear)
        bar.addWidget(undo)
        bar.addWidget(clear)
        bar.addStretch(1)

        bottom = QHBoxLayout()
        hint = label(role="caption")
        rt.bind(hint.setText, "steps.hint")
        bottom.addWidget(hint, 1)
        bottom.addWidget(self.count_label)
        self.buttons: dict[str, QPushButton] = {}
        for key, slot, accent, tip in (
            ("steps.copy", self.copy, False, "editor.copy.tip"),
            ("editor.copy_close", self.copy_and_close, False, "editor.copy_close.tip"),
            ("steps.save_as", self.save_as, False, None),
            ("steps.done", self.done, True, "editor.done.tip"),
        ):
            button = QPushButton()
            button.setProperty("accent", accent)
            rt.bind(button.setText, key)
            if tip:
                rt.bind(button.setToolTip, tip)
            button.clicked.connect(slot)
            bottom.addWidget(button)
            self.buttons[key.split(".", 1)[1]] = button
            if accent:
                self.done_button = button

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addLayout(bar)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(bottom)

        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo)
        QShortcut(QKeySequence("Ctrl+Shift+Return"), self, activated=self.copy_and_close)
        self.canvas.changed.connect(self._update_count)
        self._update_count()

    # -- actions --------------------------------------------------------------
    def _retranslate_combos(self) -> None:
        for combo, prefix in ((self.size_combo, "steps.size"), (self.shape_combo, "steps.shape")):
            for i in range(combo.count()):
                combo.setItemText(i, self.t(f"{prefix}.{combo.itemData(i)}"))
        self._update_count()

    def _update_count(self) -> None:
        self.count_label.setText(self.t("steps.count", count=len(self.doc.steps)))

    def _style(self, **changes) -> None:
        self.doc.set_style(**changes)
        self.canvas.refresh()

    def undo(self) -> None:
        if self.doc.undo():
            self.start_spin.blockSignals(True)
            self.start_spin.setValue(self.doc.style.start)
            self.start_spin.blockSignals(False)
            self.canvas.refresh()

    def _clear(self) -> None:
        self.doc.clear()
        self.canvas.refresh()

    def rendered(self) -> CapturedImage:
        return render_steps(self.result.image, self.doc)

    def copy(self) -> None:
        self._copy(self.rendered())

    def save_as(self) -> Path | None:
        path, _ = QFileDialog.getSaveFileName(
            self, self.t("steps.save_as"), str(self.save_dir / "steps.png"), "PNG (*.png);;JPEG (*.jpg)"
        )
        if not path:
            return None
        target = Path(path)
        return self.rendered().save(target, image_format=target.suffix.lstrip(".") or "png")

    def copy_and_close(self) -> None:
        """Copy the numbered image to the clipboard and close (nothing is saved)."""
        self._copy(self.rendered())
        self.close()

    def done(self) -> None:
        image = self.rendered()
        result = CaptureResult(
            image=image,
            mode=self.result.mode,
            region=self.result.region,
            metadata={**self.result.metadata, "steps": len(self.doc.steps)},
        )
        self.close()
        self.on_done(result)

