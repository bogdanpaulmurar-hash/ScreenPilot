"""Procedure recording UI: the floating session bar and the procedure editor."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QMarginsF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPageLayout, QPageSize, QPainter, QPaintEvent, QPdfWriter, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from screenpilot.docs import ProcedureSession, default_name, export_docx, export_html, export_markdown
from screenpilot.i18n import Translator
from screenpilot.ui.qt_helpers import Retranslator, app_icon
from screenpilot.ui.theme import ui_font
from screenpilot.ui.widgets import IconButton, label, theme

log = logging.getLogger(__name__)


class SessionBar(QWidget):
    """Pill at the bottom of the screen while a procedure is being recorded."""

    finish_clicked = Signal()
    cancel_clicked = Signal()

    def __init__(self, translator: Translator, is_busy: Callable[[], bool]) -> None:
        super().__init__(None)
        self.t = translator.t
        self._is_busy = is_busy
        self.count = 0
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 8, 8, 8)
        row.setSpacing(8)
        self.text = QLabel()
        self.text.setStyleSheet("color: white; background: transparent;")
        self.text.setFont(ui_font(10))
        row.addWidget(self.text, 1)
        self.finish = QPushButton()
        self.finish.setProperty("accent", True)
        self.finish.setCursor(Qt.CursorShape.PointingHandCursor)
        self.finish.clicked.connect(self.finish_clicked.emit)
        row.addWidget(self.finish)
        self.cancel = QPushButton("✕")
        self.cancel.setFixedWidth(34)
        self.cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel.clicked.connect(self.cancel_clicked.emit)
        self.cancel.setStyleSheet("QPushButton { color: white; background: transparent; border: none; }")
        row.addWidget(self.cancel)
        self._resume = QTimer(self)
        self._resume.setInterval(300)
        self._resume.timeout.connect(self._try_resume)
        self.retranslate()

    def retranslate(self) -> None:
        self.text.setText(self.t("procedure.bar", count=self.count))
        self.finish.setText(self.t("procedure.finish"))
        self.cancel.setToolTip(self.t("procedure.cancel"))
        self.adjustSize()

    def set_count(self, count: int) -> None:
        self.count = count
        self.retranslate()

    def show_bar(self) -> None:
        from PySide6.QtGui import QGuiApplication

        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        self.move(geo.center().x() - self.width() // 2, geo.bottom() - self.height() - 16)
        self.show()

    def suspend(self) -> None:
        self.hide()
        self._resume.start()

    def _try_resume(self) -> None:
        if not self._is_busy():
            self._resume.stop()
            self.show_bar()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(32, 32, 32, 238))
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setBrush(QColor(232, 17, 35))
        p.drawEllipse(QRectF(r.left() + 8, r.center().y() - 4, 8, 8))
        p.end()


def export_pdf(session: ProcedureSession, target: Path, *, step_label: str = "Step") -> Path:
    """PDF through Qt (proper Unicode fonts, selectable text)."""
    p = session.procedure
    parts = [f"<h1 style='font-size:22pt'>{_e(p.title or 'Procedure')}</h1>"]
    meta = " · ".join(x for x in (p.author, p.created.replace("T", " ")) if x)
    if meta:
        parts.append(f"<p style='color:#666'>{_e(meta)}</p>")
    if p.introduction:
        parts.append(f"<p>{_e(p.introduction)}</p>")
    page_width_px = 640
    for i, step in enumerate(session.steps):
        title = f"{step_label} {i + 1}" + (f": {step.title}" if step.title else "")
        parts.append(f"<h2 style='color:#0067c0;font-size:14pt'>{_e(title)}</h2>")
        if step.description:
            parts.append(f"<p>{_e(step.description)}</p>")
        width = min(page_width_px, step.width or page_width_px)
        uri = session.image_path(step).resolve().as_uri()
        parts.append(f"<p align='center'><img src='{uri}' width='{width}'></p>")
    doc = QTextDocument()
    doc.setDefaultFont(ui_font(10))
    doc.setHtml("".join(parts))
    writer = QPdfWriter(str(target))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter)
    writer.setResolution(96)
    writer.setTitle(p.title or "Procedure")
    writer.setCreator("ScreenPilot")
    doc.setPageSize(writer.pageLayout().paintRectPixels(96).size().toSizeF())
    doc.print_(writer)
    return target


def _e(text: str) -> str:
    import html

    return html.escape(text).replace("\n", "<br>")


class ProcedureWindow(QWidget):
    add_capture = Signal()
    edit_image = Signal(int)

    def __init__(self, session: ProcedureSession, translator: Translator, *, save_dir: Path, reveal: Callable[[Path], None], lang: str = "en") -> None:
        super().__init__(None)
        self.session = session
        self.t = translator.t
        self.translator = translator
        self.save_dir = save_dir
        self._reveal = reveal
        self.rt = rt = Retranslator(translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setProperty("page", True)
        self.setWindowIcon(app_icon())
        rt.bind(self.setWindowTitle, "procedure.window")
        self.resize(1100, 720)
        self._loading = False

        head = QVBoxLayout()
        title_row = QHBoxLayout()
        self.title = QLineEdit(session.procedure.title)
        self.title.setFont(ui_font(14))
        rt.bind(self.title.setPlaceholderText, "procedure.title.placeholder")
        self.title.editingFinished.connect(self._save_meta)
        self.author = QLineEdit(session.procedure.author)
        self.author.setMaximumWidth(220)
        rt.bind(self.author.setPlaceholderText, "procedure.author")
        self.author.editingFinished.connect(self._save_meta)
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.author)
        head.addLayout(title_row)
        self.intro = QPlainTextEdit(session.procedure.introduction)
        self.intro.setMaximumHeight(64)
        rt.bind(self.intro.setPlaceholderText, "procedure.intro")
        self.intro.textChanged.connect(self._save_meta)
        head.addWidget(self.intro)

        self.list = QListWidget()
        self.list.setIconSize(QSize(120, 72))
        self.list.setMinimumWidth(300)
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list.model().rowsMoved.connect(self._on_rows_moved)
        self.list.currentRowChanged.connect(self._show_step)

        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        self.step_title = QLineEdit()
        rt.bind(self.step_title.setPlaceholderText, "procedure.step_title")
        self.step_title.editingFinished.connect(self._save_step)
        self.step_desc = QPlainTextEdit()
        self.step_desc.setMaximumHeight(90)
        rt.bind(self.step_desc.setPlaceholderText, "procedure.step_desc")
        self.step_desc.textChanged.connect(self._save_step)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(260)
        self.preview.setStyleSheet(f"background: {theme().surface_alt}; border-radius: 8px;")
        tools = QHBoxLayout()
        self.buttons: dict[str, QWidget] = {}
        for icon_name, key, slot in (
            ("up", "procedure.up", lambda: self._move(-1)),
            ("down", "procedure.down", lambda: self._move(1)),
            ("edit", "procedure.edit_image", lambda: self.edit_image.emit(self.list.currentRow())),
            ("trash", "procedure.delete", self._delete),
        ):
            b = IconButton(icon_name, size=18)
            rt.bind(b.setToolTip, key)
            b.clicked.connect(slot)
            tools.addWidget(b)
            self.buttons[icon_name] = b
        tools.addStretch(1)
        dl.addWidget(self.step_title)
        dl.addWidget(self.step_desc)
        dl.addWidget(self.preview, 1)
        dl.addLayout(tools)
        self.empty = label(role="secondary", wrap=True)
        rt.bind(self.empty.setText, "procedure.empty")
        dl.addWidget(self.empty)

        splitter = QSplitter()
        splitter.addWidget(self.list)
        splitter.addWidget(detail)
        splitter.setSizes([320, 780])

        bottom = QHBoxLayout()
        add = QPushButton()
        rt.bind(add.setText, "procedure.add")
        add.clicked.connect(self.add_capture.emit)
        bottom.addWidget(add)
        bottom.addStretch(1)
        self.export_button = QPushButton()
        self.export_button.setProperty("accent", True)
        rt.bind(self.export_button.setText, "procedure.export")
        menu = QMenu(self)
        self.export_actions = {}
        for fmt in ("word", "pdf", "html", "md"):
            action = menu.addAction("")
            action.triggered.connect(lambda _=False, f=fmt: self.export(f))
            self.export_actions[fmt] = action
        self.export_button.setMenu(menu)
        bottom.addWidget(self.export_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.addLayout(head)
        layout.addWidget(splitter, 1)
        layout.addLayout(bottom)
        rt.on_change(self._retranslate)
        self._retranslate()
        self.reload()

    # -- data -----------------------------------------------------------------
    def _retranslate(self) -> None:
        for fmt, action in self.export_actions.items():
            action.setText(self.t(f"procedure.export.{fmt}"))
        self.reload(keep_row=True)

    def reload(self, keep_row: bool = False) -> None:
        row = self.list.currentRow() if keep_row else len(self.session.steps) - 1
        self._loading = True
        self.list.clear()
        label_text = self.t("procedure.step_label")
        for i, step in enumerate(self.session.steps):
            item = QListWidgetItem(f"{label_text} {i + 1}\n{step.title}")
            item.setIcon(QIcon(QPixmap(str(self.session.image_path(step)))))
            self.list.addItem(item)
        self._loading = False
        has = bool(self.session.steps)
        self.empty.setVisible(not has)
        for w in (self.step_title, self.step_desc, self.preview, *self.buttons.values()):
            w.setVisible(has)
        self.export_button.setEnabled(has)
        if has:
            self.list.setCurrentRow(max(0, min(row, len(self.session.steps) - 1)))

    def _show_step(self, row: int) -> None:
        if self._loading or not 0 <= row < len(self.session.steps):
            return
        step = self.session.steps[row]
        self._loading = True
        self.step_title.setText(step.title)
        self.step_desc.setPlainText(step.description)
        self._loading = False
        pix = QPixmap(str(self.session.image_path(step)))
        self.preview.setPixmap(pix.scaled(QSize(760, 420), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _save_meta(self) -> None:
        p = self.session.procedure
        p.title, p.author, p.introduction = self.title.text(), self.author.text(), self.intro.toPlainText()
        self.session.save()

    def _save_step(self) -> None:
        row = self.list.currentRow()
        if self._loading or row < 0:
            return
        self.session.update(row, title=self.step_title.text(), description=self.step_desc.toPlainText())
        item = self.list.item(row)
        item.setText(f"{self.t('procedure.step_label')} {row + 1}\n{self.step_title.text()}")

    def _on_rows_moved(self, _parent, start, _end, _dest, dest_row) -> None:
        new = dest_row if dest_row < start else dest_row - 1
        self.session.move(start, new)
        self.reload()
        self.list.setCurrentRow(new)

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        new = row + delta
        if 0 <= row < len(self.session.steps) and 0 <= new < len(self.session.steps):
            self.session.move(row, new)
            self.reload()
            self.list.setCurrentRow(new)

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.session.remove(row)
            self.reload()

    def step_added(self) -> None:
        self.reload()

    # -- export ---------------------------------------------------------------
    def export(self, fmt: str, target: Path | None = None) -> Path | None:
        self._save_meta()
        ext = {"word": "docx", "pdf": "pdf", "html": "html", "md": "md"}[fmt]
        if target is None:
            suggested = self.save_dir / f"{default_name(self.session)}.{ext}"
            path, _ = QFileDialog.getSaveFileName(self, self.t("procedure.export"), str(suggested), f"*.{ext}")
            if not path:
                return None
            target = Path(path)
        label_text = self.t("procedure.step_label")
        lang = {"ro": "ro-RO", "de": "de-DE"}.get(self.translator.language, "en-US")
        try:
            if fmt == "word":
                export_docx(self.session, target, step_label=label_text, lang=lang)
            elif fmt == "pdf":
                export_pdf(self.session, target, step_label=label_text)
            elif fmt == "html":
                export_html(self.session, target, step_label=label_text, lang=lang[:2])
            else:
                export_markdown(self.session, target, step_label=label_text)
        except OSError as exc:
            QMessageBox.warning(self, self.t("procedure.export"), str(exc))
            return None
        log.info("Procedure exported to %s", target)
        try:
            self._reveal(target)
        except OSError:
            pass
        return target
