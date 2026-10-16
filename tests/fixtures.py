"""Shared test fixtures: a realistic two-monitor, mixed-DPI desktop."""

from screenpilot.capture import FakeBackend, Monitor, Point, Rect, WindowInfo

# Laptop panel (primary, 150 %) with an external monitor to its LEFT -> negative x.
PRIMARY = Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), True, r"\\.\DISPLAY1", 1.5)
LEFT = Monitor(2, Rect(-2560, -360, 2560, 1440), Rect(-2560, -360, 2560, 1400), False, r"\\.\DISPLAY2", 1.0)

EDITOR = WindowInfo(101, "Report.docx - Word", Rect(100, 50, 800, 600), "OpusApp", 4242)
SPANNING = WindowInfo(102, "ERP Client", Rect(-200, 100, 600, 400), "SunAwtFrame", 77)


def make_backend(cursor=Point(10, 10)) -> FakeBackend:
    return FakeBackend([PRIMARY, LEFT], windows=[EDITOR, SPANNING], cursor=cursor)
