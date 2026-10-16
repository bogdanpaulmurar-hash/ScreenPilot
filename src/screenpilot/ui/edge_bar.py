"""Edge capture bar (inspired by Snagit's OneClick panel).

A slim handle rests at the top edge of the primary screen. Pointing at it
slides out a compact capture menu (region, window, screen, all screens,
delayed region, recording placeholder, hub, history). It can be dragged along
the edge, is never in your captures (it hides itself first) and can be turned
off in Settings.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QToolButton, QWidget

from screenpilot.ui import icons
from screenpilot.ui.controller import AppController
from screenpilot.ui.qt_helpers import Retranslator
from screenpilot.ui.theme import ui_font
from screenpilot.ui.widgets import on_theme_changed, theme

log = logging.getLogger(__name__)

HANDLE = QSize(132, 12)
COLLAPSE_DELAY_MS = 450
RESUME_POLL_MS = 300
ANIM_MS = 160
BUTTON_W = 64

BUTTONS = (
    # icon, tooltip key / label key, action
    ("region", "hub.mode.region", "capture.region"),
    ("window", "hub.mode.window", "capture.window"),
    ("monitor", "hub.mode.screen", "capture.fullscreen"),
    ("monitors", "hub.mode.all", "capture.all_monitors"),
    ("scroll", "hub.mode.scrolling", "capture.scrolling"),
    ("timer", "edgebar.delay", "delayed"),
    ("video", "edgebar.video", None),
    ("|", None, None),
    ("logo", "edgebar.hub", "hub"),
    ("history", "edgebar.history", "history"),
    ("eye_off", "edgebar.hide", "hide"),
)


class EdgeBar(QWidget):
    def __init__(self, controller: AppController) -> None:
        super().__init__(None)
        self.controller = controller
        self.app = controller.app
        self.t = self.app.translator.t
        self.rt = rt = Retranslator(self.app.translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.expanded = False
        self._drag_from: QPoint | None = None
        self._dragged = False

        self.panel = QFrame(self)
        self.panel.setObjectName("EdgePanel")
        row = QHBoxLayout(self.panel)
        row.setContentsMargins(8, 6, 8, 8)
        row.setSpacing(2)
        self.buttons: dict[str, QToolButton] = {}
        for icon_name, key, action in BUTTONS:
            if icon_name == "|":
                sep = QFrame()
                sep.setObjectName("EdgeSep")
                sep.setFixedSize(1, 40)
                row.addWidget(sep)
                continue
            button = QToolButton()
            button.setProperty("icon_name", icon_name)
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextUnderIcon if action and action.startswith("capture.") or action == "delayed"
                else Qt.ToolButtonStyle.ToolButtonIconOnly
            )
            button.setMinimumWidth(BUTTON_W if button.toolButtonStyle() != Qt.ToolButtonStyle.ToolButtonIconOnly else 40)
            button.setIconSize(QSize(22, 22))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFont(ui_font(8.5))
            if action == "delayed":
                params = lambda: {"seconds": self.app.settings.get("capture.default_delay_seconds")}  # noqa: E731
                rt.bind(button.setToolTip, key, params_fn=params)
                rt.bind(button.setText, "hub.delay", params_fn=params)
            elif action and action.startswith("capture."):
                rt.bind(button.setText, key)
                rt.bind(button.setToolTip, key)
            else:
                rt.bind(button.setToolTip, key)
            if action is None:
                button.setEnabled(False)
            else:
                button.clicked.connect(lambda _=False, a=action: self._activate(a))
            row.addWidget(button)
            self.buttons[action or icon_name] = button
        self.panel.adjustSize()
        self.panel.hide()

        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(self.collapse)
        self._resume_timer = QTimer(self)
        self._resume_timer.setInterval(RESUME_POLL_MS)
        self._resume_timer.timeout.connect(self._try_resume)
        self._anim = QPropertyAnimation(self, b"geometry", self)
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        on_theme_changed(self, self._restyle)
        rt.on_change(self._relayout)
        rt.bind(self.setToolTip, "edgebar.handle")
        self._unsub = self.app.settings.add_listener(self._on_setting)
        self.destroyed.connect(lambda *_: self._unsub())

    # -- geometry -------------------------------------------------------------
    def _screen_rect(self) -> QRect:
        return QGuiApplication.primaryScreen().geometry()

    def _center_x(self) -> int:
        geo = self._screen_rect()
        ratio = float(self.app.settings.get("general.edge_bar_position"))
        return round(geo.x() + min(max(ratio, 0.05), 0.95) * geo.width())

    def _collapsed_rect(self) -> QRect:
        geo = self._screen_rect()
        return QRect(self._center_x() - HANDLE.width() // 2, geo.y(), HANDLE.width(), HANDLE.height())

    def _expanded_rect(self) -> QRect:
        geo = self._screen_rect()
        size = self.panel.sizeHint()
        x = min(max(self._center_x() - size.width() // 2, geo.x()), geo.right() - size.width())
        return QRect(x, geo.y(), size.width(), size.height())

    def _relayout(self) -> None:
        self.panel.adjustSize()
        if self.isVisible():
            self.setGeometry(self._expanded_rect() if self.expanded else self._collapsed_rect())

    # -- show / hide ----------------------------------------------------------
    def start(self) -> None:
        if not self.app.settings.get("general.edge_bar"):
            return
        self.expanded = False
        self.panel.hide()
        self.setGeometry(self._collapsed_rect())
        self.show()

    def expand(self) -> None:
        if self.expanded:
            return
        self.expanded = True
        self.panel.adjustSize()
        self.panel.move(0, 0)
        self.panel.show()
        self._animate(self._expanded_rect())

    def collapse(self) -> None:
        if not self.expanded:
            return
        self.expanded = False
        self.panel.hide()
        self._anim.stop()
        self.setGeometry(self._collapsed_rect())
        self.update()

    def _animate(self, target: QRect) -> None:
        start = QRect(target.x(), target.y(), target.width(), HANDLE.height())
        self._anim.stop()
        self._anim.setStartValue(start)
        self._anim.setEndValue(target)
        self._anim.start()

    def suspend(self) -> None:
        """Hide during a capture; come back when the capture flow is over."""
        self.collapse()
        self.hide()
        self._resume_timer.start()

    def _try_resume(self) -> None:
        if not self.controller.busy:
            self._resume_timer.stop()
            self.start()

    def _on_setting(self, key: str, value) -> None:
        if key == "general.edge_bar":
            if value:
                self.start()
            else:
                self._resume_timer.stop()
                self.hide()
        elif key == "general.edge_bar_position" and self.isVisible() and not self._dragged:
            self._relayout()

    # -- actions --------------------------------------------------------------
    def _activate(self, action: str) -> None:
        if action == "hub":
            self.collapse()
            self.controller.show_hub()
        elif action == "history":
            self.collapse()
            self.controller.show_history()
        elif action == "hide":
            self.app.settings.set("general.edge_bar", False)
            try:
                self.app.settings.save()
            except OSError:
                log.exception("Could not save settings")
        elif action == "delayed":
            self.controller.trigger("capture.delayed", from_menu=True)
        else:
            self.controller.trigger(action, from_menu=True)

    # -- input ----------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._collapse_timer.stop()
        if self._drag_from is None:
            self.expand()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._collapse_timer.start(COLLAPSE_DELAY_MS)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint()
            self._dragged = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_from is None:
            return
        delta = event.globalPosition().toPoint().x() - self._drag_from.x()
        if abs(delta) < 6 and not self._dragged:
            return
        self._dragged = True
        geo = self._screen_rect()
        x = event.globalPosition().toPoint().x()
        self.app.settings.set("general.edge_bar_position", round((x - geo.x()) / max(1, geo.width()), 3))
        self.move(self._expanded_rect().x() if self.expanded else self._collapsed_rect().x(), geo.y())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._dragged:
            try:
                self.app.settings.save()
            except OSError:
                log.exception("Could not save settings")
        self._drag_from = None
        self._dragged = False

    # -- painting -------------------------------------------------------------
    def _restyle(self, t) -> None:
        self.panel.setStyleSheet(
            f"QFrame#EdgePanel {{ background: transparent; }}"
            f" QFrame#EdgeSep {{ background: {t.border_strong}; }}"
            f" QToolButton {{ background: transparent; border: none; border-radius: 8px; padding: 4px 2px; color: {t.text}; }}"
            f" QToolButton:hover {{ background: {t.surface_strong}; }}"
            f" QToolButton:pressed {{ background: {t.accent_subtle}; }}"
            f" QToolButton:disabled {{ color: {t.text_muted}; }}"
        )
        for button in self.buttons.values():
            name = button.property("icon_name")
            color = t.accent if name in ("region", "window", "monitor", "monitors", "scroll", "timer") else t.text_secondary
            button.setIcon(icons.icon(name, color, 22, t.text_muted))
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        if self.expanded:
            path = QPainterPath()
            radius = 12.0
            path.moveTo(r.left(), r.top())
            path.lineTo(r.right(), r.top())
            path.lineTo(r.right(), r.bottom() - radius)
            path.quadTo(r.right(), r.bottom(), r.right() - radius, r.bottom())
            path.lineTo(r.left() + radius, r.bottom())
            path.quadTo(r.left(), r.bottom(), r.left(), r.bottom() - radius)
            path.closeSubpath()
            p.setPen(QPen(QColor(t.border_strong), 1))
            p.setBrush(QColor(t.surface))
            p.drawPath(path)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.accent))
            p.drawRoundedRect(QRectF(r.center().x() - 18, r.top(), 36, 3), 1.5, 1.5)
        else:
            body = QRectF(r.left() + 0.5, r.top() - 6, r.width() - 1, r.height() + 5.5)
            p.setPen(QPen(QColor(t.border_strong), 1))
            p.setBrush(QColor(t.surface))
            p.drawRoundedRect(body, 6, 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.accent))
            p.drawRoundedRect(QRectF(r.center().x() - 20, r.bottom() - 6, 40, 3), 1.5, 1.5)
        p.end()
