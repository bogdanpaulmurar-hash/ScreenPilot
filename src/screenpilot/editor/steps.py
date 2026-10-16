"""Step numbering on a capture (the first piece of the Editor engine).

A :class:`StepDocument` holds numbered markers placed on an image. Numbers
are never stored: they follow the markers' order, so deleting step 2 turns
step 3 into step 2 automatically. Rendering uses Pillow, so the same output
is produced by the UI, the CLI and automation clients.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from screenpilot.capture.geometry import Point, Rect
from screenpilot.capture.image import CapturedImage

PALETTE = {
    "blue": (0, 103, 192),
    "red": (196, 43, 28),
    "orange": (218, 98, 0),
    "green": (16, 124, 16),
    "purple": (136, 23, 152),
}
SIZES = {"s": 22, "m": 30, "l": 42}  # badge diameter in image pixels
SHAPES = ("circle", "square")
UNDO_LIMIT = 100


@dataclass
class StepStyle:
    color: str = "blue"
    size: str = "m"
    shape: str = "circle"
    start: int = 1

    @property
    def diameter(self) -> int:
        return SIZES.get(self.size, SIZES["m"])

    @property
    def rgb(self) -> tuple[int, int, int]:
        return PALETTE.get(self.color, PALETTE["blue"])


@dataclass(frozen=True)
class Step:
    point: Point  # badge centre
    frame: Rect | None = None  # element the step refers to (highlighted), if snapped


@dataclass
class StepDocument:
    steps: list[Step] = field(default_factory=list)
    style: StepStyle = field(default_factory=StepStyle)
    _undo: list[tuple[list[Step], StepStyle]] = field(default_factory=list, repr=False)

    @property
    def markers(self) -> list[Point]:
        return [s.point for s in self.steps]

    # -- editing (every change is undoable) -----------------------------------
    def _snapshot(self) -> None:
        self._undo.append((list(self.steps), copy.copy(self.style)))
        del self._undo[:-UNDO_LIMIT]

    def add(self, point: Point, frame: Rect | None = None) -> int:
        """Place the next step at ``point`` (optionally highlighting ``frame``); returns its number."""
        self._snapshot()
        self.steps.append(Step(point, frame))
        return self.number(len(self.steps) - 1)

    def add_for_frame(self, frame: Rect) -> int:
        """Step attached to a detected element: badge on its top-left corner, element highlighted."""
        return self.add(Point(frame.x, frame.y), frame)

    def insert(self, index: int, point: Point, frame: Rect | None = None) -> None:
        self._snapshot()
        self.steps.insert(index, Step(point, frame))

    def move(self, index: int, point: Point, *, record: bool = True) -> None:
        if record:
            self._snapshot()
        self.steps[index] = Step(point, self.steps[index].frame)

    def remove(self, index: int) -> None:
        self._snapshot()
        del self.steps[index]

    def reorder(self, index: int, new_index: int) -> None:
        self._snapshot()
        step = self.steps.pop(index)
        self.steps.insert(new_index, step)

    def clear(self) -> None:
        if self.steps:
            self._snapshot()
            self.steps.clear()

    def set_style(self, **changes) -> None:
        self._snapshot()
        for key, value in changes.items():
            setattr(self.style, key, value)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.steps, self.style = self._undo.pop()
        return True

    # -- queries --------------------------------------------------------------
    def number(self, index: int) -> int:
        return self.style.start + index

    def hit(self, point: Point, slack: int = 4) -> int | None:
        """Index of the marker under ``point`` (topmost = last placed)."""
        radius = self.style.diameter / 2 + slack
        for index in range(len(self.steps) - 1, -1, -1):
            m = self.steps[index].point
            if (m.x - point.x) ** 2 + (m.y - point.y) ** 2 <= radius * radius:
                return index
        return None


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def render_steps(image: CapturedImage, doc: StepDocument, *, scale: float = 1.0) -> CapturedImage:
    """Return a new image with the numbered markers drawn on it."""
    base = image.to_pil().convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    style = doc.style
    d = max(12, round(style.diameter * scale))
    r = d / 2
    font = _font(max(9, round(d * (0.55 if doc.number(len(doc.steps) - 1) < 10 else 0.46))))
    line = max(2, round(3 * scale))
    for step in doc.steps:  # element highlights first, badges on top
        if step.frame is not None:
            f = step.frame
            draw.rounded_rectangle(
                (f.x - line, f.y - line, f.right - 1 + line, f.bottom - 1 + line), max(3, line * 2),
                outline=style.rgb + (255,), width=line,
            )
    for index, step in enumerate(doc.steps):
        m = step.point
        box = (m.x - r, m.y - r, m.x + r, m.y + r)
        shadow = tuple(v + max(1, d // 14) for v in box)
        if style.shape == "square":
            radius = max(2, d // 5)
            draw.rounded_rectangle(shadow, radius, fill=(0, 0, 0, 70))
            draw.rounded_rectangle(box, radius, fill=style.rgb + (255,), outline=(255, 255, 255, 255), width=max(2, d // 12))
        else:
            draw.ellipse(shadow, fill=(0, 0, 0, 70))
            draw.ellipse(box, fill=style.rgb + (255,), outline=(255, 255, 255, 255), width=max(2, d // 12))
        draw.text((m.x, m.y), str(doc.number(index)), fill=(255, 255, 255, 255), font=font, anchor="mm")
    return CapturedImage.from_pil(Image.alpha_composite(base, overlay).convert("RGB"))
