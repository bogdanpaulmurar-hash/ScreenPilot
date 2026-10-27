"""Post-capture quick preview card and pinned-to-screen windows.

Quick preview: a small floating card (bottom-right) with the thumbnail and
one-click actions. The thumbnail can be dragged straight into Outlook, Teams,
Word, a browser upload field or Explorer. It fades away on its own and never
steals keyboard focus.

Pin: keeps a capture floating above all windows — handy for comparing,
copying values from one screen into another, or following instructions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEasingCurve, QMimeData, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QCursor,
    QDrag,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from screenpilot.capture.image import CaptureResult
from screenpilot.i18n import Translator
from screenpilot.ui.qt_helpers import Retranslator, qimage_from_captured, sharp_thumbnail
from screenpilot.ui.widgets import IconBadge, IconButton, label, theme

log = logging.getLogger(__name__)

PREVIEW_TIMEOUT_MS = 6000
THUMB = QSize(216, 128)
MARGIN = 20


def screen_area_for_cursor():
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    return screen.availableGeometry()


class DragThumbnail(QLabel):
    """Thumbnail that can be dragged into other applications as a file + image."""

    def __init__(self, pixmap: QPixmap, file_provider: Callable[[], Path | None]) -> None:
        super().__init__()
        self._file_provider = file_provider
        self._press: QPoint | None = None
        self._full = pixmap
        self.setPixmap(sharp_thumbnail(pixmap, THUMB, self.devicePixelRatioF()))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(THUMB)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        t = theme()
        self.setStyleSheet(f"background: {t.surface_alt}; border: 1px solid {t.border}; border-radius: 6px;")

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position().toPoint()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press is None or (event.position().toPoint() - self._press).manhattanLength() < 8:
            return
        self._press = None
        mime = QMimeData()
        mime.setImageData(self._full.toImage())
        path = self._file_provider()
        if path is not None:
            mime.setUrls([QUrl.fromLocalFile(str(path))])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.pixmap().scaled(QSize(120, 80), Qt.AspectRatioMode.KeepAspectRatio))
        drag.exec(Qt.DropAction.CopyAction)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._press = None


class QuickPreview(QWidget):
    pin_requested = Signal(object)  # CaptureResult
    more_requested = Signal(object)  # CaptureResult: extend downwards with a scrolling capture
    steps_requested = Signal(object)  # CaptureResult: open step numbering
    edit_requested = Signal(object)  # CaptureResult: open the editor
    closed = Signal()

    def __init__(
        self,
        result: CaptureResult,
        saved_path: Path | None,
        translator: Translator,
        *,
        copy: Callable[[], None],
        save_as: Callable[[], Path | None],
        reveal: Callable[[Path], None],
        temp_file: Callable[[], Path | None],
    ) -> None:
        super().__init__(None)
        self.result = result
        self.saved_path = saved_path
        self._temp_file = temp_file
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
        card.setObjectName("PreviewCard")
        card.setStyleSheet(
            f"QFrame#PreviewCard {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: 12px; }}"
        )
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        card.setGraphicsEffect(shadow)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        pixmap = QPixmap.fromImage(qimage_from_captured(result.image))
        self.thumbnail = DragThumbnail(pixmap, self._drag_file)
        layout.addWidget(self.thumbnail)

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title_row.addWidget(IconBadge("check", 14, token="success"))
        self.title = label(role="subtitle")
        title_row.addWidget(self.title, 1)
        close = IconButton("close", size=14, token="text_secondary")
        rt.bind(close.setToolTip, "preview.close")
        close.clicked.connect(self.dismiss)
        title_row.addWidget(close)
        layout.addLayout(title_row)
        hint = label(role="caption")
        rt.bind(hint.setText, "preview.drag_hint")
        layout.addWidget(hint)

        actions = QHBoxLayout()
        actions.setSpacing(2)
        self.buttons: dict[str, IconButton] = {}
        for name, key, slot in (
            ("copy", "preview.copy", copy),
            ("save_as", "preview.save_as", lambda: self._after_save(save_as())),
            ("pin", "preview.pin", lambda: (self.pin_requested.emit(self.result), self.dismiss())),
            ("steps", "preview.steps", lambda: (self.steps_requested.emit(self.result), self.dismiss())),
            ("folder", "preview.folder", lambda: self.saved_path and reveal(self.saved_path)),
            ("edit", "preview.edit", lambda: (self.edit_requested.emit(self.result), self.dismiss())),
        ):
            button = IconButton(name, size=18)
            rt.bind(button.setToolTip, key)
            if slot is None:
                button.setEnabled(False)
            else:
                button.clicked.connect(slot)
            actions.addWidget(button)
            self.buttons[name] = button
        actions.addStretch(1)
        if result.mode != "scrolling":
            more = QPushButton()
            more.setProperty("accent", True)
            more.setCursor(Qt.CursorShape.PointingHandCursor)
            rt.bind(more.setText, "preview.more")
            rt.bind(more.setToolTip, "preview.more.tip")
            more.clicked.connect(lambda: (self.more_requested.emit(self.result), self.dismiss()))
            actions.addWidget(more)
            self.more_button = more
        layout.addLayout(actions)

        rt.on_change(self._refresh_title)
        self._refresh_title()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(220)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _refresh_title(self) -> None:
        img = self.result.image
        state = self.t("preview.saved") if self.saved_path else self.t("preview.copied")
        self.title.setText(f"{state} · {img.width}×{img.height}")
        self.buttons["folder"].setEnabled(self.saved_path is not None)

    def _after_save(self, path: Path | None) -> None:
        if path is not None:
            self.saved_path = path
            self._refresh_title()

    def _drag_file(self) -> Path | None:
        return self.saved_path or self._temp_file()

    def show_animated(self) -> None:
        self.adjustSize()
        area = screen_area_for_cursor()
        self.move(area.right() - self.width() + MARGIN - 8, area.bottom() - self.height() + MARGIN - 8)
        self.setWindowOpacity(0.0)
        self.show()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._timer.start(PREVIEW_TIMEOUT_MS)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._timer.start(PREVIEW_TIMEOUT_MS // 2)
        super().leaveEvent(event)

    def dismiss(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self._timer.stop()
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.close)
        self._fade.start()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)


class ScrollIndicator(QWidget):
    """Progress pill shown next to the region while a scrolling capture runs.

    The pill carries an OK button that finishes the capture (like Esc). The
    window never takes focus or activates (``WS_EX_NOACTIVATE`` on Windows),
    so the app being captured keeps the keyboard, and it is placed outside the
    region and excluded from screen capture, so it never shows in the frames.
    Resting the mouse on the pill pauses automatic scrolling (``hovered``),
    otherwise the scroller would move the cursor away from the button.
    """

    finish_requested = Signal()

    HEIGHT = 40
    OK_WIDTH = 64

    def __init__(self, translator: Translator, anchor: QPoint, *, manual: bool = False, top: int | None = None) -> None:
        super().__init__(None)
        import threading

        self.t = translator.t
        self.anchor = anchor
        self.region_top = top
        self.manual = manual
        self.hovered = threading.Event()  # read by the scrolling worker
        self.finished = threading.Event()
        self._ok_hover = False
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setToolTip(self.t("scroll.ok.tooltip"))
        self.text = self.t("scroll.manual.starting" if manual else "scroll.starting")
        self.setFixedHeight(self.HEIGHT)
        self._native_done = False

    def set_progress(self, frames: int, height: int) -> None:
        key = "scroll.manual.progress" if self.manual else "scroll.progress"
        self.text = self.t(key, frames=frames, height=height)
        self._fit()
        self.update()

    def _fit(self) -> None:
        from screenpilot.ui.theme import ui_font

        self.setFont(ui_font(10))
        self.setFixedWidth(self.fontMetrics().horizontalAdvance(self.text) + 48 + self.OK_WIDTH)

    def ok_rect(self) -> QRectF:
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        return QRectF(r.right() - self.OK_WIDTH - 2, r.top() + 5, self.OK_WIDTH, r.height() - 10)

    def show_near(self) -> None:
        """Below the region, else above it (outside it, so it never appears in the frames)."""
        self._fit()
        area = QGuiApplication.screenAt(self.anchor) or QGuiApplication.primaryScreen()
        geo = area.availableGeometry()
        x = min(max(geo.left() + 8, self.anchor.x()), geo.right() - self.width() - 8)
        y = self.anchor.y() + 12
        if y + self.height() > geo.bottom():
            above = (self.region_top if self.region_top is not None else geo.top()) - self.height() - 12
            # no room below or above: top of the screen (hidden from capture on Windows 10 2004+)
            y = above if above >= geo.top() else geo.top() + 12
        self.move(x, y)
        self.show()
        self._make_native()

    def _make_native(self) -> None:
        """Windows: never activate on click; keep out of screenshots."""
        if self._native_done:
            return
        self._native_done = True
        from screenpilot.platform.win32_window import exclude_from_capture, set_no_activate

        hwnd = int(self.winId())
        set_no_activate(hwnd)
        exclude_from_capture(hwnd)

    def hideEvent(self, event) -> None:  # noqa: N802
        self.hovered.clear()  # hidden for the pause question: no leave event follows
        super().hideEvent(event)

    # -- mouse: the OK button ------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self.hovered.set()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.hovered.clear()
        self._ok_hover = False
        self.update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        over = self.ok_rect().contains(event.position())
        if over != self._ok_hover:
            self._ok_hover = over
            self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        # acts on press: the window never activates, and the automatic scroller
        # may move the cursor right after a release
        if event.button() == Qt.MouseButton.LeftButton and self.ok_rect().contains(event.position()):
            self.finish()
        event.accept()

    def finish(self) -> None:
        if self.finished.is_set():
            return
        self.finished.set()
        self.text = self.t("scroll.finishing")
        self._fit()
        self.update()
        self.finish_requested.emit()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(32, 32, 32, 235))
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        accent = QColor(theme().accent)
        p.setBrush(accent.lighter(150))
        p.drawEllipse(QRectF(r.left() + 14, r.center().y() - 4, 8, 8))
        p.setPen(QColor("white"))
        text_rect = r.adjusted(30, 0, -12 - self.OK_WIDTH, 0)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text)
        ok = self.ok_rect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent.lighter(120) if self._ok_hover else accent)
        p.drawRoundedRect(ok, ok.height() / 2, ok.height() / 2)
        p.setPen(QPen(QColor("white"), 2))
        cy = ok.center().y()
        x0 = ok.left() + 12
        p.drawPolyline([QPoint(int(x0), int(cy)), QPoint(int(x0 + 4), int(cy + 4)), QPoint(int(x0 + 11), int(cy - 4))])
        p.drawText(ok.adjusted(26, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.t("scroll.ok"))
        p.end()


class PinWindow(QWidget):
    """A capture pinned above all windows."""

    MIN_ZOOM, MAX_ZOOM = 0.1, 4.0

    def __init__(self, image: QImage, translator: Translator, *, copy: Callable[[], None]) -> None:
        super().__init__(None)
        self.t = translator.t
        self._copy = copy
        self._image = image
        self._pixmap = QPixmap.fromImage(image)
        self.zoom = 1.0
        self._drag_offset: QPoint | None = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip(self.t("pin.hint"))
        self._resize_to_zoom()

    def _resize_to_zoom(self) -> None:
        dpr = self.devicePixelRatioF() or 1.0
        w = max(24, round(self._pixmap.width() * self.zoom / dpr))
        h = max(24, round(self._pixmap.height() * self.zoom / dpr))
        self.resize(w + 2, h + 2)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 1.0)
        p.drawPixmap(self.rect().adjusted(1, 1, -1, -1), self._pixmap)
        p.setPen(QPen(QColor(theme().accent), 1))
        p.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        p.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.close()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.setWindowOpacity(min(1.0, max(0.2, self.windowOpacity() + 0.1 * steps)))
        else:
            self.zoom = min(self.MAX_ZOOM, max(self.MIN_ZOOM, self.zoom * (1.1**steps)))
            self._resize_to_zoom()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._copy()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.addAction(self.t("pin.copy"), self._copy)
        menu.addAction(self.t("pin.save"), self._save_as)
        menu.addAction(self.t("pin.reset"), self._reset)
        menu.addSeparator()
        menu.addAction(self.t("pin.close"), self.close)
        menu.exec(event.globalPos())

    def _reset(self) -> None:
        self.zoom = 1.0
        self.setWindowOpacity(1.0)
        self._resize_to_zoom()

    def _save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, self.t("pin.save"), "pinned.png", "PNG (*.png);;JPEG (*.jpg)")
        if path:
            self._image.save(path)

