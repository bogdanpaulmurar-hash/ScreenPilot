"""Capture Engine public API.

This is the stable, UI-independent entry point used by the desktop UI, the CLI
and external automation (Autopilot). Every successful capture publishes a
``capture.completed`` event carrying the :class:`CaptureResult`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from screenpilot.capture.backend import CaptureBackend
from screenpilot.capture.geometry import Monitor, Point, Rect, VirtualDesktop, WindowInfo
from screenpilot.capture.image import CaptureResult
from screenpilot.core import diagnostics
from screenpilot.core.errors import CaptureError
from screenpilot.core.events import EventBus

log = logging.getLogger(__name__)

EVENT_COMPLETED = "capture.completed"
EVENT_FAILED = "capture.failed"
EVENT_COUNTDOWN = "capture.countdown"
EVENT_CANCELLED = "capture.cancelled"

MODE_REGION = "region"
MODE_WINDOW = "window"
MODE_MONITOR = "monitor"
MODE_CURRENT_MONITOR = "current_monitor"
MODE_ALL_MONITORS = "all_monitors"
MODE_SCROLLING = "scrolling"
EVENT_SCROLL_PROGRESS = "capture.scroll_progress"

DELAY_PRESETS = (3, 5, 10)
MAX_DELAY_SECONDS = 600


@dataclass(frozen=True)
class CaptureRequest:
    """A repeatable description of a capture (used for 'repeat last capture')."""

    mode: str
    params: dict[str, Any] = field(default_factory=dict)


class CaptureService:
    def __init__(
        self,
        backend: CaptureBackend,
        bus: EventBus | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.backend = backend
        self.bus = bus or EventBus()
        self._sleep = sleep
        self._lock = threading.Lock()
        self.last_request: CaptureRequest | None = None

    # -- discovery ------------------------------------------------------------
    def desktop(self) -> VirtualDesktop:
        monitors = self.backend.monitors()
        if not monitors:
            raise CaptureError("No monitors detected")
        return VirtualDesktop(monitors)

    def monitors(self) -> list[Monitor]:
        return list(self.desktop().monitors)

    def windows(self) -> list[WindowInfo]:
        return list(self.backend.list_windows())

    # -- capture modes --------------------------------------------------------
    def capture_region(self, rect: Rect) -> CaptureResult:
        desktop = self.desktop()
        area = desktop.clamp(rect)
        if area.is_empty or not desktop.is_visible(area):
            raise CaptureError(f"Region {rect} is outside all monitors", message_key="error.capture")
        return self._grab(MODE_REGION, area, CaptureRequest(MODE_REGION, {"rect": str(rect)}))

    def capture_monitor(self, index: int) -> CaptureResult:
        try:
            monitor = self.desktop().monitor(index)
        except IndexError as exc:
            raise CaptureError(str(exc)) from exc
        return self._grab(
            MODE_MONITOR, monitor.bounds, CaptureRequest(MODE_MONITOR, {"index": index}), monitor=monitor.index
        )

    def capture_current_monitor(self) -> CaptureResult:
        monitor = self.desktop().monitor_at(self.backend.cursor_position())
        return self._grab(
            MODE_CURRENT_MONITOR, monitor.bounds, CaptureRequest(MODE_CURRENT_MONITOR), monitor=monitor.index
        )

    def capture_all_monitors(self) -> CaptureResult:
        desktop = self.desktop()
        return self._grab(MODE_ALL_MONITORS, desktop.bounds, CaptureRequest(MODE_ALL_MONITORS))

    # -- freeze-then-select (used by the interactive region selector) --------
    def freeze(self) -> CaptureResult:
        """Grab the whole desktop *without* publishing it as a capture.

        The selector shows this frozen image, so the user selects exactly what
        was on screen when they pressed the hotkey (open menus, tooltips...).
        """
        desktop = self.desktop()
        with diagnostics.timed("capture.freeze", width=desktop.bounds.width, height=desktop.bounds.height):
            with self._lock:
                image = self.backend.grab(desktop.bounds)
        return CaptureResult(image=image, mode="frozen", region=desktop.bounds, metadata={"backend": self.backend.name})

    def region_from_frozen(self, frozen: CaptureResult, rect: Rect, *, mode: str = MODE_REGION, **metadata: Any) -> CaptureResult:
        """Crop a desktop rectangle out of a frozen image and publish it as a capture."""
        area = rect.intersect(frozen.region)
        if area.is_empty:
            raise CaptureError(f"Selection {rect} is outside the captured desktop")
        image = frozen.image.crop(area.translate(-frozen.region.x, -frozen.region.y))
        metadata.setdefault("backend", frozen.metadata.get("backend", self.backend.name))
        metadata["frozen"] = True
        result = CaptureResult(image=image, mode=mode, region=area, metadata=metadata)
        self.last_request = CaptureRequest(MODE_REGION, {"rect": str(area)})
        log.info("Captured %s %dx%d at %s from frozen desktop", mode, area.width, area.height, area)
        self.bus.publish(EVENT_COMPLETED, result=result)
        return result

    def capture_window(self, window: WindowInfo | None = None) -> CaptureResult:
        """Capture a window as currently visible on screen (foreground window by default)."""
        window = window or self.backend.foreground_window()
        if window is None:
            raise CaptureError("No window to capture")
        area = self.desktop().clamp(window.bounds)
        if area.is_empty:
            raise CaptureError(f"Window {window.title!r} is off-screen")
        return self._grab(
            MODE_WINDOW,
            area,
            CaptureRequest(MODE_WINDOW),
            window_title=window.title,
            window_class=window.class_name,
        )

    def capture_window_at(self, point: Point, *, include_children: bool = False) -> CaptureResult:
        window = self.backend.window_at(point, include_children=include_children)
        if window is None:
            raise CaptureError(f"No window at {point}")
        return self.capture_window(window)

    # -- scrolling ------------------------------------------------------------
    def capture_scrolling(self, rect: Rect, scroller, *, manual: bool = False, **options: Any) -> tuple[CaptureResult | None, str]:
        """Scroll ``rect`` with ``scroller`` and stitch one tall capture.

        Blocking: run from a worker thread. Returns (result, reason); see
        :func:`screenpilot.capture.scrolling.capture_scrolling` for reasons.
        The result is ``None`` when the user cancelled (``reason == "cancelled"``).
        """
        from screenpilot.capture.scrolling import capture_scrolling, capture_scrolling_manual

        area = self.desktop().clamp(rect)
        if area.is_empty:
            raise CaptureError(f"Region {rect} is outside all monitors")

        def grab(r: Rect):
            with self._lock:
                return self.backend.grab(r)

        def progress(frames: int, height: int) -> None:
            self.bus.publish(EVENT_SCROLL_PROGRESS, frames=frames, height=height)

        if manual:  # the user scrolls; Esc finishes (no pause question)
            options.pop("on_stop", None)
            options.pop("settle", None)
            image, reason = capture_scrolling_manual(grab, area, scroller, progress=progress, **options)
        else:
            image, reason = capture_scrolling(grab, area, scroller, progress=progress, **options)
        if reason == "cancelled":
            self.bus.publish(EVENT_CANCELLED, mode=MODE_SCROLLING)
            return None, reason
        result = CaptureResult(
            image=image,
            mode=MODE_SCROLLING,
            region=area,
            metadata={"backend": self.backend.name, "scroll_end": reason, "view_height": area.height},
        )
        self.last_request = CaptureRequest(MODE_REGION, {"rect": str(area)})
        self.bus.publish(EVENT_COMPLETED, result=result)
        return result, reason

    # -- delayed --------------------------------------------------------------
    def capture_delayed(
        self,
        seconds: int,
        capture: Callable[[], CaptureResult],
        *,
        cancel: threading.Event | None = None,
    ) -> CaptureResult | None:
        """Wait ``seconds`` (publishing a countdown each second), then run ``capture``.

        Blocking: call from a worker thread. Returns ``None`` if cancelled.
        """
        if not 0 <= seconds <= MAX_DELAY_SECONDS:
            raise CaptureError(f"Delay must be between 0 and {MAX_DELAY_SECONDS} seconds")
        cancel = cancel or threading.Event()
        for remaining in range(seconds, 0, -1):
            if cancel.is_set():
                break
            self.bus.publish(EVENT_COUNTDOWN, seconds=remaining)
            self._sleep(1)
        if cancel.is_set():
            log.info("Delayed capture cancelled")
            self.bus.publish(EVENT_CANCELLED)
            return None
        return capture()

    # -- repeat ---------------------------------------------------------------
    def repeat_last(self) -> CaptureResult:
        request = self.last_request
        if request is None:
            raise CaptureError("There is no previous capture to repeat")
        if request.mode == MODE_REGION:
            return self.capture_region(Rect.parse(request.params["rect"]))
        if request.mode == MODE_MONITOR:
            return self.capture_monitor(int(request.params["index"]))
        if request.mode == MODE_CURRENT_MONITOR:
            return self.capture_current_monitor()
        if request.mode == MODE_ALL_MONITORS:
            return self.capture_all_monitors()
        return self.capture_window()

    # -- internals ------------------------------------------------------------
    def _grab(self, mode: str, rect: Rect, request: CaptureRequest, **metadata: Any) -> CaptureResult:
        started = time.perf_counter()
        try:
            with self._lock:
                image = self.backend.grab(rect)
        except CaptureError as exc:
            log.error("Capture failed (%s %s): %s", mode, rect, exc)
            self.bus.publish(EVENT_FAILED, mode=mode, error=exc)
            raise
        except Exception as exc:  # noqa: BLE001 - normalise unexpected backend failures
            log.exception("Unexpected capture failure (%s %s)", mode, rect)
            self.bus.publish(EVENT_FAILED, mode=mode, error=exc)
            raise CaptureError(f"Unexpected capture failure: {exc}") from exc
        elapsed_ms = (time.perf_counter() - started) * 1000
        diagnostics.current().record("capture.grab", elapsed_ms, mode=mode, width=rect.width, height=rect.height)
        log.info("Captured %s %dx%d at %s in %.1f ms", mode, rect.width, rect.height, rect, elapsed_ms)
        metadata.setdefault("backend", self.backend.name)
        metadata["elapsed_ms"] = round(elapsed_ms, 1)
        result = CaptureResult(image=image, mode=mode, region=rect, metadata=metadata)
        self.last_request = request
        self.bus.publish(EVENT_COMPLETED, result=result)
        return result
