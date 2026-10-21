"""Scrolling capture: scroll a region step by step and stitch one tall image.

Works for browsers, ERP grids, long forms and reports — anything that
scrolls with the mouse wheel. Pure-Python core (testable anywhere); the
Windows wheel driver lives in ``win32_scroller``.

Stitching: every row of a frame gets a signature (its pixels, ignoring the
scrollbar strip). Rows that stay identical at the same position between two
frames are sticky headers/footers and are kept once. In the moving middle,
rows that occur exactly once are used as anchors; their position change
votes for the scroll offset, which is then verified over the whole overlap.

When no exact match exists (hover highlights under the parked cursor,
fractional scroll offsets, anti-aliasing that changes after a scroll), a
tolerant match compares a small per-row profile instead: brightness and the
amount of dark (text) pixels. It accepts the offset where most informative
rows agree, so a few changed rows no longer stop the capture.

Before the first frame and after each scroll, frames are grabbed until two in
a row are identical (the selector overlay has gone, smooth scrolling has
finished), with a time limit for pages that never stand still.
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
FUZZY_MIN_MATCH = 0.75  # share of informative rows that must agree in the tolerant match
FUZZY_MIN_ROWS = 12  # fewer informative rows than this: too little content to match safely
PROFILE_SAMPLES = 160  # pixels sampled per row for the tolerant profile
DARK = bytes(1 if v < 128 else 0 for v in range(256))
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


def row_profiles(image: CapturedImage) -> list[tuple[int, int]]:
    """Per row: (sum of sampled green values, number of dark samples), ignoring the scrollbar strip."""
    stride = image.stride
    usable = max(1, int(image.width * (1 - SCROLLBAR_IGNORE)))
    step = max(1, usable // PROFILE_SAMPLES) * BYTES_PER_PIXEL
    end = usable * BYTES_PER_PIXEL
    data = image.bgra
    out = []
    for y in range(image.height):
        g = data[y * stride + 1 : y * stride + end : step]
        out.append((sum(g), sum(g.translate(DARK))))
    return out


TOL_SUM, TOL_DARK = 4 * PROFILE_SAMPLES, max(2, PROFILE_SAMPLES // 40)


def _close(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return abs(a[0] - b[0]) <= TOL_SUM and abs(a[1] - b[1]) <= TOL_DARK


def find_shift_fuzzy(
    prev: list[tuple[int, int]],
    curr: list[tuple[int, int]],
    hint: int | None = None,
    ignore: tuple[int, int] | None = None,
) -> int | None:
    """Tolerant version of :func:`find_shift` on row profiles (None if nothing convincing).

    ``ignore`` is a band of rows (same position in both frames) left out of the
    comparison: where the mouse cursor is parked, rows get hover highlights.
    """
    n = len(prev)
    if n < 4 or len(curr) != n:
        return None
    background = Counter(curr).most_common(1)[0][0]
    lo, hi = ignore if ignore else (0, 0)
    skip = (lambda k: lo <= k < hi) if ignore else (lambda k: False)
    informative = [k for k in range(n) if curr[k] != background and not skip(k)]
    if len(informative) < FUZZY_MIN_ROWS:
        return None
    if sum(1 for k in informative if _close(prev[k], curr[k])) / len(informative) >= MIN_MATCH:
        return 0  # same view, only rendered a little differently
    min_overlap = max(FUZZY_MIN_ROWS, n // 6)
    scores: list[tuple[float, int]] = []
    for shift in range(1, n - min_overlap + 1):
        overlap = n - shift
        rows = [k for k in informative if k < overlap and not skip(shift + k)]
        if len(rows) < FUZZY_MIN_ROWS:
            continue
        good = sum(1 for k in rows if _close(prev[shift + k], curr[k]))
        scores.append((good / len(rows), shift))
    if not scores:
        return None
    best_score, best = max(scores)
    if best_score < FUZZY_MIN_MATCH:
        return None
    if hint:
        # repeating content (grid rows): prefer the step we scrolled last time when it is about as good
        near = [(sc, sh) for sc, sh in scores if abs(sh - hint) <= 2 and sc >= best_score - 0.03]
        if near:
            return max(near)[1]
    return best


class ScrollStitcher:
    def __init__(self, first: CapturedImage, *, max_height: int = DEFAULT_MAX_HEIGHT) -> None:
        self.width = first.width
        self.view_height = first.height
        self.max_height = max_height
        stride = first.stride
        self.rows: list[bytes] = [first.bgra[y * stride : (y + 1) * stride] for y in range(first.height)]
        self._last_sig = row_signatures(first)
        self._last_profile: list[tuple[int, int]] | None = None
        self._last = first
        self._last_shift: int | None = None
        self.fuzzy_matches = 0
        self.cursor_row: int | None = None  # row of the view where the cursor is parked (hover effects)
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
            if self._last_profile is None:
                self._last_profile = row_profiles(self._last)
            profile = row_profiles(frame)
            prev_p, curr_p = self._last_profile, profile
            # sticky header/footer, allowing the same small rendering differences
            while top < cap and _close(curr_p[top], prev_p[top]):
                top += 1
            while bottom < cap and _close(curr_p[h - 1 - bottom], prev_p[h - 1 - bottom]):
                bottom += 1
            ignore = None
            if self.cursor_row is not None:
                half = max(16, h // 16)
                ignore = (self.cursor_row - half - top, self.cursor_row + half - top)
            shift = find_shift_fuzzy(prev_p[top : h - bottom], curr_p[top : h - bottom], self._last_shift, ignore)
            if shift is None:
                raise StitchError("Could not match consecutive frames")
            self.fuzzy_matches += 1
            log.debug("Scrolling: tolerant match, shift %d", shift)
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
        self._last_profile = None
        self._last_shift = shift
        self.frames += 1
        return len(new_rows)

    def result(self) -> CapturedImage:
        return CapturedImage(self.width, len(self.rows), b"".join(self.rows))


class Scroller(Protocol):
    def scroll(self, rect: Rect, notches: int) -> None: ...

    def stop_requested(self) -> bool: ...


def _wait_released(scroller: Scroller, sleep: Callable[[float], None], timeout: float = 3.0) -> None:
    """After "continue", wait until Esc is up so the same press is not read again."""
    waited = 0.0
    while scroller.stop_requested() and waited < timeout:
        sleep(0.05)
        waited += 0.05


def grab_stable(
    grab: Callable[[Rect], CapturedImage],
    rect: Rect,
    sleep: Callable[[float], None],
    *,
    first_wait: float = 0.12,
    interval: float = 0.1,
    timeout: float = 1.2,
) -> CapturedImage:
    """Grab until two consecutive frames are identical (or ``timeout`` passes); returns the last frame."""
    sleep(first_wait)
    frame = grab(rect)
    waited = 0.0
    while waited < timeout:
        sleep(interval)
        waited += interval
        nxt = grab(rect)
        if nxt.bgra == frame.bgra:
            return nxt
        frame = nxt
    return frame


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
    settle: Callable[[], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
    on_stop: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[CapturedImage, str]:
    """Scroll ``rect`` until the content stops moving; returns (image, reason).

    reason is one of "end", "stopped", "cancelled", "max_height", "max_frames",
    "no_match".

    When the scroller reports a stop request (Esc), ``on_stop`` decides what
    happens while scrolling is paused: ``"continue"``, ``"stop"`` (keep what
    was captured) or ``"cancel"`` (discard). Without it, Esc stops.
    """
    def next_frame(first: bool = False) -> CapturedImage:
        if settle is not None:  # fixed wait (tests)
            settle()
            return grab(rect)
        if first:  # longer gaps: the overlay can stay up for a couple of quick grabs
            return grab_stable(grab, rect, sleep, first_wait=0.3, interval=0.25, timeout=1.5)
        return grab_stable(grab, rect, sleep)

    # the selector overlay may still be on screen for a moment: wait until the view is still
    stitcher = ScrollStitcher(next_frame(first=True), max_height=max_height)
    stitcher.cursor_row = rect.height // 2  # the scroller parks the cursor in the middle
    notches = notches_for(rect)
    reason = "max_frames"
    still = 0
    for _ in range(max_frames):
        if scroller.stop_requested():
            decision = on_stop() if on_stop is not None else "stop"
            if decision == "continue":
                _wait_released(scroller, sleep)
            else:
                reason = "cancelled" if decision == "cancel" else "stopped"
                break
        scroller.scroll(rect, notches)
        try:
            added = stitcher.add(next_frame())
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
    log.info(
        "Scrolling capture: %d frames, %d px, %s (%d tolerant matches)",
        stitcher.frames, stitcher.height, reason, stitcher.fuzzy_matches,
    )
    return stitcher.result(), reason


def capture_scrolling_manual(
    grab: Callable[[Rect], CapturedImage],
    rect: Rect,
    scroller: Scroller,
    *,
    max_height: int = DEFAULT_MAX_HEIGHT,
    max_seconds: float = 300.0,
    interval: float = 0.15,
    progress: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[CapturedImage, str]:
    """The user scrolls (wheel, keys, scrollbar); frames are stitched as they come. Esc finishes.

    Frames that can't be matched (scrolled too far at once) are skipped; scrolling
    back a little lets the capture pick up again. reason: "done", "max_height",
    "timeout" or "end" (nothing was scrolled).
    """
    sleep(0.3)
    stitcher = ScrollStitcher(grab_stable(grab, rect, sleep, first_wait=0.0, interval=0.25, timeout=1.5), max_height=max_height)
    started = clock()
    reason = "timeout"
    skipped = 0
    while clock() - started < max_seconds:
        if scroller.stop_requested():
            reason = "done"
            break
        sleep(interval)
        try:
            added = stitcher.add(grab(rect))
        except StitchError:
            skipped += 1
            continue
        if added and progress is not None:
            progress(stitcher.frames, stitcher.height)
        if stitcher.height >= max_height:
            reason = "max_height"
            break
    if reason == "done" and stitcher.frames == 1:
        reason = "end"
    log.info(
        "Manual scrolling capture: %d frames, %d px, %s (%d skipped, %d tolerant matches)",
        stitcher.frames, stitcher.height, reason, skipped, stitcher.fuzzy_matches,
    )
    return stitcher.result(), reason
