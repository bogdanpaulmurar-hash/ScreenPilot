"""Per-frame effects for recordings: cursor highlight, click ripples, zoom.

Frames are raw top-down BGRA bytes (as produced by GDI). To stay fast, the
highlight and ripples only touch a small patch around the cursor instead of
the whole frame; zoom crops and rescales the full frame (about 10 ms at
1080p) and is only active while the user turns it on.

Colours are given as ``(r, g, b, a)``. Because frames are handled as BGRA
buffers wrapped in a Pillow "RGBA" image, colours are swapped to BGR order
internally (:func:`_bgr`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image, ImageDraw

HIGHLIGHT_RADIUS = 26
HIGHLIGHT_COLOR = (255, 214, 0, 80)
LEFT_CLICK_COLOR = (0, 120, 212, 230)
RIGHT_CLICK_COLOR = (209, 52, 56, 230)
RIPPLE_SECONDS = 0.45
RIPPLE_MIN = 8
RIPPLE_MAX = 34
ZOOM_EASE = 0.18  # fraction of the remaining distance covered per frame
PATCH = RIPPLE_MAX + 6


def _bgr(color: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    r, g, b, a = color
    return (b, g, r, a)


def _draw_patch(buf: bytearray, width: int, height: int, center: tuple[int, int], painter) -> None:
    """Composite an overlay onto a small square around ``center``, in place.

    Only the patch rows are copied out and back, so the cost does not depend
    on the frame size (about 0.3 ms instead of ~10 ms for a 1080p frame).
    """
    x0, y0 = max(0, center[0] - PATCH), max(0, center[1] - PATCH)
    x1, y1 = min(width, center[0] + PATCH), min(height, center[1] + PATCH)
    if x1 <= x0 or y1 <= y0:
        return
    pw, ph = x1 - x0, y1 - y0
    row = pw * 4
    rows = b"".join(bytes(buf[(y * width + x0) * 4 : (y * width + x1) * 4]) for y in range(y0, y1))
    patch = Image.frombytes("RGBA", (pw, ph), rows)
    patch.putalpha(255)  # GDI leaves alpha undefined; compositing needs an opaque base
    overlay = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    painter(ImageDraw.Draw(overlay), center[0] - x0, center[1] - y0)
    out = Image.alpha_composite(patch, overlay).tobytes()
    for i, y in enumerate(range(y0, y1)):
        start = (y * width + x0) * 4
        buf[start : start + row] = out[i * row : (i + 1) * row]


@dataclass
class Ripple:
    x: int
    y: int
    start: float
    right: bool = False


@dataclass
class EffectOptions:
    highlight: bool = True
    clicks: bool = True
    zoom_factor: float = 2.0


@dataclass
class EffectRenderer:
    """Stateful renderer: tracks button transitions, ripples and zoom animation."""

    options: EffectOptions = field(default_factory=EffectOptions)
    zoom_on: bool = False
    _ripples: list[Ripple] = field(default_factory=list)
    _buttons: tuple[bool, bool] = (False, False)
    _zoom: float = 1.0
    _center: tuple[float, float] | None = None

    # -- state ------------------------------------------------------------------
    def toggle_zoom(self) -> bool:
        self.zoom_on = not self.zoom_on
        return self.zoom_on

    @property
    def active(self) -> bool:
        return self.options.highlight or self.options.clicks or self.zoom_on or self._zoom > 1.001

    def feed_buttons(self, buttons: tuple[bool, bool], cursor: tuple[int, int] | None, now: float) -> None:
        """Start a ripple on each button press (rising edge)."""
        left, right = buttons
        if cursor is not None and self.options.clicks:
            if left and not self._buttons[0]:
                self._ripples.append(Ripple(cursor[0], cursor[1], now, right=False))
            if right and not self._buttons[1]:
                self._ripples.append(Ripple(cursor[0], cursor[1], now, right=True))
        self._buttons = (left, right)
        self._ripples = [r for r in self._ripples if now - r.start < RIPPLE_SECONDS]

    # -- rendering ----------------------------------------------------------------
    def apply(
        self,
        frame: bytes,
        width: int,
        height: int,
        cursor: tuple[int, int] | None,
        now: float,
    ) -> bytes:
        """Return the frame with effects; ``cursor`` is relative to the frame."""
        if not self.active:
            return frame
        buf = bytearray(frame)
        inside = cursor is not None and 0 <= cursor[0] < width and 0 <= cursor[1] < height
        if inside and self.options.highlight:
            _draw_patch(buf, width, height, cursor, self._highlight)
        for ripple in self._ripples:
            progress = (now - ripple.start) / RIPPLE_SECONDS
            if 0 <= progress < 1:
                _draw_patch(
                    buf, width, height, (ripple.x, ripple.y),
                    lambda d, cx, cy, r=ripple, p=progress: self._ripple(d, cx, cy, r, p),
                )
        target = self.options.zoom_factor if self.zoom_on else 1.0
        if target > 1.0 or self._zoom > 1.001:
            image = Image.frombuffer("RGBA", (width, height), bytes(buf), "raw", "RGBA", 0, 1)
            return self._apply_zoom(image, cursor if inside else None).tobytes()
        return bytes(buf)

    @staticmethod
    def _highlight(draw: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
        r = HIGHLIGHT_RADIUS
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_bgr(HIGHLIGHT_COLOR))

    @staticmethod
    def _ripple(draw: ImageDraw.ImageDraw, cx: int, cy: int, ripple: Ripple, progress: float) -> None:
        radius = ripple_radius(progress)
        r, g, b, a = RIGHT_CLICK_COLOR if ripple.right else LEFT_CLICK_COLOR
        alpha = int(a * (1 - progress))
        width = max(2, int(4 * (1 - progress)) + 1)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(b, g, r, alpha), width=width)

    def _apply_zoom(self, image: Image.Image, cursor: tuple[int, int] | None) -> Image.Image:
        target = self.options.zoom_factor if self.zoom_on else 1.0
        self._zoom += (target - self._zoom) * ZOOM_EASE
        if abs(self._zoom - target) < 0.01:
            self._zoom = target
        if self._zoom <= 1.001:
            self._center = None
            return image
        w, h = image.size
        goal = cursor if cursor is not None else (self._center or (w / 2, h / 2))
        if self._center is None:
            self._center = (float(goal[0]), float(goal[1]))
        else:
            cx, cy = self._center
            self._center = (cx + (goal[0] - cx) * ZOOM_EASE, cy + (goal[1] - cy) * ZOOM_EASE)
        view_w, view_h = w / self._zoom, h / self._zoom
        left = min(max(self._center[0] - view_w / 2, 0), w - view_w)
        top = min(max(self._center[1] - view_h / 2, 0), h - view_h)
        box = (left, top, left + view_w, top + view_h)
        return image.resize((w, h), Image.Resampling.BILINEAR, box=box)

    @property
    def zoom(self) -> float:
        return self._zoom


def ripple_radius(progress: float) -> float:
    """Exposed for tests and UI previews."""
    eased = 1 - (1 - max(0.0, min(1.0, progress))) ** 2
    return RIPPLE_MIN + (RIPPLE_MAX - RIPPLE_MIN) * eased


__all__ = ["EffectOptions", "EffectRenderer", "Ripple", "ripple_radius"]
