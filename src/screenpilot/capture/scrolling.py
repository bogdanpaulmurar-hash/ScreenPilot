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
HOVER_MIN_MATCH = 0.5  # with only hover runs differing (see _hover_only)
HOVER_BAND = 48  # a hovered list row is at most this tall; two runs of up to twice this may differ
FUZZY_MIN_MATCH = 0.75  # share of informative rows that must agree in the tolerant match
FUZZY_MIN_ROWS = 12  # fewer informative rows than this: too little content to match safely
PROFILE_SAMPLES = 160  # pixels sampled per row for the tolerant profile
DARK = bytes(1 if v < 128 else 0 for v in range(256))
DEFAULT_MAX_HEIGHT = 30_000
DEFAULT_MAX_FRAMES = 80


class StitchError(Exception):
    pass


CARET_MAX_PX = 6  # a row that differs only within this many pixels is "the same" (blinking text caret)


def _rows_close(a: bytes, b: bytes) -> bool:
    if a == b:
        return True
    if len(a) != len(b):
        return False
    diff = int.from_bytes(a, "big") ^ int.from_bytes(b, "big")
    low = (diff & -diff).bit_length() - 1  # lowest differing bit
    return (diff.bit_length() - low + 7) // 8 <= CARET_MAX_PX * BYTES_PER_PIXEL


def frames_close(a: CapturedImage, b: CapturedImage) -> bool:
    """Two grabs show the same view, a blinking caret aside (it never lets a view "stand still")."""
    if a.bgra == b.bgra:
        return True
    if (a.width, a.height) != (b.width, b.height):
        return False
    stride = a.stride
    for y in range(a.height):
        ra, rb = a.bgra[y * stride : (y + 1) * stride], b.bgra[y * stride : (y + 1) * stride]
        if ra != rb and not _rows_close(ra, rb):
            return False
    return True


def row_signatures(image: CapturedImage) -> list[bytes]:
    stride = image.stride
    x1 = int(image.width * (1 - SCROLLBAR_IGNORE)) * BYTES_PER_PIXEL
    data = image.bgra
    return [data[y * stride : y * stride + x1] for y in range(image.height)]


def _votes(prev: list[bytes], curr: list[bytes]) -> "Counter[int]":
    prev_count = Counter(prev)
    curr_count = Counter(curr)
    prev_pos = {sig: i for i, sig in enumerate(prev) if prev_count[sig] == 1}
    votes: Counter[int] = Counter()
    for i, sig in enumerate(curr):
        if curr_count[sig] == 1:
            j = prev_pos.get(sig)
            if j is not None and j > i:
                votes[j - i] += 1
    return votes


OVERLAY_MIN_VOTES = 8
OVERLAY_MIN_ROWS = 12  # fewer differing rows than this: not a box
OVERLAY_MIN_DENSITY = 0.35  # share of rows in the box band that differ (its interior may be blank)
OVERLAY_OUTSIDE_MAX = 0.05  # differing rows allowed outside the band, as a share of the overlap


def tail_overlay(prev: list[bytes], curr: list[bytes], shift: int, cap: int) -> int:
    """Height of a floating box over the bottom of the view (chat input, toast, cookie bar).

    The page scrolls under such a box, so it is no sticky footer (content shows beside
    and below it) and, taken as new content, it would be pasted into the picture at every
    step. Given the scroll ``shift`` (voted by unique rows), the rows that disagree
    between the two frames are then the previous frame's box: one block that ends where
    the compared range ends. Returns its height (0 if the differences look like anything else).
    """
    n = len(prev)
    overlap = n - shift
    if overlap <= 0:
        return 0
    differing = [k for k in range(overlap) if prev[shift + k] != curr[k]]
    inside = [k for k in differing if k >= overlap - cap]
    outside = len(differing) - len(inside)
    if len(inside) < OVERLAY_MIN_ROWS or outside > OVERLAY_OUTSIDE_MAX * overlap:
        return 0
    band = overlap - min(inside)
    if len(inside) / band < OVERLAY_MIN_DENSITY:
        return 0
    return band


