"""Live annotations ("ink") drawn into a recording while it runs.

The user (or an automation client) adds numbered steps, arrows, rectangles,
highlights and text on top of the recorded area. The recorder burns them into
every frame, so they end up in the MP4/GIF exactly where they were drawn,
without a second encoding pass.

Design notes:

* Shapes are the editor's :class:`~screenpilot.editor.document.Shape` values
  and are rendered by the editor's Pillow renderer, so a step in a video looks
  like a step in a screenshot.
* Coordinates are **frame pixels** (0,0 = top-left of the recorded area).
  :meth:`InkLayer.from_screen` converts virtual-desktop physical pixels.
* Each item is rendered once into a small BGRA patch (cached by fade level);
  per frame only the patch rectangles are composited, so the cost does not
  depend on the frame size and is zero when there is no ink.
* The layer is thread-safe: the UI thread adds shapes, the recorder thread
  reads snapshots.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageDraw

from screenpilot.capture.geometry import Point, Rect
from screenpilot.editor.document import COLORS, Shape, draw_shape, shape_extent

INK_KINDS = ("step", "arrow", "rect", "highlight", "text")
FADE_OUT_SECONDS = 0.6
FADE_LEVELS = 6  # alpha steps while an item fades out (bounds the patch cache)
STEP_FONT = 22
TEXT_FONT = 22
DEFAULT_WIDTH = 5
MIN_DRAG = 6  # shorter arrows / rectangles are treated as accidental clicks
BAND = 24  # rows per composited strip (see split_patch)
DRAFT_INTERVAL = 0.08  # s between re-renders of the shape being dragged (recorder side)


@dataclass(frozen=True)
class InkItem:
    id: int
    shape: Shape
    number: int | None  # step number, None for other kinds
    created: float


@dataclass(frozen=True)
class InkView:
    """What to draw now: an item and its opacity (1.0 = fully visible)."""

    item: InkItem
    opacity: float


class InkLayer:
    def __init__(
        self,
        width: int,
        height: int,
        *,
        origin: Point = Point(0, 0),
        color: str = COLORS["red"],
        fade_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.width, self.height = width, height
        self.origin = origin
        self.color = color
        self.fade_seconds = max(0.0, float(fade_seconds))
        self.clock = clock
        self.burn_in = True  # False when the on-screen overlay itself ends up in the frames
        self.drawing = False  # draw mode: the mouse is used for ink (no click ripples)
        self._items: list[InkItem] = []
        self._draft: InkItem | None = None
        self._next_id = 1
        self._next_step = 1
        self._version = 0
        self._lock = threading.Lock()

    # -- coordinates --------------------------------------------------------------
    def from_screen(self, p: Point) -> Point:
        return Point(p.x - self.origin.x, p.y - self.origin.y)

    def _clamp(self, p: Point) -> Point:
        return Point(min(max(p.x, 0), self.width - 1), min(max(p.y, 0), self.height - 1))

    # -- building shapes ------------------------------------------------------------
    def make(self, kind: str, p1: Point, p2: Point | None = None, *, text: str = "", color: str | None = None) -> Shape:
        if kind not in INK_KINDS:
            raise ValueError(f"Unknown ink kind {kind}")
        p1 = self._clamp(p1)
        p2 = self._clamp(p2 or p1)
        size = TEXT_FONT if kind == "text" else STEP_FONT
        return Shape(kind, p1, p2, color=color or self.color, width=DEFAULT_WIDTH, text=text, font_size=size, fill=kind == "text")

    @staticmethod
    def is_meaningful(shape: Shape) -> bool:
        if shape.kind in ("step",):
            return True
        if shape.kind == "text":
            return bool(shape.text.strip())
        return max(abs(shape.p2.x - shape.p1.x), abs(shape.p2.y - shape.p1.y)) >= MIN_DRAG

    # -- editing (any thread) -----------------------------------------------------
    def add(self, shape: Shape) -> InkItem | None:
        """Add a finished shape; returns ``None`` for an accidental tiny drag."""
        if not self.is_meaningful(shape):
            self.set_draft(None)
            return None
        with self._lock:
            number = None
            if shape.kind == "step":
                number, self._next_step = self._next_step, self._next_step + 1
            item = InkItem(self._next_id, shape, number, self.clock())
            self._next_id += 1
            self._items.append(item)
            self._draft = None
            self._version += 1
            return item

    def add_step(self, at: Point, *, color: str | None = None) -> InkItem | None:
        return self.add(self.make("step", at, color=color))

    def set_draft(self, shape: Shape | None) -> None:
        """Shape being dragged; shown live (also in the video) until :meth:`add`."""
        with self._lock:
            if shape is None and self._draft is None:
                return
            self._draft = None if shape is None else InkItem(0, shape, self._next_step if shape.kind == "step" else None, self.clock())
            self._version += 1

    def undo(self) -> bool:
        with self._lock:
            if not self._items:
                return False
            item = self._items.pop()
            if item.number is not None and item.number == self._next_step - 1:
                self._next_step -= 1
            self._version += 1
            return True

    def clear(self) -> None:
        """Remove every annotation; step numbering continues (step 4 after 1–3)."""
        with self._lock:
            if self._items or self._draft:
                self._items.clear()
                self._draft = None
                self._version += 1

    # -- reading (recorder thread / UI) -----------------------------------------
    @property
    def version(self) -> int:
        return self._version

    @property
    def next_step(self) -> int:
        return self._next_step

    def __len__(self) -> int:
        return len(self._items)

    def _opacity(self, item: InkItem, now: float) -> float:
        if self.fade_seconds <= 0:
            return 1.0
        left = item.created + self.fade_seconds - now
        if left <= 0:
            return 0.0
        if left >= FADE_OUT_SECONDS:
            return 1.0
        level = max(1, round(left / FADE_OUT_SECONDS * FADE_LEVELS))
        return level / FADE_LEVELS

    def views(self, now: float | None = None) -> list[InkView]:
        """Visible items in drawing order; faded-out items are dropped."""
        now = self.clock() if now is None else now
        with self._lock:
            items = list(self._items)
            draft = self._draft
        out: list[InkView] = []
        expired = False
        for item in items:
            opacity = self._opacity(item, now)
            if opacity <= 0:
                expired = True
                continue
            out.append(InkView(item, opacity))
        if expired:
            with self._lock:
                self._items = [i for i in self._items if self._opacity(i, now) > 0]
                self._version += 1
        if draft is not None:
            out.append(InkView(draft, 1.0))
        return out

    @property
    def empty(self) -> bool:
        return not self._items and self._draft is None


@dataclass(frozen=True)
class InkPatch:
    """A rendered item: BGRA bytes (straight alpha) placed at ``rect`` in frame pixels."""

    rect: Rect
    bgra: bytes


def render_patch(item: InkItem, opacity: float, frame: Rect) -> InkPatch | None:
    """Render one item into its own small BGRA patch, clipped to the frame."""
    extent = shape_extent(item.shape).intersect(frame)
    if extent.is_empty:
        return None
    shape = item.shape.moved(-extent.x, -extent.y)
    overlay = Image.new("RGBA", (extent.width, extent.height), (0, 0, 0, 0))
    draw_shape(overlay, ImageDraw.Draw(overlay), shape, item.number)
    if opacity < 1.0:
        alpha = overlay.getchannel("A").point(lambda a: int(a * opacity))
        overlay.putalpha(alpha)
    r, g, b, a = overlay.split()
    return InkPatch(extent, Image.merge("RGBA", (b, g, r, a)).tobytes())


def split_patch(patch: InkPatch, band: int = BAND) -> list[InkPatch]:
    """Cut a patch into horizontal bands trimmed to their visible pixels.

    A long diagonal arrow has a bounding box as large as the frame but touches
    only a thin strip of each band; compositing just those strips keeps the
    per-frame cost proportional to the drawn pixels.
    """
    r = patch.rect
    image = Image.frombytes("RGBA", (r.width, r.height), patch.bgra)
    alpha = image.getchannel("A")
    out: list[InkPatch] = []
    for top in range(0, r.height, band):
        bottom = min(r.height, top + band)
        box = alpha.crop((0, top, r.width, bottom)).getbbox()
        if box is None:
            continue
        left, _t, right, _b = box
        piece = image.crop((left, top, right, bottom))
        out.append(InkPatch(Rect(r.x + left, r.y + top, right - left, bottom - top), piece.tobytes()))
    return out


class InkRenderer:
    """Composites the layer onto raw BGRA frames; patches are cached per item and fade level."""

    def __init__(self) -> None:
        self._cache: dict[tuple, tuple[InkPatch | None, list[InkPatch]]] = {}
        self._draft: tuple[InkPatch | None, list[InkPatch]] | None = None
        self._draft_at = 0.0

    def patches(self, layer: InkLayer, now: float | None = None) -> list[InkPatch]:
        """One rendered patch per visible item (for the on-screen preview)."""
        return [entry[0] for entry in self._entries(layer, now) if entry[0] is not None]

    def strips(self, layer: InkLayer, now: float | None = None) -> list[InkPatch]:
        """Trimmed strips of every item, in drawing order (for compositing into frames)."""
        return [strip for entry in self._entries(layer, now) for strip in entry[1]]

    def _entries(self, layer: InkLayer, now: float | None) -> list[tuple[InkPatch | None, list[InkPatch]]]:
        frame = Rect(0, 0, layer.width, layer.height)
        wanted: dict[tuple, tuple[InkPatch | None, list[InkPatch]]] = {}
        out = []
        tick = time.monotonic()
        for view in layer.views(now):
            item = view.item
            key = (item.id, item.shape, item.number, view.opacity)
            entry = self._cache.get(key)
            if entry is None and item.id == 0 and self._draft is not None and tick - self._draft_at < DRAFT_INTERVAL:
                out.append(self._draft)  # a shape being dragged is re-rendered a few times per second, not per frame
                continue  # not cached under the new key: it is rendered once the interval has passed
            if entry is None:
                patch = render_patch(item, view.opacity, frame)
                entry = (patch, split_patch(patch) if patch is not None else [])
            if item.id == 0 and entry is not self._draft:
                self._draft, self._draft_at = entry, tick
            wanted[key] = entry
            out.append(entry)
        self._cache = wanted  # drops patches of removed / changed items
        return out

    def apply(self, buf: bytearray, width: int, height: int, layer: InkLayer | None, now: float | None = None) -> bool:
        """Draw the ink into ``buf`` in place; returns whether anything was drawn."""
        if layer is None or not layer.burn_in or layer.empty:
            return False
        drawn = False
        for strip in self.strips(layer, now):
            composite(buf, width, height, strip)
            drawn = True
        return drawn


def composite(buf: bytearray, width: int, height: int, patch: InkPatch) -> None:
    r = patch.rect.intersect(Rect(0, 0, width, height))
    if r.is_empty or r != patch.rect:
        return  # patches are rendered clipped to the frame; a mismatch means a stale size
    row = r.width * 4
    rows = b"".join(bytes(buf[(y * width + r.x) * 4 : (y * width + r.x) * 4 + row]) for y in range(r.y, r.bottom))
    base = Image.frombytes("RGBA", (r.width, r.height), rows)
    base.putalpha(255)  # GDI leaves alpha undefined
    top = Image.frombytes("RGBA", (r.width, r.height), patch.bgra)
    out = Image.alpha_composite(base, top).tobytes()
    for i, y in enumerate(range(r.y, r.bottom)):
        start = (y * width + r.x) * 4
        buf[start : start + row] = out[i * row : (i + 1) * row]


__all__ = [
    "INK_KINDS",
    "InkItem",
    "InkLayer",
    "InkPatch",
    "InkRenderer",
    "InkView",
    "composite",
    "render_patch",
    "split_patch",
]
