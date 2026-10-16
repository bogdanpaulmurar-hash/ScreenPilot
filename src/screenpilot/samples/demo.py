"""A synthetic, realistic-looking desktop for demos, tutorials and UI tests.

Rendered with Pillow only, so screenshots in CI, documentation and the
learning center look like a real working session without capturing anyone's
actual screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from screenpilot.capture.geometry import Monitor, Point, Rect, WindowInfo
from screenpilot.capture.image import CapturedImage
from screenpilot.core.errors import CaptureError

WALLPAPER = (24, 60, 104)
TASKBAR = (32, 32, 32)
TITLE = (243, 243, 243)
WINDOW = (255, 255, 255)
TEXT = (32, 32, 32)
MUTED = (140, 140, 140)
ACCENT = (0, 120, 212)
FIELD = (250, 250, 250)
BORDER = (210, 210, 210)


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


@dataclass(frozen=True)
class DemoDesktop:
    image: Image.Image
    windows: list[WindowInfo]


def _window(draw: ImageDraw.ImageDraw, box: Rect, title: str) -> None:
    draw.rectangle((box.x, box.y, box.right - 1, box.bottom - 1), fill=WINDOW, outline=BORDER)
    draw.rectangle((box.x, box.y, box.right - 1, box.y + 31), fill=TITLE)
    draw.text((box.x + 12, box.y + 8), title, fill=TEXT, font=_font(13))
    # Caption buttons drawn as shapes (no dependency on symbol fonts): close, maximize, minimize.
    cy = box.y + 16
    cx = box.right - 24
    draw.line((cx - 5, cy - 5, cx + 5, cy + 5), fill=TEXT, width=1)
    draw.line((cx - 5, cy + 5, cx + 5, cy - 5), fill=TEXT, width=1)
    cx -= 44
    draw.rectangle((cx - 5, cy - 5, cx + 5, cy + 5), outline=TEXT)
    cx -= 44
    draw.line((cx - 5, cy, cx + 5, cy), fill=TEXT, width=1)


def render_demo_desktop(width: int, height: int) -> DemoDesktop:
    image = Image.new("RGB", (width, height), WALLPAPER)
    draw = ImageDraw.Draw(image)
    small, normal, large = _font(12), _font(14), _font(20)

    # Taskbar
    draw.rectangle((0, height - 40, width, height), fill=TASKBAR)
    for i in range(5):
        draw.rounded_rectangle((width // 2 - 110 + i * 46, height - 34, width // 2 - 78 + i * 46, height - 6), 4, fill=(70, 70, 70))
    draw.text((width - 70, height - 30), "09:41", fill=(230, 230, 230), font=small)

    # Back window: report
    report = Rect(int(width * 0.04), int(height * 0.05), int(width * 0.55), int(height * 0.62))
    _window(draw, report, "Monthly report.docx - Word")
    draw.text((report.x + 40, report.y + 60), "Monthly operations report", fill=TEXT, font=large)
    y = report.y + 100
    for i in range(12):
        line_w = report.width - 80 - (i * 37 % 140)
        if y + 10 > report.bottom - 10:
            break
        draw.rectangle((report.x + 40, y, report.x + 40 + line_w, y + 7), fill=(205, 205, 205))
        y += 20

    # Front window: ERP form
    erp = Rect(int(width * 0.38), int(height * 0.22), int(width * 0.5), int(height * 0.6))
    _window(draw, erp, "Sales Order 10482 - ERP Client")
    labels = ("Customer", "Order date", "Delivery", "Payment terms", "Amount")
    values = ("Contoso SRL", "2026-10-16", "Express", "30 days", "12,480.00 RON")
    fy = erp.y + 56
    for label, value in zip(labels, values):
        if fy + 26 > erp.bottom - 60:
            break
        draw.text((erp.x + 24, fy + 4), label, fill=MUTED, font=normal)
        draw.rectangle((erp.x + 170, fy, erp.right - 24, fy + 26), fill=FIELD, outline=BORDER)
        draw.text((erp.x + 178, fy + 4), value, fill=TEXT, font=normal)
        fy += 38
    draw.rounded_rectangle((erp.right - 220, erp.bottom - 48, erp.right - 124, erp.bottom - 16), 4, outline=BORDER, fill=WINDOW)
    draw.text((erp.right - 200, erp.bottom - 42), "Cancel", fill=TEXT, font=normal)
    draw.rounded_rectangle((erp.right - 112, erp.bottom - 48, erp.right - 24, erp.bottom - 16), 4, fill=ACCENT)
    draw.text((erp.right - 96, erp.bottom - 42), "Approve", fill=(255, 255, 255), font=normal)

    windows = [
        WindowInfo(201, "Sales Order 10482 - ERP Client", erp, "ErpMain", 300),
        WindowInfo(202, "Monthly report.docx - Word", report, "OpusApp", 301),
    ]
    return DemoDesktop(image, windows)


class DemoBackend:
    """Capture backend that serves a rendered demo desktop on one monitor."""

    name = "demo"

    def __init__(self, bounds: Rect, *, device_name: str = "", cursor: Point | None = None) -> None:
        self.bounds = bounds
        self.desktop = render_demo_desktop(bounds.width, bounds.height)
        self._windows = [
            WindowInfo(w.handle, w.title, w.bounds.translate(bounds.x, bounds.y), w.class_name, w.process_id)
            for w in self.desktop.windows
        ]
        self._monitor = Monitor(1, bounds, Rect(bounds.x, bounds.y, bounds.width, bounds.height - 40), True, device_name)
        self.cursor = cursor or Point(bounds.x + bounds.width // 2, bounds.y + bounds.height // 2)
        self.foreground: WindowInfo | None = self._windows[0]
        self.grabs: list[Rect] = []

    def monitors(self) -> Sequence[Monitor]:
        return [self._monitor]

    def grab(self, rect: Rect) -> CapturedImage:
        area = rect.intersect(self.bounds)
        if area.is_empty:
            raise CaptureError("Empty capture rectangle")
        self.grabs.append(area)
        x, y = area.x - self.bounds.x, area.y - self.bounds.y
        crop = self.desktop.image.crop((x, y, x + area.width, y + area.height))
        return CapturedImage.from_pil(crop)

    def cursor_position(self) -> Point:
        return self.cursor

    def foreground_window(self) -> WindowInfo | None:
        return self.foreground

    def list_windows(self) -> Sequence[WindowInfo]:
        return list(self._windows)

    def window_at(self, point: Point, *, include_children: bool = False) -> WindowInfo | None:
        return next((w for w in self._windows if w.bounds.contains(point)), None)
