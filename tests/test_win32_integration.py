"""Real Windows integration tests (run on the Windows CI runner, skipped elsewhere)."""

import sys
import unittest

WINDOWS = sys.platform == "win32"


@unittest.skipUnless(WINDOWS, "Windows only")
class Win32BackendIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from screenpilot.capture.win32_backend import Win32CaptureBackend

        cls.backend = Win32CaptureBackend()

    def test_monitors_detected_with_one_primary(self):
        monitors = self.backend.monitors()
        self.assertGreaterEqual(len(monitors), 1)
        self.assertEqual(sum(m.primary for m in monitors), 1)
        self.assertEqual(monitors[0].index, 1)
        self.assertTrue(monitors[0].primary)

    def test_grab_returns_requested_size(self):
        from screenpilot.capture import Rect

        primary = self.backend.monitors()[0].bounds
        rect = Rect(primary.x, primary.y, min(64, primary.width), min(48, primary.height))
        image = self.backend.grab(rect)
        self.assertEqual((image.width, image.height), (rect.width, rect.height))

    def test_full_service_capture_of_all_monitors(self):
        from screenpilot.capture import CaptureService

        result = CaptureService(self.backend).capture_all_monitors()
        self.assertEqual(result.region, CaptureService(self.backend).desktop().bounds)

    def test_cursor_and_window_queries_do_not_fail(self):
        self.backend.cursor_position()
        self.backend.foreground_window()
        self.assertIsInstance(list(self.backend.list_windows()), list)


@unittest.skipUnless(WINDOWS, "Windows only")
class Win32HotkeyListenerIntegrationTests(unittest.TestCase):
    def test_register_and_unregister(self):
        from screenpilot.hotkeys import Hotkey
        from screenpilot.hotkeys.win32_listener import Win32HotkeyListener

        listener = Win32HotkeyListener(lambda binding_id: None)
        try:
            ok = listener.register(1, Hotkey.parse("Ctrl+Alt+Shift+F24"))
            self.assertTrue(ok)
            listener.unregister(1)
        finally:
            listener.stop()


@unittest.skipUnless(WINDOWS, "Windows only")
class Win32ClipboardIntegrationTests(unittest.TestCase):
    def test_set_text_and_image(self):
        from screenpilot.capture import FakeBackend, Monitor, Rect
        from screenpilot.platform.win32_clipboard import Win32Clipboard

        clipboard = Win32Clipboard()
        clipboard.set_text("ScreenPilot ✓ diacritice: ăîșț")
        image = FakeBackend([Monitor(1, Rect(0, 0, 10, 10), Rect(0, 0, 10, 10), True)]).grab(Rect(0, 0, 10, 10))
        clipboard.set_image(image)


if __name__ == "__main__":
    unittest.main()
