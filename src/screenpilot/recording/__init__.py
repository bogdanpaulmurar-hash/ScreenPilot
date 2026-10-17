"""Recording Engine: screen video (MP4 via external ffmpeg) and animated GIF.

Frames are captured by ScreenPilot itself (GDI on Windows) so cursor
highlight, click ripples and zoom work the same for every output format.
"""

from screenpilot.recording.effects import EffectOptions, EffectRenderer
from screenpilot.recording.ffmpeg import FfmpegInfo, FfmpegSink, find_ffmpeg, probe
from screenpilot.recording.gif import PillowGifSink
from screenpilot.recording.options import RecordingOptions
from screenpilot.recording.recorder import Recorder, RecordingResult, State
from screenpilot.recording.service import RecordingService
from screenpilot.recording.source import BackendFrameSource, FrameSource

__all__ = [
    "BackendFrameSource",
    "EffectOptions",
    "EffectRenderer",
    "FfmpegInfo",
    "FfmpegSink",
    "FrameSource",
    "PillowGifSink",
    "Recorder",
    "RecordingOptions",
    "RecordingResult",
    "RecordingService",
    "State",
    "find_ffmpeg",
    "probe",
]
