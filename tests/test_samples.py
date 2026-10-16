import unittest

from screenpilot.capture import CaptureService, Point, Rect
from screenpilot.samples import DemoBackend


class DemoBackendTests(unittest.TestCase):
    def test_demo_desktop_offset_monitor(self):
        backend = DemoBackend(Rect(-1280, 0, 1280, 800))
        service = CaptureService(backend)
        erp = backend.list_windows()[0]
        self.assertTrue(erp.bounds.x < 0)
        result = service.capture_window()
        self.assertEqual(result.region, erp.bounds)
        self.assertEqual((result.image.width, result.image.height), (erp.bounds.width, erp.bounds.height))
        self.assertIs(backend.window_at(Point(erp.bounds.x + 5, erp.bounds.y + 5)), erp)
        # title bar of the window is light, wallpaper outside is dark blue
        self.assertGreater(sum(result.image.pixel(5, 5)), 600)
        wallpaper = service.capture_region(Rect(-1275, 790 - 60, 4, 4)).image.pixel(0, 0)
        self.assertEqual(wallpaper, (24, 60, 104))


if __name__ == "__main__":
    unittest.main()
