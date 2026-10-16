"""Capture Engine: screenshots of regions, windows and monitors."""

from screenpilot.capture.backend import CaptureBackend, FakeBackend, default_backend
from screenpilot.capture.geometry import Monitor, Point, Rect, VirtualDesktop, WindowInfo
from screenpilot.capture.image import CapturedImage, CaptureResult
from screenpilot.capture.service import CaptureService

__all__ = [
    "CaptureBackend",
    "CaptureResult",
    "CaptureService",
    "CapturedImage",
    "FakeBackend",
    "Monitor",
    "Point",
    "Rect",
    "VirtualDesktop",
    "WindowInfo",
    "default_backend",
]
