"""ScreenPilot line icons (original artwork, 24×24, 1.75 px stroke).

Kept as inline SVG so there are no image assets or third-party icon packs to
ship and license-review. Colour is applied at render time, so the same icon
works in light mode, dark mode and on accent backgrounds.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PATHS: dict[str, str] = {
    "region": '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><path d="M9 12h6M12 9v6"/>',
    "window": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 9h18"/><path d="M6 7h.01M8.5 7h.01"/>',
    "monitor": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    "monitors": '<rect x="2" y="6" width="12" height="9" rx="1.5"/><rect x="10" y="3" width="12" height="9" rx="1.5"/><path d="M6 19h4M8 15v4"/>',
    "scroll": '<rect x="6" y="3" width="12" height="18" rx="2"/><path d="M12 7v10M9.5 9.5 12 7l2.5 2.5M9.5 14.5 12 17l2.5-2.5"/>',
    "video": '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="m16 10 5-3v10l-5-3"/>',
    "timer": '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 1.5M10 2h4"/>',
    "cursor": '<path d="M6 3l12 7-5.5 1.5L10 17z"/><path d="m13 12 4 6"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    "save": '<path d="M5 4h11l3 3v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/>',
    "save_as": '<path d="M5 4h11l3 3v4M12 20H6a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1"/><path d="M8 4v5h7V4"/><path d="m15 20 6-6-2-2-6 6v2z"/>',
    "edit": '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    "pin": '<path d="M15 4l5 5-3 1-3 3v4l-2 2-8-8 2-2h4l3-3z"/><path d="m4 20 5-5"/>',
    "ocr": '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><path d="M8 9h8M12 9v7"/>',
    "close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "history": '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3 4v4h4M12 8v4l3 2"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v2.2M12 19.3v2.2M21.5 12h-2.2M4.7 12H2.5M18.7 5.3l-1.6 1.6M6.9 17.1l-1.6 1.6M18.7 18.7l-1.6-1.6M6.9 6.9 5.3 5.3"/>',
    "keyboard": '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M9 10h.01M12 10h.01M15 10h.01M18 10h.01M7 14h10"/>',
    "language": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="9.5" r="1.5"/><path d="m21 16-5-5-9 9"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.3-4.3"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-12M9 7V4h6v3"/>',
    "sparkle": '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/>',
    "theme": '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    "chevron_down": '<path d="m6 9 6 6 6-6"/>',
    "eye_off": '<path d="M3 3l18 18M10.6 6.1A10 10 0 0 1 12 6c5 0 8.5 4.5 9.5 6a17 17 0 0 1-3.2 3.8M6.6 6.6A17 17 0 0 0 2.5 12c1 1.5 4.5 6 9.5 6a9.6 9.6 0 0 0 4.3-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "steps": '<circle cx="6" cy="6.5" r="2.5"/><circle cx="6" cy="17.5" r="2.5"/><path d="M6 9v6M11 6.5h9M11 17.5h9M11 12h6"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "select": '<path d="M5 3l14 7.5-6.2 1.6L10 18z"/>',
    "arrow": '<path d="M5 19 19 5M10 5h9v9"/>',
    "line": '<path d="M5 19 19 5"/>',
    "rect": '<rect x="4" y="6" width="16" height="12" rx="1.5"/>',
    "ellipse": '<ellipse cx="12" cy="12" rx="8.5" ry="6.5"/>',
    "highlight": '<path d="m9 15-3 3h6l1.5-1.5"/><path d="M14 4l6 6-7.5 7.5-6-6z"/><path d="M4 21h16"/>',
    "text": '<path d="M5 6V4h14v2M12 4v16M9 20h6"/>',
    "blur": '<circle cx="7" cy="7" r="1.5"/><circle cx="12" cy="7" r="1.5"/><circle cx="17" cy="7" r="1.5"/><circle cx="7" cy="12" r="1.5"/><circle cx="12" cy="12" r="2.2"/><circle cx="17" cy="12" r="1.5"/><circle cx="7" cy="17" r="1.5"/><circle cx="12" cy="17" r="1.5"/><circle cx="17" cy="17" r="1.5"/>',
    "pixelate": '<path d="M4 4h5v5H4zM9 9h6v6H9zM15 4h5v5h-5zM4 15h5v5H4zM15 15h5v5h-5z"/>',
    "crop": '<path d="M6 2v16h16M2 6h16v16"/>',
    "spotlight": '<circle cx="12" cy="12" r="5"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "redo": '<path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/>',
    "procedure": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 3v2h6V3M8.5 10h7M8.5 14h7M8.5 18h4"/>',
    "record": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/>',
    "up": '<path d="m6 14 6-6 6 6"/>',
    "down": '<path d="m6 10 6 6 6-6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "shield": '<path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/><path d="m9 12 2 2 4-4"/>',
    "general": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "workflow": '<rect x="3" y="4" width="6" height="6" rx="1.5"/><rect x="15" y="14" width="6" height="6" rx="1.5"/><path d="M6 10v4a3 3 0 0 0 3 3h6"/><path d="m13 15 2 2-2 2"/>',
    "logo": '<rect x="2.5" y="2.5" width="19" height="19" rx="5"/><path d="M7 10V8a1 1 0 0 1 1-1h2M14 7h2a1 1 0 0 1 1 1v2M17 14v2a1 1 0 0 1-1 1h-2M10 17H8a1 1 0 0 1-1-1v-2"/>',
}

NAMES = tuple(_PATHS)


def svg(name: str, color: str, stroke: float = 1.75) -> bytes:
    body = _PATHS[name]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    ).encode()


@lru_cache(maxsize=512)
def _pixmap(name: str, color: str, size: int, dpr: float) -> QPixmap:
    px = max(1, round(size * dpr))
    pix = QPixmap(px, px)
    pix.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(svg(name, color)))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pix.setDevicePixelRatio(dpr)
    return pix


def pixmap(name: str, color: QColor | str, size: int = 20, dpr: float = 2.0) -> QPixmap:
    c = color.name(QColor.NameFormat.HexRgb) if isinstance(color, QColor) else color
    return _pixmap(name, c, size, dpr)


def icon(name: str, color: QColor | str, size: int = 20, disabled: QColor | str | None = None) -> QIcon:
    result = QIcon()
    result.addPixmap(pixmap(name, color, size), QIcon.Mode.Normal)
    if disabled is not None:
        result.addPixmap(pixmap(name, disabled, size), QIcon.Mode.Disabled)
    return result


def icon_size(size: int = 20) -> QSize:
    return QSize(size, size)
