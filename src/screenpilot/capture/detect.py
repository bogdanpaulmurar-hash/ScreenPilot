"""Automatic frame detection ("magic box") on a captured image.

Given a point, find the rectangles drawn around it on screen — input fields,
panels, group boxes, table cells, dialogs, cards — from innermost to
outermost, so the selector can snap to them without dragging.

How it works (fast enough to run on every mouse pause, pure Pillow + bytes):
1. Once per capture, build binary edge maps: a vertical-edge map (colour jump
   between x and x+1) and a horizontal-edge map (between y and y+1), plus
   their transposes so whole columns can be sliced as contiguous bytes.
2. From the point, walk left/right/up/down along the point's row/column and
   keep only edge pixels that belong to a real line (a long straight run of
   edge pixels through that row/column) — text glyphs are too short.
3. Combine the nearest candidate lines on each side and keep the rectangles
   whose four sides are (almost) continuous lines.

Grids (Excel, Google Sheets, ERP tables) are the worst case: every row and
column line is a real line, so the four candidate lists are full and the
naive search is 48⁴ ≈ 5 million combinations — seconds per mouse position,
which froze the selector. The search therefore tries combinations nearest
first (small boxes before big ones), remembers every side test, stops after
enough boxes, and never runs longer than :data:`SEARCH_BUDGET_S`.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image, ImageChops

from screenpilot.capture.geometry import Point, Rect
from screenpilot.capture.image import CapturedImage

EDGE_THRESHOLD = 10  # grey-level jump that counts as an edge (Fluent borders are subtle)
LINE_PROBE = 12  # half-length of the straight run checked through a candidate edge
LINE_PROBE_MIN = 0.8
SIDE_COVERAGE = 0.85  # fraction of a box side that must be an edge line
MAX_CANDIDATES = 48  # candidate lines kept per direction
MAX_SCAN = 2400
MIN_BOX = 10
CORNER = 6  # sides must reach the corners: rejects lines that merely cross a box
CORNER_COVERAGE = 0.34  # tolerate rounded corners (Fluent buttons, cards)
ROUNDING_PX = 10
MERGE_PX = 3  # boxes this close are the same frame (double / anti-aliased borders)
MAX_LEVELS = 6
ON = 0xFF
SEARCH_BUDGET_S = 0.025  # per mouse position; the selector repaints at ~60 Hz
MAX_BOXES = 160  # boxes collected before the nesting chain is built
CACHE_POINTS = 64


@dataclass
class _Maps:
    width: int
    height: int
    v_rows: bytes  # vertical edges, row-major: index y*w + x  (edge between x and x+1)
    v_cols: bytes  # same, column-major: index x*h + y
    h_rows: bytes  # horizontal edges, row-major (edge between y and y+1)
    h_cols: bytes  # same, column-major


class EdgeDetector:
    def __init__(self, image: CapturedImage, *, threshold: int = EDGE_THRESHOLD) -> None:
        gray = image.to_pil().convert("L")
        w, h = gray.size
        lut = [ON if v >= threshold else 0 for v in range(256)]
        right = ImageChops.offset(gray, -1, 0)
        below = ImageChops.offset(gray, 0, -1)
        vert = ImageChops.difference(gray, right).point(lut)
        horiz = ImageChops.difference(gray, below).point(lut)
        # offset() wraps around: the last column/row compares with the first; clear them.
        vert.paste(0, (w - 1, 0, w, h))
        horiz.paste(0, (0, h - 1, w, h))
        self.m = _Maps(
            w,
            h,
            vert.tobytes(),
            vert.transpose(Image.Transpose.TRANSPOSE).tobytes(),
            horiz.tobytes(),
            horiz.transpose(Image.Transpose.TRANSPOSE).tobytes(),
        )

    # -- line tests -----------------------------------------------------------
    def _v_line(self, x: int, y0: int, y1: int) -> float:
        """Fraction of edge pixels on the vertical line at column x between rows y0..y1 (inclusive)."""
        m = self.m
        y0, y1 = max(0, y0), min(m.height - 1, y1)
        if y1 < y0:
            return 0.0
        seg = m.v_cols[x * m.height + y0 : x * m.height + y1 + 1]
        return seg.count(ON) / len(seg)

    def _h_line(self, y: int, x0: int, x1: int) -> float:
        m = self.m
        x0, x1 = max(0, x0), min(m.width - 1, x1)
        if x1 < x0:
            return 0.0
        seg = m.h_rows[y * m.width + x0 : y * m.width + x1 + 1]
        return seg.count(ON) / len(seg)

    # -- candidate scanning ---------------------------------------------------
    def _scan(self, data: bytes, base: int, start: int, stop: int, step: int, accept) -> list[int]:
        found: list[int] = []
        i = start
        while (i >= stop if step < 0 else i <= stop) and len(found) < MAX_CANDIDATES:
            if step < 0:
                j = data.rfind(ON, base + stop, base + i + 1)
            else:
                j = data.find(ON, base + i, base + stop + 1)
            if j < 0:
                break
            pos = j - base
            if accept(pos):
                found.append(pos)
            i = pos + step
        return found

    def boxes_at(self, p: Point) -> list[Rect]:
        """Rectangles around ``p`` (image coordinates), innermost first."""
        m = self.m
        x, y = p.x, p.y
        if not (0 <= x < m.width and 0 <= y < m.height):
            return []
        probe = lambda f, c, a: f(c, a - LINE_PROBE, a + LINE_PROBE) >= LINE_PROBE_MIN  # noqa: E731
        lefts = self._scan(m.v_rows, y * m.width, x - 1, max(0, x - MAX_SCAN), -1, lambda c: probe(self._v_line, c, y))
        rights = self._scan(
            m.v_rows, y * m.width, x, min(m.width - 2, x + MAX_SCAN), 1, lambda c: probe(self._v_line, c, y)
        )
        tops = self._scan(m.h_cols, x * m.height, y - 1, max(0, y - MAX_SCAN), -1, lambda r: probe(self._h_line, r, x))
        bottoms = self._scan(
            m.h_cols, x * m.height, y, min(m.height - 2, y + MAX_SCAN), 1, lambda r: probe(self._h_line, r, x)
        )
        found = self._search(lefts, rights, tops, bottoms)
        boxes = sorted(found.values(), key=lambda r: r.area)
        # Clean nesting chain: each box contains the previous one; near-identical
        # boxes (a 1 px border seen from both sides) collapse into the outer one.
        chain: list[Rect] = []
        for box in boxes:
            if chain and box.intersect(chain[-1]) != chain[-1]:
                continue
            if chain and _close(box, chain[-1]):
                chain[-1] = box
                continue
            chain.append(box)
        return chain[:MAX_LEVELS]

    def _search(self, lefts: list[int], rights: list[int], tops: list[int], bottoms: list[int]) -> dict:
        """Boxes made of one candidate per side, nearest combinations first, within a time budget."""
        found: dict[tuple[int, int, int, int], Rect] = {}
        deadline = time.perf_counter() + SEARCH_BUDGET_S
        h_cache: dict[tuple[int, int, int], bool] = {}
        v_cache: dict[tuple[int, int, int], bool] = {}

        def h_side(y: int, l: int, r: int) -> bool:
            key = (y, l, r)
            if key not in h_cache:
                h_cache[key] = self._h_side(y, l, r)
            return h_cache[key]

        def v_side(x: int, t: int, b: int) -> bool:
            key = (x, t, b)
            if key not in v_cache:
                v_cache[key] = self._v_side(x, t, b)
            return v_cache[key]

        # Pairs grouped by "distance rank" (index of the left + index of the right
        # candidate, nearest = 0). Combinations are visited in order of the total
        # rank of all four sides, so small boxes around the point come first and
        # every nesting level is reached early, whatever the time budget allows.
        # Both edges of a 1 px line are candidates; they share one rank.
        rank_l, rank_r, rank_t, rank_b = (_ranks(c) for c in (lefts, rights, tops, bottoms))
        horizontal: dict[int, list[tuple[int, int]]] = {}
        for i, l in enumerate(lefts):
            for j, r in enumerate(rights):
                if r - l >= MIN_BOX:
                    horizontal.setdefault(rank_l[i] + rank_r[j], []).append((l, r))
        vertical: dict[int, list[tuple[int, int]]] = {}
        for i, t in enumerate(tops):
            for j, b in enumerate(bottoms):
                if b - t >= MIN_BOX:
                    vertical.setdefault(rank_t[i] + rank_b[j], []).append((t, b))
        if not horizontal or not vertical:
            return found
        checks = 0
        for total in range(max(horizontal) + max(vertical) + 1):
            for h_rank in range(total + 1):
                h_pairs, v_pairs = horizontal.get(h_rank), vertical.get(total - h_rank)
                if not h_pairs or not v_pairs:
                    continue
                for l, r in h_pairs:
                    for t, b in v_pairs:
                        checks += 1
                        if checks & 0xFF == 0 and time.perf_counter() > deadline:
                            return found
                        if h_side(t, l, r) and h_side(b, l, r) and v_side(l, t, b) and v_side(r, t, b):
                            rect = Rect(l + 1, t + 1, r - l, b - t)
                            found[(rect.x, rect.y, rect.width, rect.height)] = rect
                            if len(found) >= MAX_BOXES:
                                return found
        return found

    def _h_side(self, y: int, l: int, r: int) -> bool:
        """Horizontal side at edge-row y spanning the interior l+1..r and reaching both corners."""
        return (
            self._h_line(y, l + 1, r) >= _required(r - l)
            and self._h_line(y, l + 1, l + CORNER) >= CORNER_COVERAGE
            and self._h_line(y, r - CORNER, r) >= CORNER_COVERAGE
        )

    def _v_side(self, x: int, t: int, b: int) -> bool:
        return (
            self._v_line(x, t + 1, b) >= _required(b - t)
            and self._v_line(x, t + 1, t + CORNER) >= CORNER_COVERAGE
            and self._v_line(x, b - CORNER, b) >= CORNER_COVERAGE
        )


def _ranks(candidates: list[int]) -> list[int]:
    """Distance rank of each candidate (nearest first); candidates closer than
    MERGE_PX to the previous one are the same line and share its rank."""
    ranks: list[int] = []
    rank = -1
    previous: int | None = None
    for c in candidates:
        if previous is None or abs(c - previous) > MERGE_PX:
            rank += 1
        ranks.append(rank)
        previous = c
    return ranks


def _required(length: int) -> float:
    """Side coverage needed; short sides may lose up to ~10 px to rounded corners."""
    return min(SIDE_COVERAGE, 1 - ROUNDING_PX / max(1, length))


def _close(a: Rect, b: Rect) -> bool:
    return (
        abs(a.x - b.x) <= MERGE_PX
        and abs(a.y - b.y) <= MERGE_PX
        and abs(a.right - b.right) <= MERGE_PX
        and abs(a.bottom - b.bottom) <= MERGE_PX
    )


class FrameFinder:
    """Detector bound to a frozen desktop image positioned at ``origin``."""

    def __init__(self, image: CapturedImage, origin: Point) -> None:
        self.origin = origin
        from screenpilot.core import diagnostics

        with diagnostics.timed("detect.build", width=image.width, height=image.height):
            self.detector = EdgeDetector(image)
        self._cache: OrderedDict[Point, list[Rect]] = OrderedDict()

    def boxes_at(self, p: Point) -> list[Rect]:
        """Boxes around ``p`` (desktop coordinates); recent points are remembered."""
        cache = self._cache
        if p in cache:
            cache.move_to_end(p)
            return list(cache[p])
        local = Point(p.x - self.origin.x, p.y - self.origin.y)
        boxes = [b.translate(self.origin.x, self.origin.y) for b in self.detector.boxes_at(local)]
        cache[p] = boxes
        if len(cache) > CACHE_POINTS:
            cache.popitem(last=False)
        return list(boxes)
