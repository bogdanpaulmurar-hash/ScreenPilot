import tempfile
import threading
import unittest
from pathlib import Path

from screenpilot.capture import CapturedImage, CaptureService, FakeBackend, Point, Rect, VirtualDesktop
from screenpilot.capture.service import EVENT_CANCELLED, EVENT_COMPLETED, EVENT_COUNTDOWN
from screenpilot.core.errors import CaptureError
from screenpilot.core.events import EventBus

from fixtures import EDITOR, LEFT, PRIMARY, SPANNING, make_backend


class RectTests(unittest.TestCase):
    def test_from_points_any_drag_direction_is_inclusive(self):
        self.assertEqual(Rect.from_points(Point(10, 20), Point(5, 8)), Rect(5, 8, 6, 13))
        self.assertEqual(Rect.from_points(Point(3, 3), Point(3, 3)), Rect(3, 3, 1, 1))

    def test_intersect_union(self):
        a, b = Rect(0, 0, 10, 10), Rect(5, 5, 10, 10)
        self.assertEqual(a.intersect(b), Rect(5, 5, 5, 5))
        self.assertEqual(a.union(b), Rect(0, 0, 15, 15))
        self.assertTrue(a.intersect(Rect(20, 20, 1, 1)).is_empty)

    def test_parse(self):
        self.assertEqual(Rect.parse("-10, 5, 100,50"), Rect(-10, 5, 100, 50))
        with self.assertRaises(ValueError):
            Rect.parse("1,2,3")
        with self.assertRaises(ValueError):
            Rect.parse("0,0,0,5")


class VirtualDesktopTests(unittest.TestCase):
    def setUp(self):
        self.desktop = VirtualDesktop([PRIMARY, LEFT])

    def test_bounds_include_negative_coordinates(self):
        self.assertEqual(self.desktop.bounds, Rect(-2560, -360, 4480, 1440))

    def test_monitor_at_and_gap_fallback(self):
        self.assertIs(self.desktop.monitor_at(Point(-1, 0)), LEFT)
        self.assertIs(self.desktop.monitor_at(Point(0, 0)), PRIMARY)
        # (100, -50) is in the gap above the primary monitor -> nearest is primary
        self.assertIs(self.desktop.monitor_at(Point(100, -50)), PRIMARY)
        # (100, -300) is closer to the left monitor's right edge than to the primary
        self.assertIs(self.desktop.monitor_at(Point(100, -300)), LEFT)

    def test_visibility(self):
        self.assertFalse(self.desktop.is_visible(Rect(100, -300, 50, 50)))
        self.assertTrue(self.desktop.is_visible(Rect(-10, -10, 20, 20)))


class CapturedImageTests(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            CapturedImage(2, 2, b"\x00" * 15)

    def test_crop_and_pil_roundtrip(self):
        img = FakeBackend([PRIMARY]).grab(Rect(0, 0, 8, 6))
        cropped = img.crop(Rect(2, 1, 3, 4))
        self.assertEqual((cropped.width, cropped.height), (3, 4))
        self.assertEqual(cropped.pixel(0, 0), FakeBackend.color_at(2, 1))
        pil = cropped.to_pil()
        self.assertEqual(pil.getpixel((2, 3)), FakeBackend.color_at(4, 4))
        self.assertEqual(CapturedImage.from_pil(pil).pixel(2, 3), FakeBackend.color_at(4, 4))

    def test_save_formats(self):
        img = FakeBackend([PRIMARY]).grab(Rect(0, 0, 4, 4))
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ("png", "jpg", "bmp"):
                path = img.save(Path(tmp) / "sub" / f"a.{fmt}", image_format=fmt)
                self.assertGreater(path.stat().st_size, 0)


class CaptureServiceTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.events = []
        self.bus.subscribe("*", lambda e: self.events.append(e.name))
        self.backend = make_backend()
        self.service = CaptureService(self.backend, self.bus, sleep=lambda s: None)

    def test_region_capture_pixels_match_desktop_coordinates(self):
        result = self.service.capture_region(Rect(-5, 3, 10, 4))
        self.assertEqual(result.mode, "region")
        self.assertEqual(result.image.pixel(0, 0), FakeBackend.color_at(-5, 3))
        self.assertEqual(result.image.pixel(9, 3), FakeBackend.color_at(4, 6))
        self.assertIn(EVENT_COMPLETED, self.events)

    def test_region_is_clamped_to_desktop(self):
        result = self.service.capture_region(Rect(1900, 1070, 100, 100))
        self.assertEqual(result.region, Rect(1900, 1070, 20, 10))

    def test_region_entirely_in_gap_is_rejected(self):
        with self.assertRaises(CaptureError):
            self.service.capture_region(Rect(100, -300, 50, 50))

    def test_monitor_modes(self):
        self.assertEqual(self.service.capture_monitor(2).region, LEFT.bounds)
        self.backend.cursor = Point(-100, 0)
        self.assertEqual(self.service.capture_current_monitor().region, LEFT.bounds)
        self.assertEqual(self.service.capture_all_monitors().region, Rect(-2560, -360, 4480, 1440))
        with self.assertRaises(CaptureError):
            self.service.capture_monitor(9)

    def test_window_capture_foreground_and_at_point(self):
        result = self.service.capture_window()
        self.assertEqual(result.region, EDITOR.bounds)
        self.assertEqual(result.metadata["window_title"], EDITOR.title)
        result = self.service.capture_window_at(Point(-150, 200))
        self.assertEqual(result.region, SPANNING.bounds)

    def test_delayed_capture_counts_down(self):
        ticks = []
        self.bus.subscribe(EVENT_COUNTDOWN, lambda e: ticks.append(e.payload["seconds"]))
        result = self.service.capture_delayed(3, self.service.capture_all_monitors)
        self.assertIsNotNone(result)
        self.assertEqual(ticks, [3, 2, 1])

    def test_delayed_capture_cancel(self):
        cancel = threading.Event()
        service = CaptureService(self.backend, self.bus, sleep=lambda s: cancel.set())
        self.assertIsNone(service.capture_delayed(5, service.capture_all_monitors, cancel=cancel))
        self.assertIn(EVENT_CANCELLED, self.events)
        self.assertEqual(self.backend.grabs, [])

    def test_delay_bounds(self):
        with self.assertRaises(CaptureError):
            self.service.capture_delayed(-1, self.service.capture_all_monitors)

    def test_repeat_last(self):
        with self.assertRaises(CaptureError):
            self.service.repeat_last()
        self.service.capture_region(Rect(0, 0, 5, 5))
        self.assertEqual(self.service.repeat_last().region, Rect(0, 0, 5, 5))

    def test_backend_exception_is_normalised(self):
        class Broken(FakeBackend):
            def grab(self, rect):
                raise OSError("device lost")

        service = CaptureService(Broken([PRIMARY]), self.bus)
        with self.assertLogs("screenpilot.capture.service", level="ERROR"), self.assertRaises(CaptureError):
            service.capture_all_monitors()
        self.assertIn("capture.failed", self.events)


if __name__ == "__main__":
    unittest.main()
