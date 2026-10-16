"""Countdown badge shown before a delayed capture.

It is hidden for the last moment before the capture so it never appears in
the screenshot.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QKeyEvent, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from screenpilot.i18n import Translator
from screenpilot.ui.qt_helpers import ACCENT, ui_font

HIDE_BEFORE_CAPTURE_MS = 150


class CountdownOverlay(QWidget):
    elapsed = Signal()  # time is up: capture now
    cancelled = Signal()

    def __init__(self, seconds: int, translator: Translator) -> None:
        super().__init__(None)
        self.remaining = seconds
        self.translator = translator
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(180, 150)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 24, area.bottom() - self.height() - 24)
        if self.remaining <= 0:
            self.elapsed.emit()
            self.close()
            return
        self.show()
        self._timer.start()

    def _tick(self) -> None:
        self.remaining -= 1
        if self.remaining <= 0:
            self._timer.stop()
            self.hide()
            QTimer.singleShot(HIDE_BEFORE_CAPTURE_MS, self._fire)
        else:
            self.update()

    def _fire(self) -> None:
        self.elapsed.emit()
        self.close()

    def cancel(self) -> None:
        self._timer.stop()
        self.cancelled.emit()
        self.close()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.cancel()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(32, 32, 32, 225))
        p.drawRoundedRect(QRectF(self.rect()), 16, 16)
        p.setPen(ACCENT.lighter(150))
        p.setFont(ui_font(40, bold=True))
        p.drawText(QRectF(0, 8, self.width(), 96), Qt.AlignmentFlag.AlignCenter, str(self.remaining))
        p.setPen(QColor(220, 220, 220))
        p.setFont(ui_font(9))
        p.drawText(QRectF(0, 104, self.width(), 36), Qt.AlignmentFlag.AlignCenter, self.translator.t("countdown.hint"))
        p.end()
