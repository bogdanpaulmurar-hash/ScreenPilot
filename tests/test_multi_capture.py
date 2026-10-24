"""Multi-capture (Ctrl held in the selector): headless parts.

The Qt parts (selector, clipboard sequence, editor) are in ``test_ui.py``
(``MultiCaptureUiTests``).
"""

from __future__ import annotations

import unittest

from screenpilot.capture import CapturedImage, Point, Rect
from screenpilot.capture.image import stack_images, stack_offsets
from screenpilot.capture.selection import RegionList


def solid(width: int, height: int, rgb: tuple[int, int, int]) -> CapturedImage:
    r, g, b = rgb
    return CapturedImage(width, height, bytes([b, g, r, 255]) * (width * height))


class RegionListTests(unittest.TestCase):
    def test_add_keeps_order_and_ignores_empty_and_repeats(self):
        regions = RegionList()
        a, b = Rect(0, 0, 10, 10), Rect(-500, 20, 30, 40)  # b is on a monitor left of the primary
        self.assertTrue(regions.add(a))
        self.assertFalse(regions.add(a))  # same region twice in a row (double release)
        self.assertFalse(regions.add(Rect(5, 5, 0, 0)))
        self.assertFalse(regions.add(None))
        self.assertTrue(regions.add(b))
        self.assertEqual(list(regions), [a, b])
        self.assertEqual(len(regions), 2)
        self.assertEqual((regions.number(a), regions.number(b), regions.number(Rect(1, 1, 1, 1))), (1, 2, 0))

    def test_pop_and_remove_at(self):
        regions = RegionList()
        a, b, c = Rect(0, 0, 100, 100), Rect(50, 50, 100, 100), Rect(300, 0, 10, 10)
        for r in (a, b, c):
            regions.add(r)
        self.assertEqual(regions.pop(), c)
        self.assertEqual(regions.remove_at(Point(60, 60)), b)  # overlap: the most recent one goes
        self.assertIsNone(regions.remove_at(Point(900, 900)))
        self.assertEqual(list(regions), [a])
        regions.clear()
        self.assertIsNone(regions.pop())
        self.assertEqual(len(regions), 0)

    def test_final_appends_the_current_selection_once(self):
        regions = RegionList()
        a, b = Rect(0, 0, 10, 10), Rect(20, 0, 10, 10)
        regions.add(a)
        self.assertEqual(regions.final(b), [a, b])
        self.assertEqual(regions.final(a), [a])
        self.assertEqual(regions.final(None), [a])
        self.assertEqual(RegionList().final(b), [b])


class StackTests(unittest.TestCase):
    def test_offsets(self):
        self.assertEqual(stack_offsets([100, 50, 30], gap=16), [0, 116, 182])
        self.assertEqual(stack_offsets([]), [])

    def test_stack_images_one_below_the_other(self):
        red, blue = solid(40, 20, (255, 0, 0)), solid(60, 10, (0, 0, 255))
        out = stack_images([red, blue], gap=4)
        self.assertEqual((out.width, out.height), (60, 34))
        self.assertEqual(out.pixel(0, 0), (255, 0, 0))
        self.assertEqual(out.pixel(50, 5), (255, 255, 255))  # right of the narrower picture
        self.assertEqual(out.pixel(10, 22), (255, 255, 255))  # the gap
        self.assertEqual(out.pixel(59, 33), (0, 0, 255))

    def test_stack_single_and_empty(self):
        red = solid(4, 4, (255, 0, 0))
        self.assertIs(stack_images([red]), red)
        with self.assertRaises(ValueError):
            stack_images([])


if __name__ == "__main__":
    unittest.main()
