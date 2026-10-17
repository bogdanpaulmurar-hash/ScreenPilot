"""Built-in animated GIF writer (Pillow only — works without ffmpeg).

Screen recordings contain long stretches of identical frames, so consecutive
duplicates are merged into one frame with a longer duration. Frames are
downscaled to ``max_width`` and quantised to a 256-colour palette as they
arrive (about 1 byte per pixel), and the number of frames held in memory is
capped; when the cap is reached the recorder stops with ``max_frames``.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from PIL import Image

from screenpilot.core.errors import RecordingError

log = logging.getLogger(__name__)

MEMORY_BUDGET = 300 * 1024 * 1024  # bytes of palette frames kept before stopping
MIN_DURATION_MS = 20  # browsers clamp shorter GIF delays


class PillowGifSink:
    def __init__(self, output: Path, *, width: int, height: int, fps: int, max_width: int = 960) -> None:
        self.output = output
        self.width, self.height, self.fps = width, height, fps
        scale = min(1.0, max_width / width)
        self.size = (max(1, round(width * scale)), max(1, round(height * scale)))
        self.max_frames = max(10, MEMORY_BUDGET // (self.size[0] * self.size[1]))
        self._frames: list[Image.Image] = []
        self._starts: list[float] = []
        self._last_hash: bytes | None = None
        self._end = 0.0
        self._paused_at: float | None = None
        self.frames_written = 0
        self.frames_merged = 0
        self.frames_dropped = 0
        self.frames_duplicated = 0
        output.parent.mkdir(parents=True, exist_ok=True)

    @property
    def full(self) -> bool:
        return len(self._frames) >= self.max_frames

    def start(self, ts: float = 0.0) -> None:
        self._end = ts

    def wait_ready(self, timeout: float = 0.0) -> bool:
        return True

    def write(self, frame: bytes, ts: float) -> None:
        image = Image.frombuffer("RGB", (self.width, self.height), frame, "raw", "BGRX", 0, 1)
        if image.size != self.size:
            image = image.resize(self.size, Image.Resampling.BILINEAR)
        digest = hashlib.blake2b(image.tobytes(), digest_size=16).digest()
        self._end = ts
        if digest == self._last_hash:
            self.frames_merged += 1
            return
        if self.full:
            self.frames_dropped += 1
            return
        self._last_hash = digest
        self._frames.append(image.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE))
        self._starts.append(ts)
        self.frames_written += 1

    def pause(self) -> None:
        self._paused_at = self._end

    def resume(self, ts: float) -> None:
        # Paused time is already excluded from timestamps by the recorder.
        self._paused_at = None

    def durations(self) -> list[int]:
        ends = self._starts[1:] + [max(self._end, self._starts[-1] + 1 / self.fps)]
        return [max(MIN_DURATION_MS, round((e - s) * 1000)) for s, e in zip(self._starts, ends)]

    def finish(self) -> Path:
        if not self._frames:
            raise RecordingError("No frames were recorded", message_key="recording.error.empty")
        first, rest = self._frames[0], self._frames[1:]
        first.save(
            self.output,
            "GIF",
            save_all=True,
            append_images=rest,
            duration=self.durations(),
            loop=0,
            optimize=False,
            disposal=1,
        )
        log.info("GIF saved: %d frames (%d merged) -> %s", len(self._frames), self.frames_merged, self.output)
        self._frames.clear()
        return self.output

    def abort(self) -> None:
        self._frames.clear()
        self.output.unlink(missing_ok=True)
