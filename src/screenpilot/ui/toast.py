"""Small notification card, used when the tray icon is hidden (DevPilot owns the tray).

Windows only shows balloon / toast messages for a visible tray icon, so
without one ScreenPilot shows its own card in the bottom-right corner of the
screen under the mouse. Clicking it runs the optional action.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QCursor, QMouseEvent
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from screenpilot.ui.widgets import theme

TOAST_MS = 4500
MARGIN = 16


class Toast(QWidget):
    def __init__(self, title: str, body: str, *, error: bool = False, action: Callable[[], None] | None = None) -> None:
        super().__init__(None)
        self.action = action
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        t = theme()
        card = QFrame(self)
        card.setObjectName("Toast")
        accent = t.danger if error else t.accent
        card.setStyleSheet(
            f"QFrame#Toast {{ background: {t.surface}; border: 1px solid {t.border_strong};"
            f" border-left: 4px solid {accent}; border-radius: 8px; }}"
            f" QLabel {{ color: {t.text}; background: transparent; border: none; }}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        heading = QLabel(title)
        heading.setStyleSheet("font-weight: 600;")
        text = QLabel(body)
        text.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(text)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        self.setFixedWidth(340)
        if action is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        QTimer.singleShot(TOAST_MS, self.close)

    def show_in_corner(self) -> None:
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - MARGIN, area.bottom() - self.height() - MARGIN)
        self.show()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        action, self.action = self.action, None
        self.close()
        if action is not None:
            action()
