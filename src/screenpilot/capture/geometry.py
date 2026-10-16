"""Screen geometry in physical pixels on the virtual desktop.

Windows places monitors on one virtual desktop whose origin is the primary
monitor's top-left corner; monitors to the left of / above it have negative
coordinates. All capture rectangles use these physical-pixel coordinates (the
process is per-monitor DPI aware), which gives pixel-exact captures on mixed
DPI setups.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_points(cls, a: Point, b: Point) -> "Rect":
        """Rectangle spanned by a drag gesture in any direction (inclusive of both points)."""
        left, right = sorted((a.x, b.x))
        top, bottom = sorted((a.y, b.y))
        return cls(left, top, right - left + 1, bottom - top + 1)

    @classmethod
    def from_ltrb(cls, left: int, top: int, right: int, bottom: int) -> "Rect":
        """From exclusive right/bottom edges (Win32 RECT convention)."""
        return cls(left, top, max(0, right - left), max(0, bottom - top))

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    @property
    def area(self) -> int:
        return 0 if self.is_empty else self.width * self.height

    def contains(self, p: Point) -> bool:
        return self.x <= p.x < self.right and self.y <= p.y < self.bottom

    def intersect(self, other: "Rect") -> "Rect":
        left, top = max(self.x, other.x), max(self.y, other.y)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return Rect(left, top, 0, 0)
        return Rect.from_ltrb(left, top, right, bottom)

    def union(self, other: "Rect") -> "Rect":
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        return Rect.from_ltrb(
            min(self.x, other.x), min(self.y, other.y), max(self.right, other.right), max(self.bottom, other.bottom)
        )

    def translate(self, dx: int, dy: int) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.width, self.height)

    @classmethod
    def parse(cls, text: str) -> "Rect":
        """Parse ``"x,y,w,h"`` (used by CLI / automation)."""
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 4:
            raise ValueError(f"Expected x,y,width,height, got {text!r}")
        x, y, w, h = (int(p) for p in parts)
        if w <= 0 or h <= 0:
            raise ValueError("Width and height must be positive")
        return cls(x, y, w, h)

    def __str__(self) -> str:
        return f"{self.x},{self.y},{self.width},{self.height}"


@dataclass(frozen=True)
class Monitor:
    index: int
    bounds: Rect
    work_area: Rect
    primary: bool
    device_name: str = ""
    scale: float = 1.0  # e.g. 1.5 for 150 % scaling


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    bounds: Rect
    class_name: str = ""
    process_id: int = 0


def union_all(rects: Iterable[Rect]) -> Rect:
    result = Rect(0, 0, 0, 0)
    for rect in rects:
        result = result.union(rect)
    return result


class VirtualDesktop:
    """The set of monitors and helpers to reason about them."""

    def __init__(self, monitors: Sequence[Monitor]) -> None:
        if not monitors:
            raise ValueError("At least one monitor is required")
        self.monitors = list(monitors)

    @property
    def bounds(self) -> Rect:
        return union_all(m.bounds for m in self.monitors)

    @property
    def primary(self) -> Monitor:
        return next((m for m in self.monitors if m.primary), self.monitors[0])

    def monitor_at(self, point: Point) -> Monitor:
        """Monitor containing ``point``; nearest monitor if the point is in a gap."""
        for monitor in self.monitors:
            if monitor.bounds.contains(point):
                return monitor

        def distance(m: Monitor) -> int:
            b = m.bounds
            dx = max(b.x - point.x, 0, point.x - (b.right - 1))
            dy = max(b.y - point.y, 0, point.y - (b.bottom - 1))
            return dx * dx + dy * dy

        return min(self.monitors, key=distance)

    def monitor(self, index: int) -> Monitor:
        for monitor in self.monitors:
            if monitor.index == index:
                return monitor
        raise IndexError(f"No monitor with index {index}")

    def clamp(self, rect: Rect) -> Rect:
        return rect.intersect(self.bounds)

    def is_visible(self, rect: Rect) -> bool:
        """True if any part of ``rect`` is on a real monitor (not just in a gap)."""
        return any(not rect.intersect(m.bounds).is_empty for m in self.monitors)
