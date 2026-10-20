"""Built-in animated GIF writer (Pillow only — works without ffmpeg).

Frames are streamed straight to disk, so a GIF recording is no longer limited
by memory and keeps the full screen resolution:

* consecutive identical frames are merged into one longer frame;
* every other frame is stored as a *patch*: only the rectangle that changed
  since the previous frame (a moving cursor is a few dozen pixels), drawn on
  top of the previous frame (GIF disposal 1, "leave in place");
* each patch is quantised to its own 256-colour palette (no dithering, so text
  stays crisp) and LZW-compressed by Pillow's C encoder; the resulting image
  block is copied into our own GIF stream with the patch offset and delay.

Frames are only downscaled when the recorded area is wider than
``max_width`` (``0`` = never), with a high-quality Lanczos filter so text
stays readable. The file is written as ``<name>.gif.part`` and renamed when
the recording finishes; a cancelled recording leaves nothing behind.
"""

from __future__ import annotations

import io
import logging
import struct
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageChops

from screenpilot.core.errors import RecordingError

log = logging.getLogger(__name__)

MAX_FRAMES = 200_000  # distinct frames; at 15 fps this is several hours
MAX_FILE_BYTES = 1536 * 1024 * 1024  # stop before the file becomes unusable
MIN_DELAY_CS = 2  # browsers clamp shorter GIF delays
MIN_DURATION_MS = MIN_DELAY_CS * 10
DISPOSAL_LEAVE = 1
TRAILER = b"\x3b"


def _netscape_loop(loops: int = 0) -> bytes:
    return b"\x21\xff\x0bNETSCAPE2.0\x03\x01" + struct.pack("<H", loops) + b"\x00"


def _graphic_control(delay_cs: int, disposal: int = DISPOSAL_LEAVE) -> bytes:
    return b"\x21\xf9\x04" + bytes([(disposal & 7) << 2]) + struct.pack("<H", max(0, min(65535, delay_cs))) + b"\x00\x00"


def _skip_sub_blocks(data: bytes, pos: int) -> int:
    while data[pos] != 0:
        pos += data[pos] + 1
    return pos + 1


def image_block(image: Image.Image, left: int, top: int) -> bytes:
    """Encode ``image`` (mode ``P``) with Pillow and return a GIF image block at (left, top).

    The block (image descriptor + local colour table + LZW data) is taken from
    a single-frame GIF written by Pillow; only the position and the colour
    table placement are rewritten, the compressed pixels are copied as-is.
    """
    buffer = io.BytesIO()
    image.save(buffer, "GIF", optimize=False, interlace=False)
    data = buffer.getvalue()
    if data[:3] != b"GIF":
        raise RecordingError("Pillow did not produce a GIF frame", message_key="recording.error.generic")
    packed = data[10]
    pos = 13
    table = b""
    table_bits = 0
    if packed & 0x80:  # global colour table
        table_bits = packed & 0x07
        size = 3 * (2 ** (table_bits + 1))
        table = data[pos : pos + size]
        pos += size
    while pos < len(data):
        marker = data[pos]
        if marker == 0x21:  # extension (graphic control, comments): not needed
            pos = _skip_sub_blocks(data, pos + 2)
        elif marker == 0x2C:
            descriptor = data[pos : pos + 10]
            width, height = struct.unpack("<HH", descriptor[5:9])
            local = descriptor[9]
            pos += 10
            if local & 0x80:  # Pillow chose a local table: prefer it
                table_bits = local & 0x07
                size = 3 * (2 ** (table_bits + 1))
                table = data[pos : pos + size]
                pos += size
            start = pos  # LZW minimum code size, then the data sub-blocks
            pos = _skip_sub_blocks(data, pos + 1)
            lzw = data[start:pos]
            flags = (local & 0x40) | ((0x80 | table_bits) if table else 0)  # keep the interlace bit
            header = b"\x2c" + struct.pack("<HHHH", left, top, width, height) + bytes([flags])
            return header + table + lzw
        else:
            break
    raise RecordingError("Unexpected GIF structure from Pillow", message_key="recording.error.generic")


def quantize(image: Image.Image) -> Image.Image:
    """256-colour palette image; exact when the image has at most 256 colours.

    Screen content (UI, text, spreadsheets) usually fits in 256 colours, and
    then the result is lossless. Photos and gradients fall back to a fast
    octree palette without dithering (dithering makes text look noisy).
    """
    if image.getcolors(256) is not None:
        return image.quantize(colors=256, method=Image.Quantize.MAXCOVERAGE, dither=Image.Dither.NONE)
    return image.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)