def find_shift(prev: list[bytes], curr: list[bytes], hover: bool = True) -> int | None:
    """Rows scrolled between two equally sized middle segments (None if no reliable match).

    A shift is accepted when almost all overlapping rows are identical, or when
    the rows that differ are only the hovered list item before and after the
    scroll (:func:`_hover_only`): Explorer, Outlook and most lists highlight
    the row under the mouse, and that row changes with every scroll.
    """
    n = len(prev)
    if n == 0 or prev == curr:
        return 0
    votes = _votes(prev, curr)
    for shift, _ in votes.most_common(5):
        overlap = n - shift
        if overlap <= 0:
            continue
        rows = range(overlap)
        same = sum(1 for k in rows if prev[shift + k] == curr[k])
        if same / overlap >= MIN_MATCH:
            return shift
        if hover and same / overlap >= HOVER_MIN_MATCH and _hover_only(prev, curr, shift, rows):
            return shift
    return None


def _hover_only(prev: list[bytes], curr: list[bytes], shift: int, rows) -> bool:
    """The rows that differ form at most two short runs: the hovered item before and after
    the scroll (the mouse rests on a list while the user scrolls by hand)."""
    runs: list[int] = []
    last = None
    for k in rows:
        if prev[shift + k] != curr[k]:
            if last is not None and k - last <= 3:  # small gaps (grid lines) join a run
                runs[-1] += k - last
            else:
                runs.append(1)
            last = k
    return len(runs) <= 2 and all(r <= 2 * HOVER_BAND for r in runs)


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
        self._footer = 0  # rows of a floating box (and the gap below it) seen at the bottom of the view
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
        shift = None
        if self._footer > bottom:  # a floating box was seen before: try with it left out first
            shift = find_shift(prev[top : h - self._footer], sig[top : h - self._footer])
            if shift is not None:
                bottom = self._footer
            else:
                self._footer = 0
        if shift is None:
            shift = find_shift(prev[top : h - bottom], sig[top : h - bottom], hover=False)
        if shift is None:
            votes = _votes(prev[top : h - bottom], sig[top : h - bottom])
            if votes and votes.most_common(1)[0][1] >= OVERLAY_MIN_VOTES:
                guess = votes.most_common(1)[0][0]
                box = tail_overlay(prev[top : h - bottom], sig[top : h - bottom], guess, cap)
                if box:
                    total = bottom + box
                    shift = find_shift(prev[top : h - total], sig[top : h - total])
                    if shift is not None:
                        bottom = self._footer = total
        if shift is None:
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
        if frames_close(nxt, frame):
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
    finish: Callable[[], bool] | None = None,
    hold: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[CapturedImage, str]:
    """Scroll ``rect`` until the content stops moving; returns (image, reason).

    reason is one of "end", "stopped", "cancelled", "max_height", "max_frames",
    "no_match".

    When the scroller reports a stop request (Esc), ``on_stop`` decides what
    happens while scrolling is paused: ``"continue"``, ``"stop"`` (keep what
    was captured) or ``"cancel"`` (discard). Without it, Esc stops.

    ``finish`` (the OK button) stops at once and keeps what was captured,
    without the pause question. While ``hold`` is true (the mouse is over the
    OK button) no scroll is sent, so the cursor is not moved away from it.
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
        while hold is not None and hold() and not (finish is not None and finish()) and not scroller.stop_requested():
            sleep(0.05)
        if finish is not None and finish():
            reason = "stopped"
            break
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
    finish: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[CapturedImage, str]:
    """The user scrolls (wheel, keys, scrollbar); frames are stitched as they come. Esc or OK finishes.

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
        if scroller.stop_requested() or (finish is not None and finish()):
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
