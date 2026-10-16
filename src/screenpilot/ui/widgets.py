"""Reusable Fluent-style widgets: toggle switch, cards, setting rows, tiles, chips."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QEnterEvent, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from screenpilot.ui import icons
from screenpilot.ui.theme import LIGHT, Theme, ThemeManager, ui_font

_manager: ThemeManager | None = None


def set_theme_manager(manager: ThemeManager | None) -> None:
    global _manager
    _manager = manager


def theme() -> Theme:
    return _manager.theme if _manager is not None else LIGHT


def on_theme_changed(widget: QWidget, callback: Callable[[Theme], None]) -> None:
    """Call ``callback`` now and whenever the theme changes, while ``widget`` lives."""
    callback(theme())
    if _manager is not None:
        _manager.changed.connect(callback)
        widget.destroyed.connect(lambda *_: _safe_disconnect(callback))


def _safe_disconnect(callback) -> None:
    if _manager is not None:
        try:
            _manager.changed.disconnect(callback)
        except (RuntimeError, TypeError):
            pass


def label(text: str = "", role: str = "body", *, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    return lbl


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None, *, padding: int = 16, spacing: int = 12) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(spacing)


class Divider(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setProperty("divider", True)


class ToggleSwitch(QAbstractButton):
    """Windows 11 style toggle with an animated knob."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, 20)

    def _get_knob(self) -> float:
        return self._pos

    def _set_knob(self, value: float) -> None:
        self._pos = value
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        self._anim.stop()
        self._pos = 1.0 if checked else 0.0
        self.update()

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        on = self.isChecked()
        enabled = self.isEnabled()
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.accent if enabled else t.text_muted))
        else:
            p.setPen(QPen(QColor(t.text_secondary if enabled else t.text_muted), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
        radius = track.height() / 2
        p.drawRoundedRect(track, radius, radius)
        d = track.height() - 8 if not self.underMouse() else track.height() - 6
        x = track.left() + 4 + self._pos * (track.width() - 8 - d)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.on_accent if on else t.text_secondary))
        p.drawEllipse(QRectF(x, track.center().y() - d / 2, d, d))
        p.end()

    def enterEvent(self, event: QEnterEvent) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)


class IconBadge(QLabel):
    """Themed icon in a fixed square (settings rows, headers)."""

    def __init__(self, name: str, size: int = 20, token: str = "text_secondary") -> None:
        super().__init__()
        self.setFixedSize(size + 8, size + 8)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        on_theme_changed(self, lambda t: self.setPixmap(icons.pixmap(name, getattr(t, token), size)))


class SettingRow(QFrame):
    """Card row: icon, title, description on the left, a control on the right."""

    def __init__(self, icon_name: str | None, control: QWidget | None = None) -> None:
        super().__init__()
        self.setProperty("card", True)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(14)
        if icon_name:
            row.addWidget(IconBadge(icon_name), 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = label(role="body")
        self.description = label(role="caption", wrap=True)
        text.addWidget(self.title)
        text.addWidget(self.description)
        row.addLayout(text, 1)
        self.control = control
        if control is not None:
            row.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)


class IconButton(QToolButton):
    """Flat icon button whose glyph follows the theme."""

    def __init__(self, icon_name: str, *, size: int = 18, token: str = "text") -> None:
        super().__init__()
        self.icon_name = icon_name
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(size, size))
        self.setAutoRaise(True)
        on_theme_changed(self, lambda t: self.setIcon(icons.icon(icon_name, getattr(t, token), size, t.text_muted)))


class ModeTile(QAbstractButton):
    """Large capture-mode tile for the Capture Hub (icon, title, shortcut)."""

    def __init__(self, icon_name: str, *, soon: bool = False) -> None:
        super().__init__()
        self.icon_name = icon_name
        self.soon = soon
        self.shortcut_text = ""
        self.setCursor(Qt.CursorShape.ArrowCursor if soon else Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(132, 96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(150, 96)

    def set_shortcut_text(self, text: str) -> None:
        self.shortcut_text = text
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        hovered = self.underMouse() and not self.soon
        pressed = self.isDown()
        fill = t.surface_strong if pressed else (t.accent_subtle if hovered else t.surface)
        border = t.accent if (hovered or self.hasFocus()) and not self.soon else t.border
        pen = QPen(QColor(border), 1.5 if hovered else 1)
        if self.soon:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(QColor(fill))
        p.drawRoundedRect(r, 10, 10)

        icon_color = t.text_muted if self.soon else t.accent
        p.drawPixmap(int(r.left() + 14), int(r.top() + 14), icons.pixmap(self.icon_name, icon_color, 26))
        p.setPen(QColor(t.text_muted if self.soon else t.text))
        p.setFont(ui_font(10.5, weight=QFont.Weight.DemiBold))
        p.drawText(QRectF(r.left() + 14, r.top() + 48, r.width() - 20, 22), Qt.AlignmentFlag.AlignLeft, self.text())
        p.setFont(ui_font(8.5))
        p.setPen(QColor(t.text_muted))
        p.drawText(
            QRectF(r.left() + 14, r.top() + 69, r.width() - 20, 18), Qt.AlignmentFlag.AlignLeft, self.shortcut_text
        )
        p.end()


class Chip(QToolButton):
    """Pill-shaped toggle/option button (delay, destination...)."""

    def __init__(self, icon_name: str) -> None:
        super().__init__()
        self.icon_name = icon_name
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(16, 16))
        on_theme_changed(self, self._restyle)

    def _restyle(self, t: Theme) -> None:
        self.setIcon(icons.icon(self.icon_name, t.text_secondary, 16, t.text_muted))
        self.setStyleSheet(
            f"QToolButton {{ border: 1px solid {t.border_strong}; border-radius: 14px; padding: 4px 12px 4px 8px;"
            f" background: {t.surface}; color: {t.text_secondary}; }}"
            f" QToolButton:hover {{ background: {t.surface_alt}; }}"
            f" QToolButton:checked {{ background: {t.accent_subtle}; border-color: {t.accent}; color: {t.text}; }}"
            f" QToolButton:disabled {{ color: {t.text_muted}; border-style: dashed; }}"
        )

