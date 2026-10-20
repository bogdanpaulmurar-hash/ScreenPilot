"""Reading videos (info, single frames) and drawing timed annotations on frames.

* MP4 (and anything else FFmpeg reads): ``ffmpeg -i`` for the info, one
  ``ffmpeg -ss t -i … -frames:v 1`` per preview frame (raw BGRA on stdout).
* GIF: Pillow only, so GIF recordings can be edited without FFmpeg.

Annotations are drawn by the editor's Pillow renderer; each shape is rendered
once into a small RGBA patch (cached) and pasted where it belongs, so a frame
costs about the area of its annotations, not of the whole image.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageSequence

from screenpilot.capture.geometry import Rect
from screenpilot.core.errors import RecordingError
from screenpilot.editor.document import Shape, draw_shape, shape_extent
from screenpilot.videoedit.model import TimedAnnotation, VideoInfo

log = logging.getLogger(__name__)

FRAME_TIMEOUT = 30
Runner = Callable[..., "subprocess.CompletedProcess"]

_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?(\d{2,5})x(\d{2,5})")
_FPS = re.compile(r"(\d+(?:\.\d+)?) (?:fps|tbr)")
_AUDIO = re.compile(r"Stream #\d+:\d+.*?: Audio:")


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_bytes(args: Sequence[str], timeout: float = FRAME_TIMEOUT) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(
        list(args), capture_output=True, timeout=timeout, creationflags=_creation_flags(), stdin=subprocess.DEVNULL
    )


def parse_info(stderr: str) -> VideoInfo:
    """Parse ``ffmpeg -hide_banner -i file`` output (it exits with an error: no output given)."""
    duration = _DURATION.search(stderr)
    video = _VIDEO.search(stderr)
    if not duration or not video:
        raise RecordingError("Not a video file ffmpeg can read", message_key="video.error.unreadable")
    h, m, s = duration.groups()
    seconds = int(h) * 3600 + int(m) * 60 + float(s)
    line = stderr[video.start() : stderr.find("\n", video.start()) if "\n" in stderr[video.start() :] else len(stderr)]
    fps_match = _FPS.search(line)
    fps = float(fps_match.group(1)) if fps_match else 30.0
    return VideoInfo(
        width=int(video.group(2)),
        height=int(video.group(3)),
        duration=seconds,
        fps=fps if 0 < fps <= 240 else 30.0,
        has_audio=bool(_AUDIO.search(stderr)),
        codec=video.group(1),
        kind="mp4",
    )


def is_gif(path: Path) -> bool:
    return Path(path).suffix.lower() == ".gif"


def video_info(path: Path, ffmpeg: Path | None, runner: Runner = _run_bytes) -> VideoInfo:
    path = Path(path)
    if not path.exists():
        raise RecordingError(f"{path} does not exist", message_key="video.error.missing")
    if is_gif(path):
        return GifReader(path).info
    if ffmpeg is None:
        raise RecordingError("ffmpeg was not found", message_key="recording.error.no_ffmpeg")
    proc = runner([str(ffmpeg), "-hide_banner", "-i", str(path)], timeout=FRAME_TIMEOUT)
    return parse_info(proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else proc.stderr)


# -- frame readers ----------------------------------------------------------------
class FfmpegFrameReader:
    """One frame at a time, by seeking (fast: decoding starts at the previous keyframe)."""

    def __init__(self, path: Path, ffmpeg: Path, info: VideoInfo, runner: Runner = _run_bytes) -> None:
        self.path, self.ffmpeg, self.info, self.runner = Path(path), ffmpeg, info, runner

    def frame(self, t: float) -> Image.Image:
        t = min(max(0.0, t), max(0.0, self.info.duration - 0.5 / max(self.info.fps, 1)))
        args = [
            str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(self.path),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
        proc = self.runner(args, timeout=FRAME_TIMEOUT)
        size = self.info.width * self.info.height * 3
        data = proc.stdout or b""
        if len(data) < size:
            if t > 0.05:  # past the last decodable frame: show the one just before
                return self.frame(t - 1 / max(self.info.fps, 1) - 0.05)
            raise RecordingError("Could not read a frame", message_key="video.error.unreadable")
        return Image.frombytes("RGB", (self.info.width, self.info.height), data[:size])


class GifReader:
    """GIF frames with their start times; Pillow composes the frames (disposal, patches)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        with Image.open(self.path) as im:
            width, height = im.size
            starts, t = [], 0.0
            for frame in ImageSequence.Iterator(im):
                starts.append(t)
                t += max(frame.info.get("duration", 100), 10) / 1000
        self.starts = starts
        count = max(1, len(starts))
        self.info = VideoInfo(width, height, t, fps=count / t if t else 10.0, kind="gif", codec="gif")
        self._image: Image.Image | None = None
        self._cache: OrderedDict[int, Image.Image] = OrderedDict()

    def index_at(self, t: float) -> int:
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:  # last frame starting at or before t
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= t + 1e-9:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def frame(self, t: float) -> Image.Image:
        index = self.index_at(t)
        with self._lock:
            if index in self._cache:
                self._cache.move_to_end(index)
                return self._cache[index]
            if self._image is None:
                self._image = Image.open(self.path)
            self._image.seek(index)  # Pillow re-reads from the start for earlier frames
            frame = self._image.convert("RGB")
            self._cache[index] = frame
            while len(self._cache) > 12:
                self._cache.popitem(last=False)
            return frame

    def frames(self):
        """All frames in order with their start time (one sequential pass)."""
        with Image.open(self.path) as im:
            for start, frame in zip(self.starts, ImageSequence.Iterator(im)):
                yield start, frame.convert("RGB")

    def close(self) -> None:
        if self._image is not None:
            self._image.close()
            self._image = None


