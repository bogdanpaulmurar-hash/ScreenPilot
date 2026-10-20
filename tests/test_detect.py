import time
import unittest

from screenpilot.capture import CaptureService, Point, Rect, WindowInfo
from screenpilot.capture.detect import FrameFinder
from screenpilot.capture.selection import SelectionModel
from screenpilot.samples import DemoBackend


class FrameDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = DemoBackend(Rect(-1920, 0, 1920, 1080))  # negative coordinates on purpose
        cls.frozen = CaptureService(cls.backend).freeze()
        cls.finder = FrameFinder(cls.frozen.image, Point(cls.frozen.region.x, cls.frozen.region.y))
        cls.erp = cls.backend.list_windows()[0].bounds

    def test_input_field_is_innermost_frame_including_its_border(self):
        erp = self.erp
        boxes = self.finder.boxes_at(Point(erp.x + 300, erp.y + 70))
        field = boxes[0]
        # The demo draws the "Customer" field at erp.x+170 .. erp.right-24, height 27 incl. 1 px border.
        self.assertEqual((field.x, field.right), (erp.x + 170, erp.right - 23))
        self.assertEqual(field.height, 27)
        self.assertTrue(field.contains(Point(erp.x + 300, erp.y + 70)))

    def test_parents_are_nested_up_to_the_window(self):
        erp = self.erp
        boxes = self.finder.boxes_at(Point(erp.x + 100, erp.y + 300))
        self.assertEqual(boxes[-1], erp)
        for inner, outer in zip(boxes, boxes[1:]):
            self.assertEqual(outer.intersect(inner), inner)

    def test_button_with_rounded_corners(self):
        erp = self.erp
        boxes = self.finder.boxes_at(Point(erp.right - 60, erp.bottom - 30))
        self.assertEqual(boxes[0], Rect(erp.right - 112, erp.bottom - 48, 89, 33))

    def test_text_lines_are_not_boxes_and_detection_is_fast(self):
        report = self.backend.list_windows()[1].bounds
        start = time.perf_counter()
        boxes = self.finder.boxes_at(Point(report.x + 200, report.y + 140))
        self.assertLess(time.perf_counter() - start, 0.25)
        self.assertTrue(all(b.height > 40 for b in boxes))  # no box made of two text bars
        self.assertEqual(self.finder.boxes_at(Point(10_000, 10)), [])


class SelectionFramesTests(unittest.TestCase):
    def test_wheel_levels_from_field_to_window(self):
        window = WindowInfo(1, "App", Rect(0, 0, 400, 300))
        field = Rect(50, 50, 100, 20)
        panel = Rect(20, 30, 300, 200)
        m = SelectionModel(Rect(0, 0, 800, 600), [window], frames=lambda p, w: [panel, field, Rect(900, 900, 5, 5)])
        m.move(Point(60, 60))
        self.assertEqual(m.hover_candidates(), [field, panel, window.bounds])
        self.assertEqual(m.hover_rect(), field)
        m.cycle(+1)
        self.assertEqual(m.hover_rect(), panel)
        m.cycle(+5)
        self.assertEqual(m.hover_rect(), window.bounds)
        m.cycle(-1)
        m.press(Point(60, 60))
        m.release(Point(60, 60))
        self.assertEqual(m.result(), panel)  # click picks the level shown

    def test_level_resets_on_other_frame(self):
        window = WindowInfo(1, "App", Rect(0, 0, 400, 300))
        a, b = Rect(10, 10, 50, 50), Rect(100, 100, 50, 50)
        m = SelectionModel(Rect(0, 0, 800, 600), [window], frames=lambda p, w: [r for r in (a, b) if r.contains(p)])
        m.move(Point(20, 20))
        m.cycle(+1)
        self.assertEqual(m.hover_rect(), window.bounds)
        m.move(Point(110, 110))
        self.assertEqual(m.hover_rect(), b)


if __name__ == "__main__":
    unittest.main()


class SpreadsheetGridTests(unittest.TestCase):
    """Excel / Google Sheets: every row and column line is a candidate side.

    The exhaustive search took ~13 s per mouse position on a 1080p grid and
    froze the selector; it must now stay within a few tens of milliseconds.
    """

    @classmethod
    def setUpClass(cls):
        from PIL import Image, ImageDraw

        from screenpilot.capture import CapturedImage

        img = Image.new("RGB", (1920, 1080), "white")
        draw = ImageDraw.Draw(img)
        for x in range(40, 1920, 64):
            draw.line([(x, 150), (x, 1079)], fill=(212, 212, 212))
        for y in range(150, 1080, 20):
            draw.line([(40, y), (1919, y)], fill=(212, 212, 212))
        cls.finder = FrameFinder(CapturedImage.from_pil(img), Point(0, 0))

    def test_cell_is_found_fast(self):
        started = time.perf_counter()
        boxes = self.finder.boxes_at(Point(500, 500))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5, f"frame search took {elapsed * 1000:.0f} ms")
        self.assertTrue(boxes)
        cell = boxes[0]
        self.assertEqual((cell.x, cell.y, cell.width, cell.height), (488, 490, 65, 21))  # one cell, incl. its grid lines
        for inner, outer in zip(boxes, boxes[1:]):
            self.assertEqual(outer.intersect(inner), inner)

    def test_every_point_stays_fast(self):
        started = time.perf_counter()
        for x in range(60, 1900, 97):
            self.finder.boxes_at(Point(x, 300 + x % 700))
        per_point = (time.perf_counter() - started) / len(range(60, 1900, 97))
        self.assertLess(per_point, 0.1)

    def test_repeated_point_is_cached(self):
        first = self.finder.boxes_at(Point(700, 800))
        self.assertEqual(self.finder.boxes_at(Point(700, 800)), first)