class PillowGifSink:
    def __init__(self, output: Path, *, width: int, height: int, fps: int, max_width: int = 1920) -> None:
        self.output = output
        self.width, self.height, self.fps = width, height, fps
        scale = min(1.0, max_width / width) if max_width and max_width > 0 else 1.0
        self.size = (max(1, round(width * scale)), max(1, round(height * scale)))
        self.max_frames = MAX_FRAMES
        self.max_bytes = MAX_FILE_BYTES
        self.part = output.with_name(output.name + ".part")
        self._fh: BinaryIO | None = None
        self._previous: Image.Image | None = None
        self._pending: tuple[bytes, float] | None = None  # encoded block, start time
        self._starts: list[float] = []
        self._clock_cs: int | None = None  # GIF timeline already written, in centiseconds
        self._end = 0.0
        self._paused_at: float | None = None
        self.bytes_written = 0
        self.frames_written = 0
        self.frames_merged = 0
        self.frames_dropped = 0
        self.frames_duplicated = 0
        output.parent.mkdir(parents=True, exist_ok=True)

    @property
    def full(self) -> bool:
        return self.frames_written >= self.max_frames or self.bytes_written >= self.max_bytes

    def start(self, ts: float = 0.0) -> None:
        self._end = ts

    def wait_ready(self, timeout: float = 0.0) -> bool:
        return True

    # -- frames -------------------------------------------------------------------
    def _image(self, frame: bytes) -> Image.Image:
        image = Image.frombuffer("RGB", (self.width, self.height), frame, "raw", "BGRX", 0, 1)
        if image.size != self.size:
            image = image.resize(self.size, Image.Resampling.LANCZOS, reducing_gap=2.0)
        return image

    def write(self, frame: bytes, ts: float) -> None:
        image = self._image(frame)
        self._end = ts
        previous = self._previous
        if previous is None:
            box = (0, 0, *self.size)
        else:
            box = ImageChops.difference(previous, image).getbbox()
            if box is None:
                self.frames_merged += 1
                return
        if self.full:
            self.frames_dropped += 1
            return
        patch = image if previous is None else image.crop(box)
        block = image_block(quantize(patch), box[0], box[1])
        self._flush_pending(ts)
        self._pending = (block, ts)
        self._starts.append(ts)
        self._previous = image
        self.frames_written += 1

    def _open(self) -> BinaryIO:
        if self._fh is None:
            fh = open(self.part, "wb")  # noqa: SIM115 - kept open for the whole recording
            w, h = self.size
            # Tiny 2-colour global table: not used by any frame, but some old
            # viewers expect one to exist.
            header = b"GIF89a" + struct.pack("<HH", w, h) + b"\x80\x00\x00" + b"\x00\x00\x00\xff\xff\xff"
            fh.write(header + _netscape_loop(0))
            self.bytes_written += len(header) + 19
            self._fh = fh
        return self._fh

    def _flush_pending(self, end: float) -> None:
        if self._pending is None:
            return
        block, start = self._pending
        self._pending = None
        if self._clock_cs is None:
            self._clock_cs = round(start * 100)
        # Delays come from the cumulative timeline, so rounding never drifts.
        delay = max(MIN_DELAY_CS, round(end * 100) - self._clock_cs)
        self._clock_cs += delay
        data = _graphic_control(delay) + block
        self._open().write(data)
        self.bytes_written += len(data)

    def pause(self) -> None:
        self._paused_at = self._end

    def resume(self, ts: float) -> None:
        # Paused time is already excluded from timestamps by the recorder.
        self._paused_at = None

    def durations(self) -> list[int]:
        """Frame durations in milliseconds (for diagnostics and tests)."""
        if not self._starts:
            return []
        ends = self._starts[1:] + [self._final_end()]
        return [max(MIN_DURATION_MS, round((e - s) * 1000)) for s, e in zip(self._starts, ends)]

    def _final_end(self) -> float:
        return max(self._end, self._starts[-1] + 1 / max(1, self.fps)) if self._starts else self._end

    def finish(self) -> Path:
        if not self._starts:
            self._close()
            self.part.unlink(missing_ok=True)
            raise RecordingError("No frames were recorded", message_key="recording.error.empty")
        self._flush_pending(self._final_end())
        fh = self._open()
        fh.write(TRAILER)
        self.bytes_written += 1
        self._close()
        self.part.replace(self.output)
        log.info(
            "GIF saved: %d frames (%d merged), %dx%d, %.1f MB -> %s",
            self.frames_written, self.frames_merged, *self.size, self.bytes_written / 1048576, self.output,
        )
        self._previous = None
        return self.output

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def abort(self) -> None:
        self._close()
        self._previous = None
        self._pending = None
        self.part.unlink(missing_ok=True)
        self.output.unlink(missing_ok=True)
