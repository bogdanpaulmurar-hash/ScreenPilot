"""Headless video editing facade (``app.video``) for the UI, the CLI and Autopilot.

::

    project = app.video.open(Path("demo.mp4"))
    project.set_trim(1.5, 12.0)
    shape = project.make_shape("step", Point(400, 300))
    project.add(shape, 2.0, 5.0)
    app.video.export(project)          # -> demo_edited.mp4 next to the source
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from PIL import Image

from screenpilot.core.errors import RecordingError
from screenpilot.videoedit.export import Progress, export_gif, export_mp4
from screenpilot.videoedit.media import AnnotationPainter, FfmpegFrameReader, GifReader, is_gif, video_info
from screenpilot.videoedit.model import VideoProject, read_project_file

log = logging.getLogger(__name__)


class VideoEditService:
    def __init__(self, recording, *, bus=None, policy=None, settings=None) -> None:
        self.recording = recording  # RecordingService: FFmpeg discovery, encoder choice, quality
        self.bus = bus
        self.policy = policy
        self.settings = settings

    def _check_policy(self) -> None:
        if self.policy is not None and not self.policy.allows("video_edit"):
            raise RecordingError("Video editing is disabled by your administrator", message_key="policy.feature_disabled")

    def _ffmpeg_path(self) -> Path | None:
        info = self.recording.ffmpeg() if self.recording is not None else None
        return info.path if info else None

    # -- projects -------------------------------------------------------------------
    def open(self, source: Path) -> VideoProject:
        self._check_policy()
        source = Path(source)
        info = video_info(source, None if is_gif(source) else self._ffmpeg_path())
        log.info("Video opened for editing: %s (%dx%d, %.1fs, %s)", source, info.width, info.height, info.duration, info.kind)
        return VideoProject(source, info)

    def open_project_file(self, path: Path) -> VideoProject:
        data = read_project_file(path)
        source = Path(data["source"])
        if not source.is_absolute():
            source = Path(path).parent / source
        return self.open(source).apply_dict(data)

    def reader(self, project: VideoProject):
        """Frame reader for previews (``reader.frame(t)`` → RGB Pillow image)."""
        if project.info.kind == "gif":
            return GifReader(project.source)
        ffmpeg = self._ffmpeg_path()
        if ffmpeg is None:
            raise RecordingError("ffmpeg was not found", message_key="recording.error.no_ffmpeg")
        return FfmpegFrameReader(project.source, ffmpeg, project.info)

    def preview(self, project: VideoProject, t: float, reader=None, painter: AnnotationPainter | None = None) -> Image.Image:
        """Frame at source time ``t`` with the annotations visible at that time."""
        own = reader is None
        reader = reader or self.reader(project)
        painter = painter or AnnotationPainter(project.info.width, project.info.height)
        try:
            return painter.compose(reader.frame(t), project.active_at(t), project.step_numbers())
        finally:
            if own and hasattr(reader, "close"):
                reader.close()  # Windows keeps an open file locked

    # -- export ---------------------------------------------------------------------
    @staticmethod
    def default_output(project: VideoProject) -> Path:
        src = project.source
        base = src.with_name(f"{src.stem}_edited{src.suffix.lower() or '.mp4'}")
        if not base.exists():
            return base
        for n in range(2, 1000):
            candidate = src.with_name(f"{src.stem}_edited_{n}{base.suffix}")
            if not candidate.exists():
                return candidate
        raise RecordingError("No free file name for the edited video", message_key="video.error.export")

    def export(
        self,
        project: VideoProject,
        output: Path | None = None,
        *,
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        self._check_policy()
        output = Path(output) if output else self.default_output(project)
        if output.resolve() == project.source.resolve():
            raise RecordingError("The edited video cannot replace its source", message_key="video.error.same_file")
        if project.output_duration <= 0:
            raise RecordingError("Nothing to export in the selected range", message_key="video.error.empty")
        if project.info.kind == "gif" and output.suffix.lower() == ".gif":
            path = export_gif(project, output, progress=progress, cancel=cancel)
        else:
            info = self.recording.ffmpeg()
            if info is None:
                raise RecordingError("ffmpeg was not found", message_key="recording.error.no_ffmpeg")
            opts = self.recording.options()
            encoder = info.choose_encoder(opts.encoder)
            try:
                path = export_mp4(info, project, output, encoder=encoder, quality=opts.quality, progress=progress, cancel=cancel)
            except RecordingError as exc:
                if exc.message_key != "video.error.export" or encoder == "libx264" or "libx264" not in info.encoders:
                    raise
                log.warning("Export with %s failed; retrying with libx264", encoder)
                info.mark_broken(encoder)
                path = export_mp4(info, project, output, encoder="libx264", quality=opts.quality, progress=progress, cancel=cancel)
        log.info("Edited video saved: %s", path)
        if self.bus is not None:
            self.bus.publish("video.exported", path=str(path), source=str(project.source), duration=project.output_duration)
        return path



