"""Video Edit Engine: trim recordings and add timed annotations (steps, arrows,
boxes, highlights, text, blur), exported as a new MP4 (FFmpeg) or GIF (Pillow).

Headless: used by the desktop editor, the CLI (``video-info``, ``video-edit``)
and automation clients through ``app.video``.
"""

from screenpilot.videoedit.export import ExportCancelled
from screenpilot.videoedit.media import AnnotationPainter, FfmpegFrameReader, GifReader, parse_info, video_info
from screenpilot.videoedit.model import ANNOTATION_KINDS, TimedAnnotation, VideoInfo, VideoProject
from screenpilot.videoedit.service import VideoEditService

__all__ = [
    "ANNOTATION_KINDS",
    "AnnotationPainter",
    "ExportCancelled",
    "FfmpegFrameReader",
    "GifReader",
    "TimedAnnotation",
    "VideoEditService",
    "VideoInfo",
    "VideoProject",
    "parse_info",
    "video_info",
]
