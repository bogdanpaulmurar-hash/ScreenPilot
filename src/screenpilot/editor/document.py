"""Annotation document: shapes on top of a capture, rendered with Pillow.

UI-independent so the same document can be produced by the editor window,
by automation clients (Autopilot) or by the documentation engine.

Shapes are immutable values; edits replace them, which makes undo a simple
list of snapshots. Step numbers are derived from the order of step shapes,
so deleting step 2 renumbers the following steps automatically.

Vector shapes (arrows, lines, outlines, text, steps, symbols) are drawn
supersampled and scaled down, so edges are smooth instead of stair-stepped.
Pictures placed on top ("image" shapes) live in ``EditorDocument.images`` and
are referenced by id, so undo snapshots stay cheap.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from screenpilot.capture.geometry import Point, Rect
from screenpilot.capture.image import CapturedImage

KINDS = ("arrow", "line", "rect", "ellipse", "highlight", "text", "blur", "pixelate", "step", "spotlight", "image", "emoji")
BOX_KINDS = ("rect", "ellipse", "highlight", "blur", "pixelate", "spotlight", "image")
# Shapes that may sit beside the capture: the canvas grows (white) to fit them.
# (Not outlines and step badges: snapped to an element at the very edge they would add a white sliver.)
EXTENDING_KINDS = ("image", "text", "emoji", "arrow", "line")
CANVAS_BACKGROUND = (255, 255, 255)
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
# Full colour picker: vivid, deep, light and neutral rows.
PALETTE = (
    "#d13438", "#f7630c", "#ffb900", "#13a10e", "#00b7c3", "#0067c0", "#8764b8", "#e3008c",
    "#a4262c", "#ca5010", "#986f0b", "#107c10", "#038387", "#003e92", "#5c2e91", "#9b0062",
    "#f1707b", "#f7b26a", "#ffe08a", "#9fd89f", "#8be2e9", "#8ac5ff", "#c6b1de", "#f4a6d7",
    "#ffffff", "#e1e1e1", "#bdbdbd", "#8a8a8a", "#5c5c5c", "#3b3b3b", "#1b1b1b", "#000000",
)
# Symbols for instructions, grouped for the picker.
SYMBOLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("status", ("✅", "❌", "⚠️", "❗", "❓", "ℹ️", "🚫", "⛔", "✔️", "✖️", "🆗", "🆕")),
    ("pointers", ("👉", "👈", "👆", "👇", "☝️", "👍", "👎", "👌", "✋", "🖱️", "⌨️", "🖐️")),
    ("arrows", ("➡️", "⬅️", "⬆️", "⬇️", "↗️", "↘️", "↙️", "↖️", "🔄", "↩️", "↪️", "🔃")),
    ("numbers", ("❶", "❷", "❸", "❹", "❺", "❻", "❼", "❽", "❾", "❿", "Ⓐ", "Ⓑ")),
    ("objects", ("💡", "🔒", "🔓", "🔑", "📌", "📎", "🔍", "⚙️", "📁", "📄", "📧", "📞")),
    ("marks", ("⭐", "🔥", "🎯", "🏁", "⏱️", "⏳", "🔔", "💬", "🗨️", "❤️", "🟢", "🔴")),
)
UNDO_LIMIT = 200
SUPERSAMPLE = 3
MAX_SUPERSAMPLED_PIXELS = 12_000_000


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) != 6:
        return (209, 52, 56)
    try:
        return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (209, 52, 56)


def _contrast(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return (0, 0, 0) if luminance > 160 else (255, 255, 255)


@dataclass(frozen=True)
class Shape:
    kind: str
    p1: Point
    p2: Point
    color: str = COLORS["red"]
    width: int = 4
    text: str = ""
    font_size: int = 20
    fill: bool = False  # rect/ellipse filled; text on a callout; image framed
    strength: int = 12  # blur radius / pixel block size
    ref: str = ""  # image shapes: key into EditorDocument.images

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
    images: dict[str, Image.Image] = field(default_factory=dict, repr=False)
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
        if shape.kind == "image" and shape.ref not in self.images:
            raise ValueError("Image shape refers to an unknown picture")
        self._snapshot()
        self.shapes.append(shape)
        return len(self.shapes) - 1

    def add_image(self, picture: CapturedImage | Image.Image, at: Point, *, max_size: tuple[int, int] | None = None, **style) -> int:
        """Place a picture centred on ``at``; it is scaled down to fit ``max_size`` (default: 60% of the canvas)."""
        pil = picture.to_pil() if isinstance(picture, CapturedImage) else picture
        pil = pil.convert("RGBA")
        ref = uuid.uuid4().hex
        self.images[ref] = pil
        mw, mh = max_size or (max(16, round(self.width * 0.6)), max(16, round(self.height * 0.6)))
        scale = min(1.0, mw / pil.width, mh / pil.height)
        w, h = max(8, round(pil.width * scale)), max(8, round(pil.height * scale))
        p1 = Point(at.x - w // 2, at.y - h // 2)
        return self.add(Shape("image", p1, Point(p1.x + w - 1, p1.y + h - 1), ref=ref, **style))  # p2 is inclusive

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

    def send_to_back(self, index: int) -> int:
        self._snapshot()
        shape = self.shapes.pop(index)
        self.shapes.insert(0, shape)
        return 0

    def set_crop(self, rect: Rect | None) -> None:
        self._snapshot()
        if rect is not None:
            rect = rect.intersect(self.extent())
            if rect.is_empty or rect.width < 4 or rect.height < 4:
                rect = None
        self.crop = rect

    def clear(self) -> None:
        if self.shapes or self.crop:
            self._snapshot()
            self.shapes.clear()
            self.crop = None

    def copy_with(self, shapes: list[Shape]) -> "EditorDocument":
        """Same canvas and pictures, other shapes (live preview of a shape being drawn)."""
        return EditorDocument(self.width, self.height, shapes, None, self.step_start, self.images)

    # -- queries --------------------------------------------------------------
    def extent(self, shapes: Iterable[Shape] | None = None) -> Rect:
        """The canvas: the capture plus whatever pictures, labels and arrows were placed beside it.

        Coordinates stay those of the capture (a picture placed above it has negative y)."""
        area = Rect(0, 0, self.width, self.height)
        for s in self.shapes if shapes is None else shapes:
            if s.kind in EXTENDING_KINDS:
                b = shape_bounds(s)
                if b.width > 0 and b.height > 0:
                    area = area.union(b)
        return area

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
    if s.kind in ("text", "emoji"):
        b = shape_bounds(s)
        return b.x - slack <= p.x < b.right + slack and b.y - slack <= p.y < b.bottom + slack
    b = s.bounds
    inside = b.x - slack <= p.x < b.right + slack and b.y - slack <= p.y < b.bottom + slack
    if not inside:
        return False
    if s.kind in ("rect", "ellipse") and not s.fill:
        inner = Rect(b.x + slack + s.width, b.y + slack + s.width, b.width - 2 * (slack + s.width), b.height - 2 * (slack + s.width))
        return inner.is_empty or not inner.contains(p)
    return True


# -- fonts and metrics ---------------------------------------------------------
@lru_cache(maxsize=64)
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


SYMBOL_FONTS = ("seguiemj.ttf", "seguisym.ttf", "NotoColorEmoji.ttf", "DejaVuSans.ttf")
_MISSING = "\U000ffffd"  # a code point no font has: renders as the "missing glyph" box


@lru_cache(maxsize=64)
def _truetype(name: str, size: int) -> ImageFont.FreeTypeFont | None:
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return None


def _glyph_pixels(f: ImageFont.FreeTypeFont, ch: str) -> bytes:
    canvas = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((24, 24), ch, font=f, anchor="mm", embedded_color=True, fill=(0, 0, 0, 255))
    return canvas.tobytes()


@lru_cache(maxsize=512)
def _has_glyphs(name: str, text: str) -> bool:
    f = _truetype(name, 32)
    if f is None:
        return False
    missing = _glyph_pixels(f, _MISSING)
    return all(_glyph_pixels(f, ch) != missing for ch in text if not ch.isspace())


def _symbol_text(text: str) -> str:
    # Variation selectors ask for emoji presentation; the colour font already does that and Pillow would
    # otherwise give them an advance width that pushes the symbol off-centre.
    return (text or "?").replace("️", "").replace("︎", "")


def emoji_font(size: int, text: str = "") -> ImageFont.ImageFont:
    """The first symbol font that has every character (colour emoji first, then Segoe UI Symbol)."""
    base = _symbol_text(text)
    for name in SYMBOL_FONTS:
        if _truetype(name, size) is not None and (not text or _has_glyphs(name, base)):
            return _truetype(name, size)
    for name in SYMBOL_FONTS:
        if _truetype(name, size) is not None:
            return _truetype(name, size)
    return font(size)


def step_diameter(s: Shape) -> int:
    return max(18, s.font_size + 12)


TEXT_PAD = 8
_probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))


def text_bounds(s: Shape) -> Rect:
    f = font(s.font_size)
    lines = s.text.split("\n") or [""]
    widths = []
    for line in lines:
        l, _t, r, _b = _probe.textbbox((0, 0), line or " ", font=f)
        widths.append(r - l)
    line_h = round(s.font_size * 1.3)
    height = line_h * len(lines)
    pad = TEXT_PAD if s.fill else 0
    return Rect(s.p1.x - pad, s.p1.y - pad, max(widths) + 2 * pad, height + 2 * pad)


def emoji_bounds(s: Shape) -> Rect:
    """Symbols are centred on p1, so a click places the symbol right under the cursor."""
    text = _symbol_text(s.text)
    l, t, r, b = _probe.textbbox((s.p1.x, s.p1.y), text, font=emoji_font(s.font_size, text), anchor="mm", embedded_color=True)
    return Rect(math.floor(l), math.floor(t), max(1, math.ceil(r - l)), max(1, math.ceil(b - t)))


def shape_bounds(s: Shape) -> Rect:
    """Visible extent of a shape (selection frame, hit testing)."""
    if s.kind == "text":
        return text_bounds(s)
    if s.kind == "emoji":
        return emoji_bounds(s)
    if s.kind == "step":
        d = step_diameter(s)
        return Rect(s.p1.x - d // 2, s.p1.y - d // 2, d, d)
    return s.bounds


def _paint_extent(s: Shape) -> Rect:
    """Everything a vector shape may paint, including arrow heads, shadows and text strokes."""
    if s.kind in ("text", "emoji", "step"):
        b = shape_bounds(s)
        pad = 4 + s.font_size // 6
    else:
        b = s.bounds
        pad = s.width * 5 + 14 if s.kind == "arrow" else s.width + 3
    return Rect(b.x - pad, b.y - pad, b.width + 2 * pad, b.height + 2 * pad)


# -- drawing -------------------------------------------------------------------
class _Space:
    """Maps image coordinates into a supersampled layer that starts at (ox, oy)."""

    def __init__(self, ox: int, oy: int, k: int) -> None:
        self.ox, self.oy, self.k = ox, oy, k

    def x(self, v: float) -> float:
        return (v - self.ox) * self.k

    def y(self, v: float) -> float:
        return (v - self.oy) * self.k

    def pt(self, p: Point) -> tuple[float, float]:
        return (self.x(p.x), self.y(p.y))

    def box(self, b: Rect) -> tuple[float, float, float, float]:
        return (self.x(b.x), self.y(b.y), self.x(b.right) - 1, self.y(b.bottom) - 1)


def _arrow_geometry(a: Point, b: Point, width: int) -> tuple[list[tuple[float, float]], tuple[float, float]]:
    """Head polygon (tip, wing, notch, wing) and the point where the shaft should end."""
    angle = math.atan2(b.y - a.y, b.x - a.x)
    shaft = math.hypot(b.x - a.x, b.y - a.y)
    length = min(max(14.0, width * 3.6 + 6), max(8.0, shaft * 0.9))
    spread = math.radians(24)
    cos, sin = math.cos, math.sin
    left = (b.x - length * cos(angle - spread), b.y - length * sin(angle - spread))
    right = (b.x - length * cos(angle + spread), b.y - length * sin(angle + spread))
    notch_len = length * 0.78
    notch = (b.x - notch_len * cos(angle), b.y - notch_len * sin(angle))
    end_len = length * 0.62
    end = (b.x - end_len * cos(angle), b.y - end_len * sin(angle))
    return [(b.x, b.y), left, notch, right], end


def _draw_vector(draw: ImageDraw.ImageDraw, s: Shape, number: int | None, sp: _Space) -> None:
    rgb = _rgb(s.color)
    k = sp.k
    solid = rgb + (255,)
    if s.kind == "highlight":
        draw.rounded_rectangle(sp.box(s.bounds), 3 * k, fill=rgb + (90,))
        return
    if s.kind in ("rect", "ellipse"):
        box = sp.box(s.bounds)
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            return
        fill = rgb + (60,) if s.fill else None
        if s.kind == "rect":
            radius = min(max(2.0, s.width * 0.75) * k, (box[2] - box[0]) / 2, (box[3] - box[1]) / 2)
            draw.rounded_rectangle(box, radius, outline=solid, width=s.width * k, fill=fill)
        else:
            draw.ellipse(box, outline=solid, width=s.width * k, fill=fill)
        return
    if s.kind in ("line", "arrow"):
        w = s.width * k
        start = sp.pt(s.p1)
        if s.kind == "arrow" and s.p1 != s.p2:
            head, end = _arrow_geometry(s.p1, s.p2, s.width)
            end_pt = (sp.x(end[0]), sp.y(end[1]))
            draw.line([start, end_pt], fill=solid, width=w)
            draw.polygon([(sp.x(x), sp.y(y)) for x, y in head], fill=solid)
        else:
            end_pt = sp.pt(s.p2)
            draw.line([start, end_pt], fill=solid, width=w)
            r = w / 2
            draw.ellipse((end_pt[0] - r, end_pt[1] - r, end_pt[0] + r, end_pt[1] + r), fill=solid)
        r = w / 2
        draw.ellipse((start[0] - r, start[1] - r, start[0] + r, start[1] + r), fill=solid)
        return
    if s.kind == "text":
        f = font(s.font_size * k)
        line_h = round(s.font_size * 1.3) * k
        x0, y0 = sp.pt(s.p1)
        if s.fill:
            tb = text_bounds(s)
            draw.rounded_rectangle(sp.box(tb), 6 * k, fill=rgb + (240,))
            text_rgb, stroke, stroke_rgb = _contrast(rgb), 0, None
        else:
            # a thin contrasting outline keeps plain text readable on any background
            text_rgb, stroke, stroke_rgb = rgb, max(1, s.font_size // 16) * k, _contrast(rgb) + (200,)
        for i, line in enumerate(s.text.split("\n")):
            draw.text((x0, y0 + i * line_h), line, fill=text_rgb + (255,), font=f, stroke_width=stroke, stroke_fill=stroke_rgb)
        return
    if s.kind == "emoji":
        text = _symbol_text(s.text)
        draw.text(sp.pt(s.p1), text, fill=solid, font=emoji_font(s.font_size * k, text), anchor="mm", embedded_color=True)
        return
    if s.kind == "step":
        d = step_diameter(s)
        r = d / 2 * k
        cx, cy = sp.pt(s.p1)
        off = 1.5 * k
        draw.ellipse((cx - r + off, cy - r + off * 1.4, cx + r + off, cy + r + off * 1.4), fill=(0, 0, 0, 60))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=solid, outline=(255, 255, 255, 255), width=max(2, d // 12) * k)
        label = str(number if number is not None else "?")
        size = round(d * (0.55 if len(label) < 2 else 0.45)) * k
        draw.text((cx, cy), label, fill=_contrast(rgb) + (255,), font=font(size, True), anchor="mm")


def _composite_vectors(canvas: Image.Image, batch: list[tuple[Shape, int | None]]) -> None:
    """Draw a run of vector shapes supersampled, scale down (smooth edges) and paste."""
    if not batch:
        return
    area = Rect(0, 0, canvas.width, canvas.height)
    x0, y0, x1, y1 = canvas.width, canvas.height, 0, 0
    for s, _n in batch:
        e = _paint_extent(s).intersect(area)
        if e.is_empty:
            continue
        x0, y0, x1, y1 = min(x0, e.x), min(y0, e.y), max(x1, e.right), max(y1, e.bottom)
    if x1 <= x0 or y1 <= y0:
        return
    w, h = x1 - x0, y1 - y0
    k = SUPERSAMPLE
    while k > 1 and w * h * k * k > MAX_SUPERSAMPLED_PIXELS:
        k -= 1
    layer = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    sp = _Space(x0, y0, k)
    for s, number in batch:
        _draw_vector(draw, s, number, sp)
    if k > 1:
        layer = layer.reduce(k)
    canvas.paste(layer, (x0, y0), layer)


@lru_cache(maxsize=16)
def _shadow(w: int, h: int, radius: int) -> Image.Image:
    pad = radius * 3
    mask = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, pad + w - 1, pad + h - 1), 4, fill=90)
    mask = mask.filter(ImageFilter.GaussianBlur(radius))
    shadow = Image.new("RGBA", mask.size, (0, 0, 0, 255))
    shadow.putalpha(mask)
    return shadow


def _paste_picture(canvas: Image.Image, doc: EditorDocument, s: Shape) -> None:
    picture = doc.images.get(s.ref)
    b = s.bounds
    if picture is None or b.width < 2 or b.height < 2:
        return
    scaled = picture if picture.size == (b.width, b.height) else picture.resize((b.width, b.height), Image.Resampling.LANCZOS)
    shadow = _shadow(b.width, b.height, 6)
    pad = (shadow.width - b.width) // 2
    canvas.paste(shadow, (b.x - pad, b.y - pad + 3), shadow)
    canvas.paste(scaled, (b.x, b.y), scaled)
    if s.fill:  # coloured frame
        frame = Shape("rect", Point(b.x - s.width, b.y - s.width), Point(b.right + s.width, b.bottom + s.width), color=s.color, width=s.width)
        _composite_vectors(canvas, [(frame, None)])
    else:  # hairline so pasted screenshots stand out from the one below
        edge = Image.new("RGBA", (b.width, b.height), (0, 0, 0, 0))
        ImageDraw.Draw(edge).rectangle((0, 0, b.width - 1, b.height - 1), outline=(0, 0, 0, 60), width=1)
        canvas.paste(edge, (b.x, b.y), edge)


def _apply_effect(img: Image.Image, s: Shape) -> None:
    b = s.bounds.intersect(Rect(0, 0, img.width, img.height))
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


def _spotlight(canvas: Image.Image, spots: list[Shape]) -> Image.Image:
    mask = Image.new("L", canvas.size, 140)
    md = ImageDraw.Draw(mask)
    for s in spots:
        b = s.bounds
        md.rounded_rectangle((b.x, b.y, b.right - 1, b.bottom - 1), 8, fill=0)
    dim = Image.new("RGBA", canvas.size, (0, 0, 0, 255))
    dim.putalpha(mask)
    return Image.alpha_composite(canvas, dim)


# -- public helpers for other engines (recording annotations, video editor) ----
def hit_shape(s: Shape, p: Point, slack: int = 6) -> bool:
    """Is ``p`` on shape ``s``? (public for other engines' editors)."""
    return _hit_shape(s, p, slack)


def draw_shape(img: Image.Image, overlay: ImageDraw.ImageDraw, s: Shape, number: int | None) -> None:
    """Draw one shape for other engines (recording annotations, video editor).

    Vector shapes are composited supersampled onto ``img`` (an RGBA layer, usually the
    one ``overlay`` draws on); blur and pixelate change the pixels of ``img``. Pictures
    need the document that owns them, so they are not drawn here.
    """
    del overlay  # kept for API compatibility: everything is painted through ``img``
    if s.kind in ("blur", "pixelate"):
        _apply_effect(img, s)
    elif s.kind in ("spotlight", "image"):
        return
    else:
        _composite_vectors(img, [(s, number)])


def shape_extent(s: Shape) -> Rect:
    """Pixels a vector shape may touch (stroke, arrow head, step shadow, text)."""
    return _paint_extent(s)


def render(image: CapturedImage, doc: EditorDocument, *, apply_crop: bool = True, skip: Iterable[int] = ()) -> CapturedImage:
    """Flatten the document onto the image (on a larger white canvas when shapes sit beside it)."""
    base = image.to_pil().convert("RGB")
    skip = set(skip)
    ext = doc.extent()
    if (ext.x, ext.y, ext.width, ext.height) != (0, 0, base.width, base.height):
        sheet = Image.new("RGB", (ext.width, ext.height), CANVAS_BACKGROUND)
        sheet.paste(base, (-ext.x, -ext.y))
        base = sheet
        crop = doc.crop.translate(-ext.x, -ext.y) if doc.crop is not None else None
        doc = EditorDocument(ext.width, ext.height, [s.moved(-ext.x, -ext.y) for s in doc.shapes], crop, doc.step_start, doc.images)
    # Everything is painted in stacking order, so blur / pixelate / spotlight affect whatever lies
    # underneath them: the capture *and* pictures placed earlier (also beside the capture).
    canvas = base.convert("RGBA")
    spot = [s for i, s in enumerate(doc.shapes) if s.kind == "spotlight" and i not in skip]
    batch: list[tuple[Shape, int | None]] = []
    for index, s in enumerate(doc.shapes):
        if index in skip:
            continue
        if s.kind in ("image", "blur", "pixelate", "spotlight"):
            _composite_vectors(canvas, batch)  # shapes before it end up underneath
            batch = []
            if s.kind == "image":
                _paste_picture(canvas, doc, s)
            elif s.kind == "spotlight":
                if s is spot[0]:  # all spotlight holes dim at once, at the first one's level
                    canvas = _spotlight(canvas, spot)
            else:
                _apply_effect(canvas, s)
            continue
        batch.append((s, doc.step_number(index) if s.kind == "step" else None))
    _composite_vectors(canvas, batch)
    result = canvas.convert("RGB")
    if apply_crop and doc.crop is not None:
        c = doc.crop
        result = result.crop((c.x, c.y, c.right, c.bottom))
    return CapturedImage.from_pil(result)


# -- handles (shared by the editor window; headless so they can be tested without Qt) ------------
SCALED_KINDS = ("text", "emoji", "step")
MIN_FONT = {"text": 6, "emoji": 12, "step": 6}
MAX_FONT = 400


def box_handles(b: Rect) -> dict[str, Point]:
    """8 handles on a box: corners and edge middles (inclusive coordinates, like p1/p2)."""
    l, t, r, bm = b.x, b.y, b.right - 1, b.bottom - 1
    cx, cy = (l + r) // 2, (t + bm) // 2
    return {
        "nw": Point(l, t), "n": Point(cx, t), "ne": Point(r, t), "e": Point(r, cy),
        "se": Point(r, bm), "s": Point(cx, bm), "sw": Point(l, bm), "w": Point(l, cy),
    }


def resize_box(b: Rect, handle: str, p: Point, keep_ratio: bool, min_size: int = 6) -> tuple[int, int, int, int]:
    """Drag ``handle`` of box ``b`` to ``p``; the opposite side stays. Returns inclusive l, t, r, bottom."""
    l, t, r, bm = b.x, b.y, b.right - 1, b.bottom - 1
    if "w" in handle:
        l = min(p.x, r - min_size)
    if "e" in handle:
        r = max(p.x, l + min_size)
    if "n" in handle:
        t = min(p.y, bm - min_size)
    if "s" in handle:
        bm = max(p.y, t + min_size)
    if keep_ratio and len(handle) == 2 and b.width > 0 and b.height > 0:
        ratio = b.width / b.height
        w, h = r - l + 1, bm - t + 1
        if w / h > ratio:
            w = max(min_size, round(h * ratio))
        else:
            h = max(min_size, round(w / ratio))
        if "w" in handle:
            l = r - w + 1
        else:
            r = l + w - 1
        if "n" in handle:
            t = bm - h + 1
        else:
            bm = t + h - 1
    return l, t, r, bm


def scaled(origin: Shape, handle: str, p: Point) -> Shape:
    """Text, symbols and step badges: a corner handle scales the size; the opposite corner stays put."""
    b = shape_bounds(origin)
    l, t, r, bm = resize_box(b, handle, p, True)
    factor = ((r - l + 1) / max(1, b.width) + (bm - t + 1) / max(1, b.height)) / 2
    size = max(MIN_FONT.get(origin.kind, 6), min(MAX_FONT, round(origin.font_size * factor)))
    shape = replace(origin, font_size=size)
    nb = shape_bounds(shape)
    ax, nx = (b.x, nb.x) if "e" in handle else (b.right, nb.right)
    ay, ny = (b.y, nb.y) if "s" in handle else (b.bottom, nb.bottom)
    return shape.moved(ax - nx, ay - ny)
