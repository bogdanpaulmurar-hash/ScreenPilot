"""Headless recording facade used by the UI, the CLI and Autopilot.

::

    rec = app.recording.start(Rect(0, 0, 1280, 720))   # settings decide format/audio
    ...
    result = rec.stop(wait=True)
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from screenpilot.capture.geometry import Rect
from screenpilot.core.errors import RecordingError
from screenpilot.recording.ffmpeg import FfmpegInfo, FfmpegSink, convert_to_gif, find_ffmpeg, probe
from screenpilot.recording.gif import PillowGifSink
from screenpilot.recording.options import RecordingOptions
from screenpilot.recording.recorder import Recorder
from screenpilot.recording.source import FrameSource, default_source

log = logging.getLogger(__name__)


class RecordingService:
    def __init__(
        self,
        settings,
        *,
        backend=None,
        bus=None,
        policy=None,
        default_dir: Callable[[], Path] | None = None,
        app_dir: Path | None = None,
        extra_dirs: tuple[Path, ...] = (),
        source_factory: Callable[[], FrameSource] | None = None,
        ffmpeg_prober: Callable[[Path], FfmpegInfo] = probe,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.bus = bus
        self.policy = policy
        self.default_dir = default_dir or (lambda: Path.home() / "Videos")
        self.app_dir = app_dir
        self.extra_dirs = extra_dirs
        self.source_factory = source_factory or (lambda: default_source(self.backend))
        self.ffmpeg_prober = ffmpeg_prober
        self._ffmpeg: FfmpegInfo | None = None
        self._ffmpeg_key: str | None = None
        self._lock = threading.Lock()
        self.current: Recorder | None = None

    # -- environment ----------------------------------------------------------------
    def options(self) -> RecordingOptions:
        opts = RecordingOptions.from_settings(self.settings)
        if opts.has_audio and self.policy is not None and not self.policy.allows("record_audio"):
            log.info("Audio recording disabled by policy")
            opts = opts.without_audio()
        return opts

    def ffmpeg(self, *, refresh: bool = False) -> FfmpegInfo | None:
        """Located and probed ffmpeg, cached until the configured path changes."""
        configured = self.settings.get("recording.ffmpeg_path", "") or ""
        with self._lock:
            if refresh or self._ffmpeg_key != configured:
                self._ffmpeg_key = configured
                self._ffmpeg = None
                path = find_ffmpeg(configured, self.app_dir, self.extra_dirs)
                if path is None:
                    log.info("ffmpeg not found (configured=%r); MP4 recording unavailable", configured)
                else:
                    try:
                        self._ffmpeg = self.ffmpeg_prober(path)
                    except RecordingError as exc:
                        log.warning("%s", exc)
            return self._ffmpeg

    def audio_devices(self) -> list[str]:
        info = self.ffmpeg()
        return info.audio_devices() if info else []

    def output_dir(self) -> Path:
        configured = self.settings.get("recording.output_dir", "") or ""
        return Path(configured) if configured else self.default_dir()

    def output_path(self, fmt: str, now: datetime | None = None) -> Path:
        now = now or datetime.now()
        folder = self.output_dir()
        base = folder / f"ScreenPilot_{now:%Y-%m-%d_%H-%M-%S}.{fmt}"
        if not base.exists():
            return base
        for n in range(1, 1000):
            candidate = folder / f"{base.stem}_{n}.{fmt}"
            if not candidate.exists():
                return candidate
        raise RecordingError("No free file name for the recording")

    # -- recording ------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.active

    def prepare(self, options: RecordingOptions | None = None, *, output: Path | None = None) -> Recorder:
        """Build a recorder (encoder chosen, output path reserved) without starting it."""
        if self.policy is not None and not self.policy.allows("recording"):
            raise RecordingError("Recording is disabled by your administrator", message_key="policy.feature_disabled")
        if self.busy:
            raise RecordingError("A recording is already running", message_key="recording.error.busy")
        self._learn_from_last_failure()
        opts = options or self.options()
        output = output or self.output_path(opts.format)
        info = None if opts.is_gif else self.ffmpeg()
        if not opts.is_gif and info is None:
            raise RecordingError("ffmpeg was not found", message_key="recording.error.no_ffmpeg")
        encoder = info.choose_encoder(opts.encoder) if info else "gif"

        def sink_factory(width: int, height: int):
            if opts.is_gif:
                return PillowGifSink(output, width=width, height=height, fps=opts.gif_fps, max_width=opts.gif_max_width)
            return FfmpegSink(
                info,
                output,
                width=width,
                height=height,
                fps=opts.fps,
                quality=opts.quality,
                encoder=encoder,
                microphone=opts.microphone,
                system_audio=opts.system_audio,
                audio_offset_ms=opts.audio_offset_ms,
            )

        source = self.source_factory()
        if hasattr(source, "show_cursor"):
            source.show_cursor = opts.show_cursor
        recorder = Recorder(source, sink_factory, opts, bus=self.bus, encoder=encoder)
        self.current = recorder
        return recorder

    def _learn_from_last_failure(self) -> None:
        last = self.current
        if last is None or last.error is None or self._ffmpeg is None:
            return
        if last.error.message_key == "recording.error.encoder_failed" and last.encoder not in ("", "gif", "libx264"):
            self._ffmpeg.mark_broken(last.encoder)
        self.current = None

    def start(self, rect: Rect, options: RecordingOptions | None = None, *, output: Path | None = None) -> Recorder:
        recorder = self.prepare(options, output=output)
        try:
            recorder.start(rect)
        except Exception:
            self.current = None
            raise
        return recorder

    def to_gif(self, video: Path, *, fps: int | None = None, max_width: int | None = None) -> Path:
        info = self.ffmpeg()
        if info is None:
            raise RecordingError("ffmpeg was not found", message_key="recording.error.no_ffmpeg")
        opts = self.options()
        return convert_to_gif(
            info, video, video.with_suffix(".gif"), fps=fps or opts.gif_fps, max_width=max_width or opts.gif_max_width
        )
