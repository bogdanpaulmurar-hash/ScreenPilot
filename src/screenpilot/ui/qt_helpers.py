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


def app_icon(size: int = 256, badge: QColor | None = None) -> QIcon:
    """The ScreenPilot icon (tray, taskbar, windows) from :mod:`screenpilot.branding`."""
    from screenpilot.branding import ICO_SIZES, render_icon

    icon = QIcon()
    for px in sorted(set(ICO_SIZES) | {size}):
        rgba = render_icon(px)
        image = QImage(rgba.tobytes("raw", "RGBA"), px, px, px * 4, QImage.Format.Format_RGBA8888).copy()
        pix = QPixmap.fromImage(image)
        if badge is not None:
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
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


def fit_target(widget, image_w: int, image_h: int, margin: float = 0.0) -> QRectF:
    """Target rectangle for drawing a capture 1:1 on high-DPI screens (see ``fit_view``)."""
    from screenpilot.capture.geometry import fit_view

    dpr = widget.devicePixelRatioF() if hasattr(widget, "devicePixelRatioF") else 1.0
    return QRectF(*fit_view(image_w, image_h, widget.width(), widget.height(), dpr=dpr, margin=margin))


def sharp_thumbnail(pixmap: QPixmap, size, dpr: float) -> QPixmap:
    """Scaled pixmap rendered at physical resolution, so thumbnails are crisp at 125 % / 150 %."""
    from PySide6.QtCore import QSize

    dpr = dpr if dpr and dpr > 0 else 1.0
    physical = QSize(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)))
    thumb = pixmap.scaled(physical, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    thumb.setDevicePixelRatio(dpr)
    return thumb


def present(window) -> None:
    """Show a window in front *with keyboard focus* (so Esc and shortcuts reach it).

    Windows refuses the foreground to a process that was not the last one the
    user interacted with; after a global hotkey and a background capture the
    new window would otherwise open behind the active app or without focus.
    """
    window.show()
    window.raise_()
    window.activateWindow()
    try:
        from PySide6.QtCore import QTimer

        from screenpilot.platform.win32_window import force_foreground

        QTimer.singleShot(0, lambda: force_foreground(int(window.winId())) if window.isVisible() else None)
    except RuntimeError:  # the window was deleted meanwhile
        pass


def on_escape(window, handler) -> None:
    """Esc anywhere in ``window`` (even while a child widget has focus) calls ``handler``."""
    from PySide6.QtGui import QKeySequence, QShortcut

    shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), window)
    shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
    shortcut.activated.connect(handler)
    return shortcut
