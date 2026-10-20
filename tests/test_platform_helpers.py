"""Platform helpers that are pure Python: key latch, clipboard PNG, Explorer fallback."""

import io
import sys
import unittest
from pathlib import Path

from PIL import Image

from screenpilot.capture import CapturedImage
from screenpilot.platform.dib import png_from_image
from screenpilot.platform.keys import KeyLatch
from screenpilot.platform import win32_window


class KeyLatchTests(unittest.TestCase):
    def test_one_event_per_press_and_held_key_at_start_ignored(self):
        samples = iter([True, True, False, True, True, False, False, True])
        latch = KeyLatch(lambda: next(samples))  # first sample: already held -> not a press
        self.assertEqual([latch.poll() for _ in range(7)], [False, False, True, False, False, False, True])

    def test_reset_absorbs_a_held_key(self):
        state = {"down": False}
        latch = KeyLatch(lambda: state["down"])
        state["down"] = True
        latch.reset()  # e.g. the Esc that closed a dialog
        self.assertFalse(latch.poll())
        state["down"] = False
        self.assertFalse(latch.poll())
        state["down"] = True
        self.assertTrue(latch.poll())


class ClipboardPngTests(unittest.TestCase):
    def test_png_is_lossless(self):
        img = Image.new("RGB", (37, 21), (12, 34, 56))
        img.putpixel((5, 7), (250, 1, 128))
        data = png_from_image(CapturedImage.from_pil(img))
        self.assertTrue(data.startswith(b"\x89PNG"))
        with Image.open(io.BytesIO(data)) as decoded:
            self.assertEqual(decoded.convert("RGB").tobytes(), img.tobytes())


class Win32WindowFallbackTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "checks the no-op behaviour elsewhere")
    def test_helpers_are_safe_no_ops_off_windows(self):
        self.assertFalse(win32_window.force_foreground(123))
        self.assertFalse(win32_window.disable_ghosting())
        self.assertFalse(win32_window.key_down(win32_window.VK_ESCAPE))
        self.assertFalse(win32_window.reveal_in_explorer(Path("x")))


if __name__ == "__main__":
    unittest.main()
