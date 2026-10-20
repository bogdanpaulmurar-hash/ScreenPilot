"""Export an edited video: trimmed, with annotations burned in for their time range.

MP4 — one FFmpeg pass:

* the source is cut with input seeking (``-ss`` / ``-t`` before ``-i``) and
  re-encoded, so the cut is frame-exact (stream copy can only cut at
  keyframes, every ~2 s in our recordings);
* every vector annotation is a small PNG input placed with
  ``overlay=x:y:enable='between(t,a,b)'``; PNGs are converted to YUV with the
  BT.709 matrix so colours match the video;
* blur = ``crop`` + ``boxblur`` of the area, overlaid for its time range;
* audio is re-encoded to AAC (cut together with the video).

GIF — Pillow only (no FFmpeg): frames are read in order, annotated and
written with the streaming :class:`~screenpilot.recording.gif.PillowGifSink`.

The output is written to ``<name>.part`` and renamed when complete, so a
cancelled or failed export never leaves a broken file; the source is never
modified.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from screenpilot.core.errors import RecordingError
from screenpilot.recording.ffmpeg import COLOR_TAGS, FfmpegInfo, encoder_args, even
from screenpilot.recording.gif import PillowGifSink
from screenpilot.videoedit.media import AnnotationPainter, GifReader, blur_box, blur_radius
from screenpilot.videoedit.model import TimedAnnotation, VideoProject

log = logging.getLogger(__name__)

Progress = Callable[[float], None]  # 0.0 … 1.0
EXPORT_TIMEOUT = 6 * 3600


class ExportCancelled(RecordingError):
    def __init__(self) -> None:
        super().__init__("Export cancelled", message_key="video.export.cancelled")


def _fmt(t: float) -> str:
    return f"{max(0.0, t):.3f}"


def build_filter(
    annotations: list[tuple[TimedAnnotation, int | None]],
    overlays: list[tuple[int, int]],
    width: int,
    height: int,
    *,
    first_input: int = 1,
) -> str:
    """``filter_complex`` for the video: blur regions, then PNG overlays, then 4:2:0.

    ``annotations`` are (annotation with output-relative times, overlay index
    or None for blur); ``overlays`` holds the (x, y) of each PNG input.
    """
    parts = ["[0:v]setpts=PTS-STARTPTS[v0]"]
    label, n = "v0", 0

    def next_label() -> str:
        nonlocal n
        n += 1
        return f"v{n}"

    for ann, _ in annotations:
        if ann.kind != "blur":
            continue
        b = blur_box(ann.shape, width, height)
        if b.width < 2 or b.height < 2:
            continue
        w, h = even(b.width), even(b.height)
        radius = min(blur_radius(ann.shape), max(1, min(w, h) // 2 - 1))
        main, crop, blurred, out = f"m{n}", f"c{n}", f"b{n}", next_label()
        enable = f"between(t,{_fmt(ann.start)},{_fmt(ann.end)})"
        parts.append(f"[{label}]split[{main}][{crop}]")
        parts.append(f"[{crop}]crop={w}:{h}:{b.x}:{b.y},boxblur={radius}:2[{blurred}]")
        parts.append(f"[{main}][{blurred}]overlay={b.x}:{b.y}:enable='{enable}'[{out}]")
        label = out
    for ann, index in annotations:
        if index is None:
            continue
        x, y = overlays[index]
        src, conv, out = first_input + index, f"p{index}", next_label()
        enable = f"between(t,{_fmt(ann.start)},{_fmt(ann.end)})"
        parts.append(f"[{src}:v]scale=out_color_matrix=bt709:out_range=tv,format=yuva420p[{conv}]")
        parts.append(f"[{label}][{conv}]overlay={x}:{y}:enable='{enable}':format=yuv420[{out}]")
        label = out
    parts.append(f"[{label}]format=yuv420p[vout]")
    return ";".join(parts)


def export_command(
    ffmpeg: Path,
    project: VideoProject,
    output: Path,
    png_inputs: list[Path],
    filter_graph: str,
    *,
    encoder: str,
    quality: int,
) -> list[str]:
    info = project.info
    fps = max(1, round(info.fps))
    args = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
        "-ss", _fmt(project.start), "-t", _fmt(project.output_duration), "-i", str(project.source),
    ]
    for png in png_inputs:
        args += ["-i", str(png)]
    args += ["-filter_complex", filter_graph, "-map", "[vout]", "-map", "0:a?"]
    args += encoder_args(encoder, quality, fps, info.width, info.height)
    args += COLOR_TAGS
    args += ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-f", "mp4", str(output)]
    return args


def parse_progress(line: str, total: float) -> float | None:
    """``out_time_us=…`` / ``out_time_ms=…`` lines of ``-progress`` (both are microseconds)."""
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms") and value.strip().lstrip("-").isdigit() and total > 0:
        return min(1.0, max(0.0, int(value) / 1_000_000 / total))
    if key == "progress" and value.strip() == "end":
        return 1.0
    return None


def part_path(output: Path) -> Path:
    return output.with_name(output.name + ".part")


def export_mp4(
    info: FfmpegInfo,
    project: VideoProject,
    output: Path,
    *,
    encoder: str,
    quality: int = 20,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> Path:
    numbers = project.step_numbers()
    painter = AnnotationPainter(project.info.width, project.info.height)
    part = part_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="screenpilot-video-") as tmp:
        entries: list[tuple[TimedAnnotation, int | None]] = []
        overlays: list[tuple[int, int]] = []
        pngs: list[Path] = []
        for ann in project.in_output():
            if ann.kind == "blur":
                entries.append((ann, None))
                continue
            patch = painter.patch(ann.shape, numbers.get(ann.id))
            if patch is None:
                continue
            image, rect = patch
            png = Path(tmp) / f"a{len(pngs)}.png"
            image.save(png, "PNG")
            entries.append((ann, len(pngs)))
            overlays.append((rect.x, rect.y))
            pngs.append(png)
        graph = build_filter(entries, overlays, project.info.width, project.info.height)
        args = export_command(info.path, project, part, pngs, graph, encoder=encoder, quality=quality)
        log.info("Exporting %s -> %s (%d annotations, %.1fs, encoder %s)", project.source, output, len(entries), project.output_duration, encoder)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", creationflags=flags,
        )
        stderr: list[str] = []
        reader = threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True)
        reader.start()
        try:
            for line in proc.stdout:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    break
                value = parse_progress(line, project.output_duration)
                if value is not None and progress is not None:
                    progress(value)
            code = proc.wait(timeout=EXPORT_TIMEOUT)
        except BaseException:
            proc.kill()
            part.unlink(missing_ok=True)
            raise
        reader.join(5)
    if cancel is not None and cancel.is_set():
        part.unlink(missing_ok=True)
        raise ExportCancelled()
    if code != 0 or not part.exists():
        part.unlink(missing_ok=True)
        tail = "".join(stderr).strip()[-400:]
        raise RecordingError(f"Video export failed: {tail}", message_key="video.error.export")
    part.replace(output)
    if progress is not None:
        progress(1.0)
    return output


def export_gif(
    project: VideoProject,
    output: Path,
    *,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    reader = GifReader(project.source)
    info = project.info
    numbers = project.step_numbers()
    painter = AnnotationPainter(info.width, info.height)
    sink = PillowGifSink(output, width=info.width, height=info.height, fps=max(1, round(info.fps)), max_width=0)
    start, end, total = project.start, project.end, project.output_duration
    sink.start(0.0)
    written = 0
    try:
        frames = list(enumerate(reader.starts))
        for i, (index, t) in enumerate(frames):
            nxt = reader.starts[index + 1] if index + 1 < len(reader.starts) else info.duration
            if nxt <= start or t >= end:
                continue
            if cancel is not None and cancel.is_set():
                raise ExportCancelled()
            frame = reader.frame(t)
            # Annotation changes inside a long frame (static screen) need their own GIF frames.
            cuts = sorted({max(t, start), *[x for a in project.annotations for x in (a.start, a.end) if max(t, start) < x < min(nxt, end)]})
            for cut in cuts:
                composed = painter.compose(frame, project.active_at(cut), numbers)
                sink.write(composed.convert("RGBA").tobytes("raw", "BGRA"), cut - start)
                written += 1
            if progress is not None:
                progress(min(0.99, (min(nxt, end) - start) / total if total else 1.0))
        if written == 0:
            raise RecordingError("Nothing to export in the selected range", message_key="video.error.empty")
        sink.extend_to(total)
        path = sink.finish()
    except BaseException:
        sink.abort()
        raise
    finally:
        reader.close()
    if progress is not None:
        progress(1.0)
    return path


__all__ = ["ExportCancelled", "build_filter", "export_command", "export_gif", "export_mp4", "parse_progress", "part_path"]
