"""Interactive region selection logic, independent of any UI toolkit.

The Qt overlay feeds mouse/keyboard input into :class:`SelectionModel` and only
paints its state. Keeping the behaviour here makes it unit-testable and lets
another front end reuse it.

All coordinates are physical virtual-desktop pixels.

Interaction:
* hover            -> the window under the cursor is highlighted (snapping)
* click            -> select the highlighted window
* drag             -> select a rectangle (pixel precise, any direction)
* after selecting  -> drag a handle to resize, drag inside to move,
                      arrows move, Alt+arrows resize, Shift = 10 px steps
* Enter / double-click confirms, Esc cancels
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence

from screenpilot.capture.geometry import Point, Rect, WindowInfo

CLICK_TOLERANCE = 3  # px of movement still treated as a click
HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")


class State(str, Enum):
    IDLE = "idle"  # nothing selected yet, hovering
    DRAGGING = "dragging"  # creating a new rectangle
    ADJUSTING = "adjusting"  # selection exists, can be tweaked
    MOVING = "moving"
    RESIZING = "resizing"


@dataclass(frozen=True)
class Snapshot:
    """Immutable view of the model for painting."""

    state: State
    cursor: Point
    selection: Rect | None
    hover: Rect | None


class SelectionModel:
    def __init__(
        self,
        bounds: Rect,
        windows: Sequence[WindowInfo] = (),
        *,
        snap: bool = True,
        handle_size: int = 8,
        frames: Callable[[Point, WindowInfo | None], Sequence[Rect]] | None = None,
    ) -> None:
        self.bounds = bounds
        self.frames = frames  # inner frame detection (image edges, child controls)
        self.level = 0  # 0 = innermost frame under the cursor; wheel/Tab go outward
        self._candidates_at: Point | None = None
        self._candidates: list[Rect] = []
        self.windows = [w for w in windows if not w.bounds.intersect(bounds).is_empty]
        self.snap = snap
        self.handle_size = handle_size
        self.state = State.IDLE
        self.cursor = Point(bounds.x + bounds.width // 2, bounds.y + bounds.height // 2)
        self.selection: Rect | None = None
        self._anchor: Point | None = None
        self._press: Point | None = None
        self._grab_offset = Point(0, 0)
        self._handle: str | None = None
        self._before_edit: Rect | None = None
        self._hover_at_press: Rect | None = None

    # -- queries --------------------------------------------------------------
    def snapshot(self) -> Snapshot:
        return Snapshot(self.state, self.cursor, self.selection, self.hover_rect())

    def hover_window(self) -> WindowInfo | None:
        if not self.snap or self.state is not State.IDLE:
            return None
        for window in self.windows:  # Z-order: topmost first
            if window.bounds.contains(self.cursor):
                return window
        return None

    def hover_candidates(self) -> list[Rect]:
        """Frames under the cursor, innermost first, ending with the whole window."""
        if not self.snap or self.state is not State.IDLE:
            return []
        if self._candidates_at == self.cursor:
            return self._candidates
        window = self.hover_window()
        outer = window.bounds.intersect(self.bounds) if window else None
        found: list[Rect] = []
        if self.frames is not None:
            for rect in self.frames(self.cursor, window):
                rect = rect.intersect(outer or self.bounds)
                if rect.is_empty or rect.width < 4 or rect.height < 4 or not rect.contains(self.cursor):
                    continue
                if outer is not None and rect == outer:
                    continue
                found.append(rect)
        found.sort(key=lambda r: r.area)
        chain: list[Rect] = []
        for rect in found:  # keep a strict nesting chain
            if not chain or (rect.intersect(chain[-1]) == chain[-1] and rect != chain[-1]):
                chain.append(rect)
        if outer is not None:
            chain.append(outer)
        previous_inner = self._candidates[0] if self._candidates else None
        self._candidates_at, self._candidates = self.cursor, chain
        if not chain or chain[0] != previous_inner:
            self.level = 0  # moved onto a different frame: start from the innermost again
        return chain

    def hover_rect(self) -> Rect | None:
        candidates = self.hover_candidates()
        if not candidates:
            return None
        return candidates[min(self.level, len(candidates) - 1)]

    def cycle(self, delta: int) -> None:
        """Walk outward (+1) or inward (-1) through the frames under the cursor."""
        candidates = self.hover_candidates()
        if candidates:
            self.level = min(max(0, min(self.level, len(candidates) - 1) + delta), len(candidates) - 1)

    def handle_rects(self) -> dict[str, Rect]:
        if self.selection is None:
            return {}
        s, h = self.selection, self.handle_size
        xs = {"w": s.x, "c": s.x + s.width // 2, "e": s.right - 1}
        ys = {"n": s.y, "c": s.y + s.height // 2, "s": s.bottom - 1}
        centers = {
            "nw": (xs["w"], ys["n"]),
            "n": (xs["c"], ys["n"]),
            "ne": (xs["e"], ys["n"]),
            "e": (xs["e"], ys["c"]),
            "se": (xs["e"], ys["s"]),
            "s": (xs["c"], ys["s"]),
            "sw": (xs["w"], ys["s"]),
            "w": (xs["w"], ys["c"]),
        }
        return {name: Rect(cx - h // 2, cy - h // 2, h, h) for name, (cx, cy) in centers.items()}

    def hit_test(self, p: Point) -> str | None:
        """'nw'..'w' for a handle, 'inside', or None."""
        for name, rect in self.handle_rects().items():
            if rect.contains(p):
                return name
        if self.selection is not None and self.selection.contains(p):
            return "inside"
        return None

    # -- mouse ----------------------------------------------------------------
    def _clamp(self, p: Point) -> Point:
        return Point(
            min(max(p.x, self.bounds.x), self.bounds.right - 1),
            min(max(p.y, self.bounds.y), self.bounds.bottom - 1),
        )

    def press(self, p: Point) -> None:
        p = self._clamp(p)
        self.cursor = p
        self._press = p
        hit = self.hit_test(p) if self.state is State.ADJUSTING else None
        if hit == "inside":
            assert self.selection is not None
            self.state = State.MOVING
            self._grab_offset = Point(p.x - self.selection.x, p.y - self.selection.y)
            self._before_edit = self.selection
        elif hit is not None:
            self.state = State.RESIZING
            self._handle = hit
            self._before_edit = self.selection
        else:
            self._hover_at_press = self.hover_rect()
            self.state = State.DRAGGING
            self._anchor = p
            self.selection = Rect(p.x, p.y, 1, 1)

    def move(self, p: Point) -> None:
        p = self._clamp(p)
        self.cursor = p
        if self.state is State.DRAGGING and self._anchor is not None:
            self.selection = Rect.from_points(self._anchor, p)
        elif self.state is State.MOVING and self.selection is not None:
            self.selection = self._fit(
                Rect(p.x - self._grab_offset.x, p.y - self._grab_offset.y, self.selection.width, self.selection.height)
            )
        elif self.state is State.RESIZING and self._before_edit is not None and self._handle:
            self.selection = self._resize(self._before_edit, self._handle, p)

    def release(self, p: Point) -> None:
        self.move(p)
        if self.state is State.DRAGGING:
            press = self._press or p
            moved = max(abs(p.x - press.x), abs(p.y - press.y))
            if moved <= CLICK_TOLERANCE:
                self.selection = self._hover_at_press  # click = pick window (None if no window)
            self.state = State.ADJUSTING if self.selection is not None else State.IDLE
        elif self.state in (State.MOVING, State.RESIZING):
            self.state = State.ADJUSTING
        self._handle = None
        self._anchor = None

    # -- keyboard -------------------------------------------------------------
    def nudge(self, dx: int, dy: int, *, resize: bool = False) -> None:
        if self.selection is None:
            self.cursor = self._clamp(Point(self.cursor.x + dx, self.cursor.y + dy))
            return
        s = self.selection
        if resize:
            self.selection = Rect(s.x, s.y, max(1, s.width + dx), max(1, s.height + dy)).intersect(self.bounds)
        else:
            self.selection = self._fit(Rect(s.x + dx, s.y + dy, s.width, s.height))

    def select_all(self) -> None:
        self.selection = self.bounds
        self.state = State.ADJUSTING

    def reset(self) -> None:
        self.selection = None
        self.state = State.IDLE

    def result(self) -> Rect | None:
        return self.selection if self.selection and not self.selection.is_empty else None

    # -- helpers --------------------------------------------------------------
    def _fit(self, r: Rect) -> Rect:
        """Keep a moved rectangle fully inside the bounds without changing its size."""
        b = self.bounds
        x = min(max(r.x, b.x), b.right - r.width)
        y = min(max(r.y, b.y), b.bottom - r.height)
        return Rect(x, y, r.width, r.height)

    @staticmethod
    def _resize(start: Rect, handle: str, p: Point) -> Rect:
        left, top, right, bottom = start.x, start.y, start.right - 1, start.bottom - 1
        if "w" in handle:
            left = p.x
        if "e" in handle:
            right = p.x
        if "n" in handle:
            top = p.y
        if "s" in handle:
            bottom = p.y
        return Rect.from_points(Point(left, top), Point(right, bottom))


class RegionList:
    """Regions collected one after another while Ctrl is held (multi-capture).

    Kept in selection order across all monitors: the order decides the
    clipboard sequence and the layout in the editor. Headless, like
    :class:`SelectionModel`, so it is unit-testable.
    """

    def __init__(self) -> None:
        self.rects: list[Rect] = []

    def __len__(self) -> int:
        return len(self.rects)

    def __iter__(self):
        return iter(self.rects)

    def add(self, rect: Rect | None) -> bool:
        """Append a region; empty ones and an exact repeat of the last one are ignored."""
        if rect is None or rect.is_empty:
            return False
        if self.rects and self.rects[-1] == rect:
            return False
        self.rects.append(rect)
        return True

    def pop(self) -> Rect | None:
        """Drop the last region (Backspace / Ctrl+Z)."""
        return self.rects.pop() if self.rects else None

    def remove_at(self, p: Point) -> Rect | None:
        """Drop the most recent region under ``p`` (right-click on it)."""
        for i in range(len(self.rects) - 1, -1, -1):
            if self.rects[i].contains(p):
                return self.rects.pop(i)
        return None

    def clear(self) -> None:
        self.rects.clear()

    def number(self, rect: Rect) -> int:
        """1-based position shown on the region, 0 if it is not in the list."""
        for i, r in enumerate(self.rects):
            if r == rect:
                return i + 1
        return 0

    def final(self, current: Rect | None = None) -> list[Rect]:
        """What a confirmation captures: the collected regions plus the current selection."""
        rects = list(self.rects)
        if current is not None and not current.is_empty and current not in rects:
            rects.append(current)
        return rects
