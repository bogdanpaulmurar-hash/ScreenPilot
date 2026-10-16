"""Small Qt utilities shared by the UI modules."""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap

from screenpilot.capture.image import CapturedImage
from screenpilot.core.events import Event, EventBus
from screenpilot.i18n import Translator

log = logging.getLogger(__name__)

ACCENT = QColor(0, 120, 212)  # Windows / Fluent accent blue


def qimage_from_captured(image: CapturedImage) -> QImage:
    """Zero-conversion view of BGRA bytes as a QImage (copied so Qt owns the memory)."""
    qimg = QImage(image.bgra, image.width, image.height, image.stride, QImage.Format.Format_RGB32)
    return qimg.copy()


class EventBridge(QObject):
    """Re-emits EventBus events on the GUI thread.

    Bus handlers run on the publisher's thread (capture worker, hotkey thread);
    emitting a Qt signal from there to a QObject living in the GUI thread is
    delivered as a queued call, which is the safe way to touch widgets.
    """

    event = Signal(object)

    def __init__(self, bus: EventBus, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sub = bus.subscribe("*", self._relay)

    def _relay(self, event: Event) -> None:
        self.event.emit(event)

    def close(self) -> None:
        self._sub.cancel()


class Retranslator:
    """Keeps widget texts in sync with the current language (live switching).

    Usage: ``tr.bind(label.setText, "settings.title")``; every bound setter is
    called immediately and again on each language change.
    """

    def __init__(self, translator: Translator) -> None:
        self.translator = translator
        self._bindings: list[tuple[Callable[[str], None], str, dict, Callable[[], dict] | None]] = []
        self._callbacks: list[Callable[[], None]] = []
        self._remove = translator.add_listener(lambda _lang: self.apply())

    def bind(self, setter: Callable[[str], None], key: str, params_fn: Callable[[], dict] | None = None, **params) -> None:
        self._bindings.append((setter, key, params, params_fn))
        self._apply_one(setter, key, params, params_fn)

    def on_change(self, callback: Callable[[], None]) -> None:
        """Run arbitrary refresh code on language change (tables, menus...)."""
        self._callbacks.append(callback)

    def _apply_one(self, setter, key, params, params_fn) -> None:
        values = dict(params)
        if params_fn is not None:
            values.update(params_fn())
        try:
            setter(self.translator.t(key, **values))
        except RuntimeError:  # underlying C++ widget already deleted
            pass

    def apply(self) -> None:
        for binding in list(self._bindings):
            self._apply_one(*binding)
        for callback in list(self._callbacks):
            try:
                callback()
            except RuntimeError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("Retranslate callback failed")

    def dispose(self) -> None:
        self._remove()
        self._bindings.clear()
        self._callbacks.clear()


def app_icon(size: int = 64, badge: QColor | None = None) -> QIcon:
    """The ScreenPilot icon, drawn in code (no image assets to ship or vet)."""
    icon = QIcon()
    for px in (16, 20, 24, 32, 48, size):
        pix = QPixmap(px, px)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        m = px * 0.08
        body = QRectF(m, m, px - 2 * m, px - 2 * m)
        path = QPainterPath()
        path.addRoundedRect(body, px * 0.2, px * 0.2)
        p.fillPath(path, ACCENT)
        # Selection corners
        pen = QPen(QColor("white"), max(1.5, px * 0.09))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        a, b, c = px * 0.28, px * 0.72, px * 0.16
        for (x, y, dx, dy) in ((a, a, 1, 1), (b, a, -1, 1), (a, b, 1, -1), (b, b, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * c, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * c))
        if badge is not None:
            r = px * 0.32
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(badge)
            p.drawEllipse(QRectF(px - r, px - r, r, r))
        p.end()
        icon.addPixmap(pix)
    return icon


def ui_font(point_size: float, bold: bool = False) -> QFont:
    font = QFont("Segoe UI")
    font.setPointSizeF(point_size)
    font.setBold(bold)
    return font
