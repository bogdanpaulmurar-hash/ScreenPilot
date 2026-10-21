"""Recording loop: grab → effects → sink, on a dedicated thread.

The recorder is UI-agnostic. It publishes ``recording.state`` and
``recording.finished`` / ``recording.failed`` on the event bus; the UI polls
:attr:`Recorder.elapsed` for its timer.

Timing model: timestamps are recording time (wall clock minus paused time).
Sinks turn them into a constant frame rate (MP4) or per-frame durations
(GIF), so slow grabs never make the video play fast.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from screenpilot.capture.geometry import Point, Rect
from screenpilot.core import diagnostics as diag
from screenpilot.core.errors import RecordingError, ScreenPilotError
from screenpilot.recording.effects import EffectOptions, EffectRenderer
from screenpilot.recording.ffmpeg import even
from screenpilot.recording.ink import InkLayer
from screenpilot.recording.options import RecordingOptions
from screenpilot.recording.source import FrameSource

log = logging.getLogger(__name__)

READY_TIMEOUT = 10.0
SALVAGE_AFTER_S = 5  # same window as the UI's one-time encoder retry


class Sink(Protocol):
    output: Path
    frames_written: int
    frames_dropped: int
    frames_duplicated: int

    def start(self, ts: float = 0.0) -> None: ...

    def wait_ready(self, timeout: float = ...) -> bool: ...

    def write(self, frame: bytes, ts: float) -> None: ...

    def pause(self) -> None: ...

    def resume(self, ts: float) -> None: ...

    def finish(self) -> Path: ...

    def abort(self) -> None: ...


SinkFactory = Callable[[int, int], Sink]


class State(str, enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    FINISHING = "finishing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class RecordingResult:
    path: Path
    format: str
    rect: Rect
    duration: float
    reason: str
    frames_captured: int
    frames_written: int
    frames_duplicated: int
    frames_dropped: int
    avg_grab_ms: float
    avg_effects_ms: float
    encoder: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    @property
    def capture_fps(self) -> float:
        return self.frames_captured / self.duration if self.duration > 0 else 0.0


class Recorder:
    def __init__(
        self,
        source: FrameSource,
        sink_factory: SinkFactory,
        options: RecordingOptions,
        *,
        bus=None,
        clock: Callable[[], float] = time.monotonic,
        encoder: str = "",
    ) -> None:
        self.source = source
        self.sink_factory = sink_factory
        self.options = options
        self.bus = bus
        self.clock = clock
        self.encoder = encoder
        self.effects = EffectRenderer(
            EffectOptions(
                highlight=options.highlight_cursor,
                clicks=options.click_effects,
                zoom_factor=options.zoom_factor,
            )
        )
        self.ink: InkLayer | None = None  # created by start() once the area is known
        self.state = State.IDLE
        self.result: RecordingResult | None = None
        self.error: ScreenPilotError | None = None
        self.rect = Rect(0, 0, 0, 0)
        self.sink: Sink | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cancel = False
        self._pause_requested = False
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._origin = 0.0
        self._paused_total = 0.0
        self._pause_started: float | None = None
        self._frames = 0
        self._grab_ms = 0.0
        self._fx_ms = 0.0
        self._reason = "stopped"
        self._done = threading.Event()

    # -- public API ---------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        if self.state in (State.IDLE,):
            return 0.0
        if self.result is not None:
            return self.result.duration
        now = self._pause_started if self._pause_started is not None else self.clock()
        return max(0.0, now - self._origin - self._paused_total)

    @property
    def active(self) -> bool:
        return self.state in (State.RECORDING, State.PAUSED, State.FINISHING)

    def start(self, rect: Rect) -> None:
        if self.state != State.IDLE:
            raise RecordingError("Recorder already used", message_key="recording.error.busy")
        self.rect = Rect(rect.x, rect.y, even(rect.width), even(rect.height))
        if self.rect.width < 16 or self.rect.height < 16:
            raise RecordingError("Recording area is too small", message_key="recording.error.too_small")
        self.ink = InkLayer(
            self.rect.width,
            self.rect.height,
            origin=Point(self.rect.x, self.rect.y),
            color=self.options.ink_color,
            fade_seconds=self.options.ink_fade_seconds,
        )
        self.effects.ink = self.ink
        self.source.open(self.rect)
        try:
            self.sink = self.sink_factory(self.rect.width, self.rect.height)
            self.sink.start(0.0)
        except Exception:
            self.source.close()
            raise
        self._set_state(State.RECORDING)
        log.info(
            "Recording started: %s, %s at %d fps, encoder=%s, audio=%s",
            self.rect, self.options.format, self.options.frame_rate, self.encoder or "-",
            bool(self.options.has_audio),
        )
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self.state == State.RECORDING:
                self._pause_requested = True
                self._wake.set()

    def resume(self) -> None:
        with self._lock:
            if self.state == State.PAUSED or self._pause_requested:
                self._pause_requested = False
                self._wake.set()

    def toggle_pause(self) -> None:
        if self.state == State.PAUSED or self._pause_requested:
            self.resume()
        else:
            self.pause()

    def toggle_zoom(self) -> bool:
        return self.effects.toggle_zoom()

    def stop(self, *, wait: bool = False, timeout: float | None = None) -> RecordingResult | None:
        self._stop.set()
        self._wake.set()
        if wait:
            self.join(timeout)
        return self.result

    def cancel(self) -> None:
        self._cancel = True
        self.stop()

    def join(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    # -- internals ----------------------------------------------------------------
    def _set_state(self, state: State) -> None:
        self.state = state
        if self.bus is not None:
            self.bus.publish("recording.state", state=state.value, elapsed=self.elapsed)

    def _ts(self) -> float:
        return self.clock() - self._origin - self._paused_total

    def _frame(self, ts: float) -> bytes:
        t0 = time.perf_counter()
        frame = self.source.grab()
        t1 = time.perf_counter()
        cursor = self.source.cursor()
        self.effects.feed_buttons(self.source.buttons(), cursor, ts)
        frame = self.effects.apply(frame, self.rect.width, self.rect.height, cursor, ts)
        self._grab_ms += (t1 - t0) * 1000
        self._fx_ms += (time.perf_counter() - t1) * 1000
        self._frames += 1
        return frame

    def _prime(self, ts: float) -> None:
        """Write the first frame of a segment and wait until the encoder accepts it."""
        sink = self.sink
        started = self.clock()
        sink.write(self._frame(ts), ts)
        if not sink.wait_ready(READY_TIMEOUT):
            raise RecordingError("The encoder did not start in time", message_key="recording.error.encoder_failed")
        # Encoder start-up (opening audio devices) must not count as recording time.
        self._paused_total += self.clock() - started

    def _run(self) -> None:
        opts = self.options
        interval = 1.0 / opts.frame_rate
        limit = opts.max_minutes * 60
        sink = self.sink
        try:
            self._origin = self.clock()
            self._prime(0.0)
            while not self._stop.is_set():
                if self._pause_requested:
                    self._do_pause(sink)
                    continue
                ts = self._ts()
                if ts >= limit:
                    self._reason = "max_duration"
                    break
                sink.write(self._frame(ts), ts)
                if getattr(sink, "full", False):
                    self._reason = "max_frames"
                    break
                next_ts = (int(ts / interval) + 1) * interval
                delay = next_ts - self._ts()
                if delay > 0:
                    self._wake.wait(delay)
                    self._wake.clear()
            self._finish(sink)
        except ScreenPilotError as exc:
            self._fail(sink, exc)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            log.exception("Recording crashed")
            self._fail(sink, RecordingError(f"Recording failed: {exc}", message_key="recording.error.generic"))
        finally:
            self.source.close()
            self._done.set()

    def _do_pause(self, sink: Sink) -> None:
        self._pause_started = self.clock()
        sink.pause()
        self._set_state(State.PAUSED)
        log.info("Recording paused at %.1fs", self._pause_started - self._origin - self._paused_total)
        while self._pause_requested and not self._stop.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
        self._paused_total += self.clock() - self._pause_started
        self._pause_started = None
        if self._stop.is_set():
            return
        ts = self._ts()
        sink.resume(ts)
        self._set_state(State.RECORDING)
        self._prime(ts)
        log.info("Recording resumed at %.1fs", ts)

    def _finish(self, sink: Sink) -> None:
        if self._cancel:
            sink.abort()
            self._set_state(State.CANCELLED)
            log.info("Recording cancelled")
            if self.bus is not None:
                self.bus.publish("recording.cancelled")
            return
        duration = self._ts() if self._pause_started is None else self._pause_started - self._origin - self._paused_total
        self._pause_started = None
        self._set_state(State.FINISHING)
        started = time.perf_counter()
        path = sink.finish()
        finish_ms = (time.perf_counter() - started) * 1000
        frames = max(self._frames, 1)
        self.result = RecordingResult(
            path=path,
            format=self.options.format,
            rect=self.rect,
            duration=max(duration, 0.0),
            reason=self._reason,
            frames_captured=self._frames,
            frames_written=sink.frames_written,
            frames_duplicated=sink.frames_duplicated,
            frames_dropped=sink.frames_dropped,
            avg_grab_ms=self._grab_ms / frames,
            avg_effects_ms=self._fx_ms / frames,
            encoder=self.encoder,
        )
        r = self.result
        diag.current().record(
            "recording.session",
            r.duration * 1000,
            format=r.format,
            size=f"{r.rect.width}x{r.rect.height}",
            target_fps=self.options.frame_rate,
            capture_fps=round(r.capture_fps, 1),
            grab_ms=round(r.avg_grab_ms, 1),
            effects_ms=round(r.avg_effects_ms, 1),
            duplicated=r.frames_duplicated,
            dropped=r.frames_dropped,
            finish_ms=round(finish_ms),
            encoder=r.encoder,
            reason=r.reason,
        )
        log.info(
            "Recording saved: %s (%.1fs, %d frames captured, %.1f fps, %d duplicated, %d dropped, grab %.1f ms, effects %.1f ms)",
            path, r.duration, r.frames_captured, r.capture_fps, r.frames_duplicated, r.frames_dropped,
            r.avg_grab_ms, r.avg_effects_ms,
        )
        self._set_state(State.DONE)
        if self.bus is not None:
            self.bus.publish("recording.finished", result=r)

    def _fail(self, sink: Sink | None, exc: ScreenPilotError) -> None:
        log.error("Recording failed: %s", exc)
        self.error = exc
        self.partial: Path | None = None
        # Past the first seconds (an encoder retry would restart from scratch), keep what was
        # recorded when the encoder died: that is minutes of work the user does not want to lose.
        keep = not self._cancel and self._frames > self.options.frame_rate * SALVAGE_AFTER_S
        if sink is not None:
            try:
                if keep and hasattr(sink, "salvage"):
                    self.partial = sink.salvage()
                    if self.partial is not None:
                        log.info("Kept the recording up to the failure: %s", self.partial)
                if self.partial is None:
                    sink.abort()
            except Exception:  # noqa: BLE001
                log.exception("Cleaning up the failed recording failed")
        self._set_state(State.FAILED)
        if self.bus is not None:
            self.bus.publish("recording.failed", error=exc, partial=self.partial, seconds=self.elapsed)
