"""OCR result window: recognized text (editable for corrections), the image
with word boxes, search with highlighting, language switch, copy and save."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from screenpilot.capture.image import CaptureResult
from screenpilot.i18n import Translator
from screenpilot.ocr import OcrResult, to_markdown
from screenpilot.ui.qt_helpers import Retranslator, app_icon, fit_target, on_escape, qimage_from_captured
from screenpilot.ui.widgets import Chip, label, theme

log = logging.getLogger(__name__)

LANGS = ("auto", "en", "ro", "de")


class WordOverlay(QWidget):
    """The capture with recognized words outlined; matches highlighted."""

    def __init__(self, result: CaptureResult) -> None:
        super().__init__()
        self.capture = result
        self.pixmap = QPixmap.fromImage(qimage_from_captured(result.image))
        self.ocr: OcrResult | None = None
        self.query = ""
        self.show_boxes = True
        self.setMinimumSize(240, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_result(self, ocr: OcrResult | None) -> None:
        self.ocr = ocr
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(t.surface_alt))
        w, h = self.pixmap.width(), self.pixmap.height()
        target = fit_target(self, w, h, margin=8)
        scale = target.width() / w
        p.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))
        if self.ocr is not None and (self.show_boxes or self.query):
            q = self.query.strip().lower()
            for word in self.ocr.words:
                r = word.rect
                box = QRectF(target.left() + r.x * scale, target.top() + r.y * scale, r.width * scale, r.height * scale)
                if q and q in word.text.lower():
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(255, 185, 0, 120))
                    p.drawRect(box.adjusted(-2, -2, 2, 2))
                elif self.show_boxes:
                    p.setPen(QPen(QColor(t.accent), 1))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRect(box)
        p.end()


class OcrWindow(QWidget):
    rerun_requested = Signal(str)  # language

    def __init__(
        self,
        capture: CaptureResult,
        translator: Translator,
        *,
        copy_text: Callable[[str], None],
        save_dir: Path,
        language: str = "auto",
    ) -> None:
        super().__init__(None)
        self.capture = capture
        self.t = translator.t
        self._copy_text = copy_text
        self.save_dir = save_dir
        self.result: OcrResult | None = None
        self.rt = rt = Retranslator(translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setProperty("page", True)
        self.setWindowIcon(app_icon())
        rt.bind(self.setWindowTitle, "ocr.title")
        self.resize(1000, 620)

        top = QHBoxLayout()
        lang_label = label(role="secondary")
        rt.bind(lang_label.setText, "ocr.language")
        top.addWidget(lang_label)
        self.language = QComboBox()
        for code in LANGS:
            self.language.addItem("", code)
        self.language.setCurrentIndex(max(0, self.language.findData(language)))
        top.addWidget(self.language)
        rerun = QPushButton()
        rt.bind(rerun.setText, "ocr.rerun")
        rerun.clicked.connect(lambda: self.rerun_requested.emit(self.language.currentData()))
        top.addWidget(rerun)
        top.addSpacing(12)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        rt.bind(self.search.setPlaceholderText, "ocr.search")
        self.search.textChanged.connect(self._on_search)
        top.addWidget(self.search, 1)
        self.matches = label(role="caption")
        top.addWidget(self.matches)
        self.boxes_chip = Chip("search")
        self.boxes_chip.setCheckable(True)
        self.boxes_chip.setChecked(True)
        rt.bind(self.boxes_chip.setText, "ocr.boxes")
        self.boxes_chip.toggled.connect(self._toggle_boxes)
        top.addWidget(self.boxes_chip)

        self.overlay = WordOverlay(capture)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(self.t("ocr.running"))
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.overlay)
        splitter.addWidget(self.text)
        splitter.setSizes([520, 480])

        bottom = QHBoxLayout()
        self.status = label(role="caption")
        bottom.addWidget(self.status, 1)
        for key, slot, accent in (
            ("ocr.copy_md", self.copy_markdown, False),
            ("ocr.save", self.save, False),
            ("ocr.copy", self.copy, True),
        ):
            button = QPushButton()
            button.setProperty("accent", accent)
            rt.bind(button.setText, key)
            button.clicked.connect(slot)
            bottom.addWidget(button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        layout.addLayout(bottom)
        rt.on_change(self._retranslate)
        self._retranslate()

        on_escape(self, self.close)  # Esc: back to work

    def _retranslate(self) -> None:
        for i in range(self.language.count()):
            code = self.language.itemData(i)
            self.language.setItemText(i, self.t("ocr.language.auto") if code == "auto" else {"en": "English", "ro": "Română", "de": "Deutsch"}[code])
        self._update_status()
        self._on_search(self.search.text())

    def set_result(self, result: OcrResult) -> None:
        self.result = result
        self.text.setPlainText(result.text)
        if not result.text.strip():
            self.text.setPlaceholderText(self.t("ocr.empty"))
        self.overlay.set_result(result)
        self._update_status()

    def set_error(self, message: str) -> None:
        self.text.setPlaceholderText(message)
        self.status.setText(message)

    def set_running(self) -> None:
        self.text.clear()
        self.text.setPlaceholderText(self.t("ocr.running"))
        self.status.setText(self.t("ocr.running"))

    def _update_status(self) -> None:
        r = self.result
        if r is None:
            return
        if r.confidence is not None:
            self.status.setText(self.t("ocr.confidence", value=r.confidence) + f" · {r.engine} · {r.language}")
        else:
            self.status.setText(self.t("ocr.confidence.na", engine=f"{r.engine} · {r.language}"))

    def _on_search(self, query: str) -> None:
        self.overlay.query = query
        self.overlay.update()
        if self.result is not None and query.strip():
            self.matches.setText(self.t("ocr.matches", count=len(self.result.find(query))))
        else:
            self.matches.setText("")

    def _toggle_boxes(self, on: bool) -> None:
        self.overlay.show_boxes = on
        self.overlay.update()

    def copy(self) -> None:
        self._copy_text(self.text.toPlainText())

    def copy_markdown(self) -> None:
        if self.result is not None and self.text.toPlainText() == self.result.text:
            self._copy_text(to_markdown(self.result))
        else:  # the user corrected the text: keep their version
            self._copy_text(self.text.toPlainText())

    def save(self) -> Path | None:
        path, chosen = QFileDialog.getSaveFileName(
            self, self.t("ocr.save"), str(self.save_dir / "text.txt"), self.t("ocr.save_filter")
        )
        if not path:
            return None
        target = Path(path)
        content = self.text.toPlainText()
        if target.suffix.lower() == ".md" and self.result is not None and content == self.result.text:
            content = to_markdown(self.result)
        target.write_text(content, encoding="utf-8")
        return target

