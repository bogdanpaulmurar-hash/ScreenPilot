import unittest

from PIL import Image

from screenpilot.capture import CapturedImage, Point
from screenpilot.editor import PALETTE, StepDocument, render_steps


def white(w=200, h=120) -> CapturedImage:
    return CapturedImage.from_pil(Image.new("RGB", (w, h), "white"))


class StepDocumentTests(unittest.TestCase):
    def test_step_attached_to_detected_element(self):
        from screenpilot.capture import Rect

        doc = StepDocument()
        doc.add_for_frame(Rect(50, 40, 100, 30))
        self.assertEqual(doc.markers, [Point(50, 40)])
        out = render_steps(white(), doc)
        # highlight outline drawn just outside the element's right edge
        self.assertNotEqual(out.pixel(150 + 1, 55), (255, 255, 255))
        self.assertEqual(out.pixel(100, 55), (255, 255, 255))  # inside stays clean
        doc.move(0, Point(10, 10))
        self.assertEqual(doc.steps[0].frame, Rect(50, 40, 100, 30))

    def test_auto_numbering_and_renumber_on_delete(self):
        doc = StepDocument()
        self.assertEqual(doc.add(Point(10, 10)), 1)
        self.assertEqual(doc.add(Point(50, 10)), 2)
        self.assertEqual(doc.add(Point(90, 10)), 3)
        doc.remove(1)
        self.assertEqual([doc.number(i) for i in range(len(doc.markers))], [1, 2])
        self.assertEqual(doc.markers[1], Point(90, 10))

    def test_hit_move_reorder_start_and_undo(self):
        doc = StepDocument()
        doc.add(Point(20, 20))
        doc.add(Point(100, 20))
        self.assertEqual(doc.hit(Point(104, 24)), 1)
        self.assertIsNone(doc.hit(Point(60, 80)))
        doc.move(1, Point(150, 80))
        doc.reorder(1, 0)
        self.assertEqual(doc.markers[0], Point(150, 80))
        doc.set_style(start=5, color="red")
        self.assertEqual(doc.number(0), 5)
        self.assertTrue(doc.undo())
        self.assertEqual((doc.style.start, doc.style.color), (1, "blue"))
        self.assertTrue(doc.undo())
        self.assertTrue(doc.undo())
        self.assertEqual(doc.markers, [Point(20, 20), Point(100, 20)])
        doc.clear()
        self.assertEqual(doc.markers, [])
        self.assertTrue(doc.undo())
        self.assertEqual(len(doc.markers), 2)

    def test_render_draws_coloured_badges_and_keeps_original(self):
        image = white()
        doc = StepDocument()
        doc.add(Point(40, 60))
        doc.add(Point(150, 60))
        out = render_steps(image, doc)
        self.assertEqual((out.width, out.height), (200, 120))
        # Near the badge edge (inside the ring, away from the digit) the fill colour shows.
        r, g, b = out.pixel(40 - 10, 60)
        self.assertLess(abs(r - PALETTE["blue"][0]) + abs(g - PALETTE["blue"][1]) + abs(b - PALETTE["blue"][2]), 40)
        self.assertEqual(out.pixel(100, 10), (255, 255, 255))
        self.assertEqual(image.pixel(40 - 10, 60), (255, 255, 255))  # source untouched

    def test_square_shape_and_two_digit_numbers(self):
        doc = StepDocument()
        doc.set_style(shape="square", size="l", start=9)
        doc.add(Point(30, 30))
        doc.add(Point(100, 30))
        out = render_steps(white(), doc)
        self.assertNotEqual(out.pixel(30 - 14, 30 - 14), (255, 255, 255))  # square corner is filled


if __name__ == "__main__":
    unittest.main()
