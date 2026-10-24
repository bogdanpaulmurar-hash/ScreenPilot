"""In-memory capture result.

Pixels are kept as raw top-down BGRA bytes exactly as Windows produces them.
Conversion to a Pillow image happens lazily and only when needed (saving,
editing), which keeps the capture path fast and memory-light.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from screenpilot.capture.geometry import Rect

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

BYTES_PER_PIXEL = 4


@dataclass(frozen=True)
class CapturedImage:
    width: int
    height: int
    bgra: bytes

    def __post_init__(self) -> None:
        expected = self.width * self.height * BYTES_PER_PIXEL
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Image dimensions must be positive")
        if len(self.bgra) != expected:
            raise ValueError(f"Pixel buffer has {len(self.bgra)} bytes, expected {expected}")

    @property
    def stride(self) -> int:
        return self.width * BYTES_PER_PIXEL

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        """RGB of one pixel (used by color picker and tests)."""
        i = y * self.stride + x * BYTES_PER_PIXEL
        b, g, r = self.bgra[i], self.bgra[i + 1], self.bgra[i + 2]
        return r, g, b

    def crop(self, rect: Rect) -> "CapturedImage":
        """Crop with coordinates relative to this image."""
        area = rect.intersect(Rect(0, 0, self.width, self.height))
        if area.is_empty:
            raise ValueError("Crop rectangle is outside the image")
        rows = []
        start = area.x * BYTES_PER_PIXEL
        end = start + area.width * BYTES_PER_PIXEL
        for y in range(area.y, area.bottom):
            offset = y * self.stride
            rows.append(self.bgra[offset + start : offset + end])
        return CapturedImage(area.width, area.height, b"".join(rows))

    def to_pil(self) -> "Image":
        from PIL import Image

        # GDI leaves the alpha byte undefined, so read it as padding ("BGRX").
        return Image.frombuffer("RGB", (self.width, self.height), self.bgra, "raw", "BGRX", self.stride, 1)

    @classmethod
    def from_pil(cls, image: "Image") -> "CapturedImage":
        rgba = image.convert("RGBA")
        return cls(rgba.width, rgba.height, rgba.tobytes("raw", "BGRA"))

    def save(self, path: Path, *, image_format: str = "png", jpeg_quality: int = 90) -> Path:
        image_format = image_format.lower()
        path.parent.mkdir(parents=True, exist_ok=True)
        image = self.to_pil()
        if image_format in ("jpg", "jpeg"):
            image.save(path, "JPEG", quality=jpeg_quality, optimize=True)
        elif image_format == "bmp":
            image.save(path, "BMP")
        else:
            image.save(path, "PNG", optimize=False)  # optimize=True is slow on large captures
        return path


def stack_offsets(heights: list[int], gap: int = 16) -> list[int]:
    """Top of each picture when they are placed one below the other, ``gap`` px apart."""
    offsets, y = [], 0
    for h in heights:
        offsets.append(y)
        y += h + gap
    return offsets


def stack_images(images: list[CapturedImage], gap: int = 16, background: tuple[int, int, int] = (255, 255, 255)) -> CapturedImage:
    """One image with all ``images`` one below the other, left aligned (multi-capture "one image")."""
    from PIL import Image

    if not images:
        raise ValueError("No images to stack")
    if len(images) == 1:
        return images[0]
    offsets = stack_offsets([i.height for i in images], gap)
    width = max(i.width for i in images)
    height = offsets[-1] + images[-1].height
    canvas = Image.new("RGB", (width, height), background)
    for image, top in zip(images, offsets):
        canvas.paste(image.to_pil(), (0, top))
    return CapturedImage.from_pil(canvas)


@dataclass(frozen=True)
class CaptureResult:
    image: CapturedImage
    mode: str
    region: Rect
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
