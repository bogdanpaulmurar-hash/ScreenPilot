"""Optional watermark: text (user, date, custom) stamped on captures."""

from __future__ import annotations

import getpass
import socket
from datetime import datetime

from PIL import Image, ImageDraw

from screenpilot.capture.image import CaptureResult, CapturedImage
from screenpilot.editor.document import font

POSITIONS = ("bottom-right", "bottom-left", "top-right", "top-left", "center", "tiled")
MARGIN = 12


def render_text(template: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    values = {
        "user": user,
        "computer": socket.gethostname(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
    }
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def watermark_image(
    image: CapturedImage, text: str, *, position: str = "bottom-right", opacity: int = 55, size: int = 14
) -> CapturedImage:
    base = image.to_pil().convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    scaled = max(9, round(size * max(1.0, base.width / 1600)))
    f = font(scaled, True)
    l, t, r, b = draw.textbbox((0, 0), text, font=f)
    tw, th = r - l, b - t
    alpha = round(255 * max(5, min(100, opacity)) / 100)
    pad = 6
    if position == "tiled":
        step_x, step_y = tw + 120, th + 90
        for y in range(-th, base.height + th, step_y):
            offset = (y // step_y % 2) * (step_x // 2)
            for x in range(-tw + offset, base.width + tw, step_x):
                draw.text((x, y), text, font=f, fill=(128, 128, 128, alpha))
    else:
        if position == "center":
            x, y = (base.width - tw) // 2, (base.height - th) // 2
        else:
            x = MARGIN if "left" in position else base.width - tw - MARGIN - 2 * pad
            y = MARGIN if "top" in position else base.height - th - MARGIN - 2 * pad
        draw.rounded_rectangle((x, y, x + tw + 2 * pad, y + th + 2 * pad + 2), 5, fill=(0, 0, 0, round(alpha * 0.55)))
        draw.text((x + pad - l, y + pad - t), text, font=f, fill=(255, 255, 255, alpha))
    return CapturedImage.from_pil(Image.alpha_composite(base, layer).convert("RGB"))


def apply_watermark(result: CaptureResult, settings) -> CaptureResult:
    text = render_text(settings.get("watermark.text") or "")
    if not text.strip():
        return result
    image = watermark_image(
        result.image,
        text,
        position=settings.get("watermark.position"),
        opacity=int(settings.get("watermark.opacity")),
        size=int(settings.get("watermark.size")),
    )
    return CaptureResult(image, result.mode, result.region, result.timestamp, {**result.metadata, "watermarked": True})
