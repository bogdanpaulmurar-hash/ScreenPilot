"""Recording options, built from settings (``recording.*``)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

FORMATS = ("mp4", "gif")
ENCODER_MODES = ("auto", "hardware", "software")

FPS_RANGE = (5, 60)
GIF_FPS_RANGE = (5, 30)
QUALITY_RANGE = (16, 40)  # CRF-like: lower = better quality, larger file


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    return max(bounds[0], min(bounds[1], int(value)))


@dataclass(frozen=True)
class RecordingOptions:
    format: str = "mp4"
    fps: int = 30
    gif_fps: int = 15
    gif_max_width: int = 1920
    quality: int = 20
    encoder: str = "auto"
    microphone: str = ""  # DirectShow device name, "" = no microphone
    system_audio: str = ""  # DirectShow loopback device (e.g. "Stereo Mix"), "" = none
    audio_offset_ms: int = 0
    show_cursor: bool = True
    highlight_cursor: bool = True
    click_effects: bool = True
    zoom_factor: float = 2.0
    countdown: int = 3
    max_minutes: int = 60
    ffmpeg_path: str = ""

    def __post_init__(self) -> None:
        if self.format not in FORMATS:
            object.__setattr__(self, "format", "mp4")
        if self.encoder not in ENCODER_MODES:
            object.__setattr__(self, "encoder", "auto")
        object.__setattr__(self, "fps", _clamp(self.fps, FPS_RANGE))
        object.__setattr__(self, "gif_fps", _clamp(self.gif_fps, GIF_FPS_RANGE))
        object.__setattr__(self, "quality", _clamp(self.quality, QUALITY_RANGE))
        object.__setattr__(self, "gif_max_width", max(160, min(3840, int(self.gif_max_width))))
        object.__setattr__(self, "countdown", max(0, min(10, int(self.countdown))))
        object.__setattr__(self, "max_minutes", max(1, min(240, int(self.max_minutes))))
        object.__setattr__(self, "zoom_factor", max(1.25, min(4.0, float(self.zoom_factor))))

    @property
    def is_gif(self) -> bool:
        return self.format == "gif"

    @property
    def frame_rate(self) -> int:
        """Capture rate: GIFs need far fewer frames than video."""
        return self.gif_fps if self.is_gif else self.fps

    @property
    def has_audio(self) -> bool:
        return not self.is_gif and bool(self.microphone or self.system_audio)

    def without_audio(self) -> "RecordingOptions":
        return replace(self, microphone="", system_audio="")

    def with_format(self, fmt: str) -> "RecordingOptions":
        return replace(self, format=fmt)

    @classmethod
    def from_settings(cls, settings: Any) -> "RecordingOptions":
        def get(key: str, default: Any) -> Any:
            value = settings.get(f"recording.{key}", default)
            return default if value is None else value

        base = cls()
        values = {name: get(name, getattr(base, name)) for name in base.__dataclass_fields__}
        return cls(**values)
