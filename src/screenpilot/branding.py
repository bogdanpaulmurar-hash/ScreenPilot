"""The ScreenPilot icon, drawn with Pillow (no binary assets in the repository).

One drawing is used everywhere: tray, taskbar, windows (through Qt) and the
``.ico`` file referenced by the Start-menu shortcut and the autostart entry.

Design: a rounded square with a blue→teal gradient, white viewfinder corners
and a red "record" dot — capture and recording in one mark that stays
readable at 16 px in the tray.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

TOP = (0, 103, 192)  # #0067C0
BOTTOM = (0, 153, 168)  # #0099A8
DOT = (232, 17, 35)  # #E81123
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SUPERSAMPLE = 4


@lru_cache(maxsize=16)
def render_icon(size: int = 256) -> Image.Image:
    """RGBA icon of ``size`` × ``size`` pixels (cached)."""
    s = size * SUPERSAMPLE
    gradient = Image.new("RGBA", (1, s))
    for y in range(s):
        t = y / max(1, s - 1)
        gradient.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))
    gradient = gradient.resize((s, s))
    mask = Image.new("L", (s, s), 0)
    margin = round(s * 0.06)
    ImageDraw.Draw(mask).rounded_rectangle((margin, margin, s - margin, s - margin), radius=round(s * 0.22), fill=255)
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    image.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(image)
    # Small sizes need relatively thicker strokes to stay legible in the tray.
    width = round(s * (0.11 if size <= 24 else 0.085))
    a, b, arm = s * 0.25, s * 0.75, s * (0.17 if size <= 24 else 0.15)
    for x, y, dx, dy in ((a, a, 1, 1), (b, a, -1, 1), (a, b, 1, -1), (b, b, -1, -1)):
        draw.line([(x, y + dy * arm), (x, y), (x + dx * arm, y)], fill="white", width=width, joint="curve")
        r = width / 2
        for px, py in ((x, y + dy * arm), (x + dx * arm, y)):
            draw.ellipse((px - r, py - r, px + r, py + r), fill="white")
        draw.ellipse((x - r, y - r, x + r, y + r), fill="white")
    r = s * (0.12 if size <= 24 else 0.1)
    c = s / 2
    draw.ellipse((c - r - width * 0.35, c - r - width * 0.35, c + r + width * 0.35, c + r + width * 0.35), fill="white")
    draw.ellipse((c - r, c - r, c + r, c + r), fill=DOT)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(path: Path) -> Path:
    """Multi-resolution ``.ico`` for shortcuts and the autostart entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    largest = render_icon(max(ICO_SIZES))
    largest.save(path, format="ICO", sizes=[(n, n) for n in ICO_SIZES], append_images=[render_icon(n) for n in ICO_SIZES])
    return path


def write_png(path: Path, size: int = 256) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    render_icon(size).save(path, "PNG")
    return path
