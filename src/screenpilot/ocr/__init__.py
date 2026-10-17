"""OCR Engine: Windows OCR (built in) or Tesseract (optional), plus sensitive-data detection."""

from screenpilot.ocr.engine import (
    OcrEngine,
    OcrError,
    OcrLine,
    OcrResult,
    OcrWord,
    TesseractEngine,
    WindowsOcrEngine,
    default_engine,
)
from screenpilot.ocr.export import to_markdown, to_plain_text
from screenpilot.ocr.sensitive import SensitiveMatch, blur_rects, find_sensitive

__all__ = [
    "OcrEngine",
    "OcrError",
    "OcrLine",
    "OcrResult",
    "OcrWord",
    "SensitiveMatch",
    "TesseractEngine",
    "WindowsOcrEngine",
    "blur_rects",
    "default_engine",
    "find_sensitive",
    "to_markdown",
    "to_plain_text",
]
