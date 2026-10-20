"""Annotation document: shapes on top of a capture, rendered with Pillow.

UI-independent so the same document can be produced by the editor window,
by automation clients (Autopilot) or by the documentation engine.

Shapes are immutable values; edits replace them, which makes undo a simple
list of snapshots. Step numbers are derived from the order of step shapes,
so deleting step 2 renumbers the following steps automatically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from screenpilot.capture.geometry import Point, Rect
from screenpilot.capture.image import CapturedImage

KINDS = ("arrow", "line", "rect", "ellipse", "highlight", "text", "blur", "pixelate", "step", "spotlight")
COLORS = {
    "red": "#d13438",
    "orange": "#f7630c",
    "yellow": "#ffb900",
    "green": "#13a10e",
    "blue": "#0067c0",
    "purple": "#8764b8",
    "black": "#1b1b1b",
    "white": "#ffffff",
}
UNDO_LIMIT = 200


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) != 6:
        return (209, 52, 56)
    return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


@dataclass(frozen=True)
class Shape:
    kind: str
    p1: Point
    p2: Point
    color: str = COLORS["red"]
    width: int = 4
    text: str = ""
    font_size: int = 20
    fill: bool = False  # rect/ellipse filled; text drawn on a callout background
    strength: int = 12  # blur radius / pixel block size

    @property
    def bounds(self) -> Rect:
        return Rect.from_points(self.p1, self.p2)

    def moved(self, dx: int, dy: int) -> "Shape":
        return replace(self, p1=Point(self.p1.x + dx, self.p1.y + dy), p2=Point(self.p2.x + dx, self.p2.y + dy))


@dataclass
class EditorDocument:
    width: int
    height: int
    shapes: list[Shape] = field(default_factory=list)
    crop: Rect | None = None
    step_start: int = 1
    _undo: list[tuple[list[Shape], Rect | None]] = field(default_factory=list, repr=False)
    _redo: list[tuple[list[Shape], Rect | None]] = field(default_factory=list, repr=False)

    # -- history --------------------------------------------------------------
    def _snapshot(self) -> None:
        self._undo.append((list(self.shapes), self.crop))
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append((list(self.shapes), self.crop))
        self.shapes, self.crop = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append((list(self.shapes), self.crop))
        self.shapes, self.crop = self._redo.pop()
        return True

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    # -- editing --------------------------------------------------------------
    def add(self, shape: Shape) -> int:
        if shape.kind not in KINDS:
            raise ValueError(f"Unknown shape kind {shape.kind}")
        self._snapshot()
        self.shapes.append(shape)
        return len(self.shapes) - 1

    def replace(self, index: int, shape: Shape, *, record: bool = True) -> None:
        if record:
            self._snapshot()
        self.shapes[index] = shape

    def remove(self, index: int) -> None:
        self._snapshot()
        del self.shapes[index]

    def bring_to_front(self, index: int) -> int:
        self._snapshot()
        shape = self.shapes.pop(index)
        self.shapes.append(shape)
        return len(self.shapes) - 1

    def set_crop(self, rect: Rect | None) -> None:
        self._snapshot()
        if rect is not None:
            rect = rect.intersect(Rect(0, 0, self.width, self.height))
            if rect.is_empty or rect.width < 4 or rect.height < 4:
                rect = None
        self.crop = rect

    def clear(self) -> None:
        if self.shapes or self.crop:
            self._snapshot()
            self.shapes.clear()
            self.crop = None

    # -- queries --------------------------------------------------------------
    def step_number(self, index: int) -> int:
        n = 0
        for i, shape in enumerate(self.shapes):
            if shape.kind == "step":
                if i == index:
                    return self.step_start + n
                n += 1
        raise ValueError("Not a step")

    def hit(self, p: Point, slack: int = 6) -> int | None:
        """Topmost shape under ``p``."""
        for index in range(len(self.shapes) - 1, -1, -1):
            if _hit_shape(self.shapes[index], p, slack):
                return index
        return None


def _dist_to_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    if dx == dy == 0:
        return math.hypot(p.x - a.x, p.y - a.y)
    t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))


def _hit_shape(s: Shape, p: Point, slack: int) -> bool:
    if s.kind in ("arrow", "line"):
        return _dist_to_segment(p, s.p1, s.p2) <= s.width / 2 + slack
    if s.kind == "step":
        r = step_diameter(s) / 2 + slack
        return (p.x - s.p1.x) ** 2 + (p.y - s.p1.y) ** 2 <= r * r
    if s.kind == "text":
        b = text_bounds(s)
        return b.x - slack <= p.x < b.right + slack and b.y - slack <= p.y < b.bottom + slack
    b = s.bounds
    inside = b.x - slack <= p.x < b.right + slack and b.y - slack <= p.y < b.bottom + slack
    if not inside:
        return False
    if s.kind in ("rect", "ellipse") and not s.fill:
        inner = Rect(b.x + slack + s.width, b.y + slack + s.width, b.width - 2 * (slack + s.width), b.height - 2 * (slack + s.width))
        return inner.is_empty or not inner.contains(p)
    return True


# -- rendering ---------------------------------------------------------------
@lru_cache(maxsize=32)
def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def step_diameter(s: Shape) -> int:
    return max(18, s.font_size + 12)


TEXT_PAD = 8


def text_bounds(s: Shape) -> Rect:
    f = font(s.font_size)
    lines = s.text.split("\n") or [""]
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    widths, height = [], 0
    for line in lines:
        l, t, r, b = probe.textbbox((0, 0), line or " ", font=f)
        widths.append(r - l)
    line_h = round(s.font_size * 1.3)
    height = line_h * len(lines)
    pad = TEXT_PAD if s.fill else 0
    return Rect(s.p1.x - pad, s.p1.y - pad, max(widths) + 2 * pad, height + 2 * pad)


def _arrow_head(a: Point, b: Point, width: int) -> list[tuple[float, float]]:
    angle = math.atan2(b.y - a.y, b.x - a.x)
    length = max(12.0, width * 4.0)
    spread = math.radians(26)
    left = (b.x - length * math.cos(angle - spread), b.y - length * math.sin(angle - spread))
    right = (b.x - length * math.cos(angle + spread), b.y - length * math.sin(angle + spread))
    return [(b.x, b.y), left, right]


def _draw_shape(img: Image.Image, overlay: ImageDraw.ImageDraw, s: Shape, number: int | None) -> None:
    rgb = _rgb(s.color)
    b = s.bounds
    box = (b.x, b.y, b.right - 1, b.bottom - 1)
    if s.kind in ("blur", "pixelate"):
        if b.is_empty:
            return
        region = img.crop((b.x, b.y, b.right, b.bottom))
        if s.kind == "blur":
            region = region.filter(ImageFilter.GaussianBlur(max(2, s.strength)))
        else:
            block = max(4, s.strength)
            small = region.resize((max(1, b.width // block), max(1, b.height // block)), Image.Resampling.BILINEAR)
            region = small.resize(region.size, Image.Resampling.NEAREST)
        img.paste(region, (b.x, b.y))
        return
    if s.kind == "highlight":
        overlay.rectangle(box, fill=rgb + (90,))
        return
    if s.kind == "rect":
        overlay.rectangle(box, outline=rgb + (255,), width=s.width, fill=rgb + (60,) if s.fill else None)
        return
    if s.kind == "ellipse":
        overlay.ellipse(box, outline=rgb + (255,), width=s.width, fill=rgb + (60,) if s.fill else None)
        return
    if s.kind in ("line", "arrow"):
        end = s.p2
        if s.kind == "arrow":
            head = _arrow_head(s.p1, s.p2, s.width)
            # stop the shaft inside the head so the tip stays sharp
            angle = math.atan2(s.p2.y - s.p1.y, s.p2.x - s.p1.x)
            back = max(12.0, s.width * 4.0) * 0.6
            end = Point(round(s.p2.x - back * math.cos(angle)), round(s.p2.y - back * math.sin(angle)))
            overlay.line([(s.p1.x, s.p1.y), (end.x, end.y)], fill=rgb + (255,), width=s.width)
            overlay.polygon(head, fill=rgb + (255,))
        else:
            overlay.line([(s.p1.x, s.p1.y), (end.x, end.y)], fill=rgb + (255,), width=s.width)
        r = s.width / 2
        overlay.ellipse((s.p1.x - r, s.p1.y - r, s.p1.x + r, s.p1.y + r), fill=rgb + (255,))
        return
    if s.kind == "text":
        tb = text_bounds(s)
        if s.fill:
            overlay.rounded_rectangle((tb.x, tb.y, tb.right - 1, tb.bottom - 1), 6, fill=rgb + (235,))
            luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
            text_rgb = (0, 0, 0) if luminance > 160 else (255, 255, 255)
        else:
            text_rgb = rgb
        f = font(s.font_size)
        line_h = round(s.font_size * 1.3)
        for i, line in enumerate(s.text.split("\n")):
            overlay.text((s.p1.x, s.p1.y + i * line_h), line, fill=text_rgb + (255,), font=f)
        return
    if s.kind == "step":
        d = step_diameter(s)
        r = d / 2
        c = s.p1
        overlay.ellipse((c.x - r + 2, c.y - r + 2, c.x + r + 2, c.y + r + 2), fill=(0, 0, 0, 70))
        overlay.ellipse((c.x - r, c.y - r, c.x + r, c.y + r), fill=rgb + (255,), outline=(255, 255, 255, 255), width=max(2, d // 12))
        label = str(number if number is not None else "?")
        overlay.text((c.x, c.y), label, fill=(255, 255, 255, 255), font=font(round(d * (0.55 if len(label) < 2 else 0.45)), True), anchor="mm")


def hit_shape(s: Shape, p: Point, slack: int = 6) -> bool:
    """Is ``p`` on shape ``s``? (public for other engines' editors)."""
    return _hit_shape(s, p, slack)


def draw_shape(img: Image.Image, overlay: ImageDraw.ImageDraw, s: Shape, number: int | None) -> None:
    """Public entry for other engines (recording annotations) that draw editor shapes."""
    _draw_shape(img, overlay, s, number)


def shape_extent(s: Shape) -> Rect:
    """Pixels a vector shape may touch (stroke, arrow head, step shadow, text)."""
    if s.kind == "text":
        t = text_bounds(s)
        return Rect(t.x - 4, t.y - 4, t.width + 8, t.height + 8)
    if s.kind == "step":
        d = step_diameter(s)
        return Rect(s.p1.x - d // 2 - 4, s.p1.y - d // 2 - 4, d + 10, d + 10)
    pad = max(s.width * 4, 12) + 2 if s.kind == "arrow" else s.width + 2
    b = s.bounds
    return Rect(b.x - pad, b.y - pad, b.width + 2 * pad, b.height + 2 * pad)


def render(image: CapturedImage, doc: EditorDocument, *, apply_crop: bool = True, skip: Iterable[int] = ()) -> CapturedImage:
    """Flatten the document onto the image."""
    base = image.to_pil().convert("RGB")
    skip = set(skip)
    # Effects that read pixels (blur, pixelate) apply to the image in order, then vector shapes on top.
    for index, s in enumerate(doc.shapes):
        if index not in skip and s.kind in ("blur", "pixelate"):
            _draw_shape(base, ImageDraw.Draw(base), s, None)
    spot = [s for i, s in enumerate(doc.shapes) if s.kind == "spotlight" and i not in skip]
    canvas = base.convert("RGBA")
    if spot:
        mask = Image.new("L", canvas.size, 140)
        md = ImageDraw.Draw(mask)
        for s in spot:
            b = s.bounds
            md.rounded_rectangle((b.x, b.y, b.right - 1, b.bottom - 1), 8, fill=0)
        dim = Image.new("RGBA", canvas.size, (0, 0, 0, 255))
        dim.putalpha(mask)
        canvas = Image.alpha_composite(canvas, dim)
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for index, s in enumerate(doc.shapes):
        if index in skip or s.kind in ("blur", "pixelate", "spotlight"):
            continue
        number = doc.step_number(index) if s.kind == "step" else None
        _draw_shape(canvas, draw, s, number)
    result = Image.alpha_composite(canvas, overlay).convert("RGB")
    if apply_crop and doc.crop is not None:
        c = doc.crop
        result = result.crop((c.x, c.y, c.right, c.bottom))
    return CapturedImage.from_pil(result)