# -- annotations on frames --------------------------------------------------------
class AnnotationPainter:
    """Renders annotation patches once and composites them onto frames."""

    def __init__(self, width: int, height: int, cache_size: int = 256) -> None:
        self.frame_rect = Rect(0, 0, width, height)
        self._cache: OrderedDict[tuple, tuple[Image.Image, Rect] | None] = OrderedDict()
        self._size = cache_size

    def patch(self, shape: Shape, number: int | None) -> tuple[Image.Image, Rect] | None:
        key = (shape, number)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        extent = shape_extent(shape).intersect(self.frame_rect)
        result = None
        if not extent.is_empty:
            overlay = Image.new("RGBA", (extent.width, extent.height), (0, 0, 0, 0))
            draw_shape(overlay, ImageDraw.Draw(overlay), shape.moved(-extent.x, -extent.y), number)
            result = (overlay, extent)
        self._cache[key] = result
        while len(self._cache) > self._size:
            self._cache.popitem(last=False)
        return result

    def compose(self, frame: Image.Image, annotations: Sequence[TimedAnnotation], numbers: dict[int, int]) -> Image.Image:
        """Return ``frame`` (RGB) with the given annotations drawn; the input is not modified."""
        if not annotations:
            return frame
        out = frame.copy()
        for ann in annotations:
            if ann.kind == "blur":
                blur_region(out, ann.shape)
        rgba = None
        for ann in annotations:
            if ann.kind == "blur":
                continue
            patch = self.patch(ann.shape, numbers.get(ann.id))
            if patch is None:
                continue
            image, r = patch
            if rgba is None:
                rgba = out.convert("RGBA")
            rgba.alpha_composite(image, (r.x, r.y))
        return rgba.convert("RGB") if rgba is not None else out


def blur_box(shape: Shape, width: int, height: int) -> Rect:
    return shape.bounds.intersect(Rect(0, 0, width, height))


def blur_radius(shape: Shape) -> int:
    return max(4, shape.strength)


def blur_region(image: Image.Image, shape: Shape) -> None:
    b = blur_box(shape, *image.size)
    if b.is_empty or b.width < 2 or b.height < 2:
        return
    region = image.crop((b.x, b.y, b.right, b.bottom)).filter(ImageFilter.GaussianBlur(blur_radius(shape)))
    image.paste(region, (b.x, b.y))


__all__ = [
    "AnnotationPainter",
    "FfmpegFrameReader",
    "GifReader",
    "blur_region",
    "is_gif",
    "parse_info",
    "video_info",
]
