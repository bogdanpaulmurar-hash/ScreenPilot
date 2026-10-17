import time
import unittest

from PIL import Image, ImageDraw

from screenpilot.capture import CapturedImage, Point, Rect
from screenpilot.editor.document import EditorDocument, Shape, render


def checker(w=300, h=200) -> CapturedImage:
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for x in range(0, w, 10):
        for y in range(0, h, 10):
            if (x // 10 + y // 10) % 2:
                d.rectangle((x, y, x + 9, y + 9), fill="black")
    return CapturedImage.from_pil(img)


def close(a, b, tol=40):
    return sum(abs(x - y) for x, y in zip(a, b)) <= tol


class EditorDocumentTests(unittest.TestCase):
    def test_add_undo_redo_and_remove(self):
        doc = EditorDocument(300, 200)
        doc.add(Shape("rect", Point(10, 10), Point(50, 50)))
        doc.add(Shape("arrow", Point(0, 0), Point(100, 100)))
        self.assertEqual(len(doc.shapes), 2)
        self.assertTrue(doc.undo())
        self.assertEqual(len(doc.shapes), 1)
        self.assertTrue(doc.redo())
        self.assertEqual(doc.shapes[1].kind, "arrow")
        doc.remove(0)
        self.assertEqual([s.kind for s in doc.shapes], ["arrow"])
        with self.assertRaises(ValueError):
            doc.add(Shape("star", Point(0, 0), Point(1, 1)))

    def test_step_numbers_follow_order(self):
        doc = EditorDocument(300, 200)
        a = doc.add(Shape("step", Point(20, 20), Point(20, 20)))
        doc.add(Shape("rect", Point(0, 0), Point(5, 5)))
        b = doc.add(Shape("step", Point(60, 20), Point(60, 20)))
        c = doc.add(Shape("step", Point(100, 20), Point(100, 20)))
        self.assertEqual([doc.step_number(i) for i in (a, b, c)], [1, 2, 3])
        doc.remove(a)
        self.assertEqual(doc.step_number(b - 1), 1)
        doc.step_start = 5
        self.assertEqual(doc.step_number(c - 1), 6)

    def test_hit_testing(self):
        doc = EditorDocument(300, 200)
        doc.add(Shape("rect", Point(10, 10), Point(110, 110), width=4))
        doc.add(Shape("line", Point(0, 150), Point(200, 150), width=4))
        self.assertEqual(doc.hit(Point(12, 60)), 0)  # on the outline
        self.assertIsNone(doc.hit(Point(60, 60)))  # inside an unfilled rect
        self.assertEqual(doc.hit(Point(100, 152)), 1)
        doc.replace(0, Shape("rect", Point(10, 10), Point(110, 110), fill=True))
        self.assertEqual(doc.hit(Point(60, 60)), 0)

    def test_render_shapes_effects_and_crop(self):
        image = checker()
        doc = EditorDocument(300, 200)
        doc.add(Shape("blur", Point(0, 0), Point(99, 99), strength=8))
        doc.add(Shape("pixelate", Point(200, 0), Point(299, 99), strength=20))
        doc.add(Shape("rect", Point(120, 120), Point(180, 180), color="#13a10e", width=6))
        doc.add(Shape("highlight", Point(0, 150), Point(100, 190), color="#ffb900"))
        doc.add(Shape("text", Point(200, 150), Point(200, 150), text="Hi", fill=True, color="#0067c0"))
        out = render(image, doc)
        # blur: checker pattern averaged to grey
        self.assertTrue(close(out.pixel(50, 50), (128, 128, 128), 90))
        # pixelate: 20 px blocks are uniform
        self.assertEqual(out.pixel(201, 1), out.pixel(218, 18))
        # rect outline green
        self.assertTrue(close(out.pixel(121, 150), (19, 161, 14)))
        # outside everything untouched
        self.assertEqual(out.pixel(150, 105), image.pixel(150, 105))
        # source untouched
        self.assertEqual(image.pixel(55, 55), checker().pixel(55, 55))
        doc.set_crop(Rect(100, 100, 120, 80))
        cropped = render(image, doc)
        self.assertEqual((cropped.width, cropped.height), (120, 80))

    def test_spotlight_dims_outside(self):
        image = CapturedImage.from_pil(Image.new("RGB", (100, 100), "white"))
        doc = EditorDocument(100, 100)
        doc.add(Shape("spotlight", Point(30, 30), Point(70, 70)))
        out = render(image, doc)
        self.assertEqual(out.pixel(50, 50), (255, 255, 255))
        self.assertLess(out.pixel(5, 5)[0], 200)

    def test_render_speed_1080p(self):
        image = CapturedImage.from_pil(Image.new("RGB", (1920, 1080), "white"))
        doc = EditorDocument(1920, 1080)
        for i in range(20):
            doc.add(Shape("arrow", Point(10 + i * 50, 10), Point(300 + i * 50, 400)))
        doc.add(Shape("blur", Point(100, 100), Point(500, 400)))
        start = time.perf_counter()
        render(image, doc)
        self.assertLess(time.perf_counter() - start, 1.5)


if __name__ == "__main__":
    unittest.main()
