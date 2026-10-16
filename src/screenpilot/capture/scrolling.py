"""Scrolling capture: scroll a region step by step and stitch one tall image.

Works for browsers, ERP grids, long forms and reports — anything that
scrolls with the mouse wheel. Pure-Python core (testable anywhere); the
Windows wheel driver lives in ``win32_scroller``.

Stitching: every row of a frame gets a signature (its pixels, ignoring the
scrollbar strip). Rows that stay identical at the same position between two
frames are sticky headers/footers and are kept once. In the moving middle,
rows that occur exactly once are used as anchors; their position change
votes for the scroll offset, which is then verified over the whole overlap.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Callable, Protocol

from screenpilot.capture.geometry import Rect
from screenpilot.capture.image import BYTES_PER_PIXEL, CapturedImage

log = logging.getLogger(__name__)

SCROLLBAR_IGNORE = 0.12  # right part of each row ignored for matching (scrollbar thumb moves)
MAX_STATIC_RATIO = 0.35  # sticky header/footer can't be more than this much of the view
MIN_MATCH = 0.9
DEFAULT_MAX_HEIGHT = 30_000
DEFAULT_MAX_FRAMES = 80


class StitchError(Exception):
    pass


def row_signatures(image: CapturedImage) -> list[bytes]:
    stride = image.stride
    x1 = int(image.width * (1 - SCROLLBAR_IGNORE)) * BYTES_PER_PIXEL
    data = image.bgra
    return [data[y * stride : y * stride + x1] for y in range(image.height)]


def find_shift(prev: list[bytes], curr: list[bytes]) -> int | None:
    """Rows scrolled between two equally sized middle segments (None if no reliable match)."""
    n = len(prev)
    if n == 0 or prev == curr:
        return 0
    prev_count = Counter(prev)
    curr_count = Counter(curr)
    prev_pos = {sig: i for i, sig in enumerate(prev) if prev_count[sig] == 1}
    votes: Counter[int] = Counter()
    for i, sig in enumerate(curr):
        if curr_count[sig] == 1:
            j = prev_pos.get(sig)
            if j is not None and j > i:
                votes[j - i] += 1
    for shift, _ in votes.most_common(5):
        overlap = n - shift
        if overlap <= 0:
            continue
        same = sum(1 for k in range(overlap) if prev[shift + k] == curr[k])
        if same / overlap >= MIN_MATCH:
            return shift
    return None


class ScrollStitcher:
    def __init__(self, first: CapturedImage, *, max_height: int = DEFAULT_MAX_HEIGHT) -> None:
        self.width = first.width
        self.view_height = first.height
        self.max_height = max_height
        stride = first.stride
        self.rows: list[bytes] = [first.bgra[y * stride : (y + 1) * stride] for y in range(first.height)]
        self._last_sig = row_signatures(first)
        self._last = first
        self.frames = 1
        self.finished_reason = ""

    @property
    def height(self) -> int:
        return len(self.rows)

    def add(self, frame: CapturedImage) -> int:
        """Append the new content of ``frame``; returns the rows added (0 = nothing moved)."""
        if (frame.width, frame.height) != (self.width, self.view_height):
            raise StitchError("Frame size changed during scrolling")
        sig = row_signatures(frame)
        prev = self._last_sig
        h = self.view_height
        if sig == prev:
            return 0
        cap = int(h * MAX_STATIC_RATIO)
        top = 0
        while top < cap and sig[top] == prev[top]:
            top += 1
        bottom = 0
        while bottom < cap and sig[h - 1 - bottom] == prev[h - 1 - bottom]:
            bottom += 1
        shift = find_shift(prev[top : h - bottom], sig[top : h - bottom])
        if shift is None:
            raise StitchError("Could not match consecutive frames")
        if shift == 0:
            return 0
        stride = frame.stride
        start = h - bottom - shift
        new_rows = [frame.bgra[y * stride : (y + 1) * stride] for y in range(start, h - bottom)]
        room = self.max_height - len(self.rows)
        if room <= 0:
            return 0
        new_rows = new_rows[:room]
        insert_at = len(self.rows) - bottom  # keep a sticky footer at the very bottom
        self.rows[insert_at:insert_at] = new_rows
        self._last_sig = sig
        self._last = frame
        self.frames += 1
        return len(new_rows)

    def result(self) -> CapturedImage:
        return CapturedImage(self.width, len(self.rows), b"".join(self.rows))


class Scroller(Protocol):
    def scroll(self, rect: Rect, notches: int) -> None: ...

    def stop_requested(self) -> bool: ...


def notches_for(rect: Rect) -> int:
    """Scroll less than one view height so consecutive frames overlap."""
    return max(1, min(5, rect.height // 220))


def capture_scrolling(
    grab: Callable[[Rect], CapturedImage],
    rect: Rect,
    scroller: Scroller,
    *,
    max_frames: int = DEFAULT_MAX_FRAMES,
    max_height: int = DEFAULT_MAX_HEIGHT,
    settle: Callable[[], None] = lambda: time.sleep(0.35),
    progress: Callable[[int, int], None] | None = None,
) -> tuple[CapturedImage, str]:
    """Scroll ``rect`` until the content stops moving; returns (image, reason).

    reason is one of "end", "stopped", "max_height", "max_frames", "no_match".
    """
    stitcher = ScrollStitcher(grab(rect), max_height=max_height)
    notches = notches_for(rect)
    reason = "max_frames"
    still = 0
    for _ in range(max_frames):
        if scroller.stop_requested():
            reason = "stopped"
            break
        scroller.scroll(rect, notches)
        settle()
        try:
            added = stitcher.add(grab(rect))
        except StitchError as exc:
            log.warning("Scrolling capture stopped: %s", exc)
            reason = "no_match"
            break
        if progress is not None:
            progress(stitcher.frames, stitcher.height)
        if stitcher.height >= max_height:
            reason = "max_height"
            break
        if added == 0:
            still += 1
            if still >= 2:  # nothing moved twice in a row: end of the content
                reason = "end"
                break
        else:
            still = 0
    log.info("Scrolling capture: %d frames, %d px, %s", stitcher.frames, stitcher.height, reason)
    return stitcher.result(), reason
