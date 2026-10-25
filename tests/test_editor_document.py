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


class EditorPicturesTests(unittest.TestCase):
    """Pictures placed in the editor (procedures built from several captures)."""

    def test_picture_is_scaled_and_placed(self):
        doc = EditorDocument(300, 200)
        red = Image.new("RGB", (1000, 500), "red")
        index = doc.add_image(red, Point(150, 100))
        shape = doc.shapes[index]
        self.assertEqual(shape.kind, "image")
        self.assertIn(shape.ref, doc.images)
        self.assertLessEqual(shape.bounds.width, 180)  # at most 60% of the canvas
        out = render(checker(), doc).to_pil()
        self.assertTrue(close(out.getpixel((150, 100)), (255, 0, 0)))

    def test_picture_beside_the_capture_grows_a_white_canvas(self):
        doc = EditorDocument(300, 200)
        doc.add_image(Image.new("RGB", (100, 100), "blue"), Point(150, 260), max_size=(100, 100))
        ext = doc.extent()
        self.assertEqual((ext.x, ext.y), (0, 0))
        self.assertGreater(ext.bottom, 300)
        out = render(checker(), doc).to_pil()
        self.assertEqual(out.size, (ext.width, ext.height))
        self.assertTrue(close(out.getpixel((5, ext.height - 5)), (255, 255, 255)))  # white margin
        self.assertTrue(close(out.getpixel((150, 260)), (0, 0, 255)))

    def test_picture_can_be_moved_resized_and_undone(self):
        doc = EditorDocument(300, 200)
        i = doc.add_image(Image.new("RGB", (60, 40), "green"), Point(50, 50))
        moved = doc.shapes[i].moved(100, 20)
        doc.replace(i, moved)
        self.assertEqual(doc.shapes[i].p1, moved.p1)
        bigger = Shape("image", moved.p1, Point(moved.p2.x + 40, moved.p2.y + 20), ref=moved.ref)
        doc.replace(i, bigger)
        self.assertEqual(doc.shapes[i].bounds.width, bigger.bounds.width)
        self.assertTrue(doc.undo())
        self.assertEqual(doc.shapes[i].p1, moved.p1)

    def test_shapes_after_a_picture_are_drawn_on_top(self):
        doc = EditorDocument(300, 200)
        doc.add_image(Image.new("RGB", (100, 100), "blue"), Point(150, 100), max_size=(100, 100))
        doc.add(Shape("rect", Point(120, 70), Point(180, 130), color="#ff0000", fill=True, width=4))
        out = render(checker(), doc).to_pil()
        r, g, b = out.getpixel((150, 100))
        self.assertGreater(r, 40)  # red fill tints the blue picture
        doc.send_to_back(1)
        r2, _g2, b2 = render(checker(), doc).to_pil().getpixel((150, 100))
        self.assertLess(r2, 30)
        self.assertGreater(b2, 200)

    def test_unknown_picture_is_rejected(self):
        doc = EditorDocument(100, 100)
        with self.assertRaises(ValueError):
            doc.add(Shape("image", Point(0, 0), Point(10, 10), ref="missing"))

    def test_symbol_renders(self):
        doc = EditorDocument(300, 200)
        doc.add(Shape("emoji", Point(150, 100), Point(150, 100), text="✅", font_size=40, color="#13a10e"))
        out = render(CapturedImage.from_pil(Image.new("RGB", (300, 200), "white")), doc).to_pil()
        self.assertNotEqual(out.getbbox(), None)
        changed = sum(1 for x in range(130, 170) for y in range(80, 120) if out.getpixel((x, y)) != (255, 255, 255))
        self.assertGreater(changed, 20)

    def test_public_helpers_for_other_engines(self):
        from screenpilot.editor.document import draw_shape, hit_shape, shape_extent

        arrow = Shape("arrow", Point(10, 10), Point(90, 60), width=5)
        self.assertTrue(hit_shape(arrow, Point(50, 35)))
        extent = shape_extent(arrow)
        self.assertTrue(extent.contains(Point(90, 60)) if hasattr(extent, "contains") else extent.right > 90)
        layer = Image.new("RGBA", (120, 90), (0, 0, 0, 0))
        draw_shape(layer, ImageDraw.Draw(layer), arrow, None)
        self.assertGreater(layer.getchannel("A").getbbox()[2], 80)


class HandleGeometryTests(unittest.TestCase):
    """Handle maths used by the editor window (headless, no Qt needed)."""

    def test_box_handles_and_resize_keep_the_opposite_side(self):
        from screenpilot.editor.document import box_handles, resize_box

        handles = box_handles(Rect(10, 20, 100, 50))
        self.assertEqual(set(handles), {"nw", "n", "ne", "e", "se", "s", "sw", "w"})
        self.assertEqual(handles["se"], Point(109, 69))
        self.assertEqual(resize_box(Rect(10, 20, 100, 50), "e", Point(200, 0), False), (10, 20, 200, 69))
        self.assertEqual(resize_box(Rect(10, 20, 100, 50), "nw", Point(500, 500), False, 6), (103, 63, 109, 69))  # min size
        l, t, r, b = resize_box(Rect(0, 0, 100, 50), "se", Point(299, 60), True)
        self.assertAlmostEqual((r - l + 1) / (b - t + 1), 2.0, delta=0.05)  # proportions kept
        self.assertEqual((l, t), (0, 0))

    def test_text_symbols_and_steps_scale_from_a_corner(self):
        from screenpilot.editor.document import scaled, shape_bounds

        for kind, text, size in (("text", "Hello\nworld", 20), ("emoji", "✅", 40), ("step", "", 20)):
            with self.subTest(kind=kind):
                shape = Shape(kind, Point(100, 100), Point(100, 100), text=text, font_size=size)
                b = shape_bounds(shape)
                bigger = scaled(shape, "se", Point(b.right - 1 + b.width, b.bottom - 1 + b.height))
                nb = shape_bounds(bigger)
                self.assertGreater(bigger.font_size, size * 1.6)
                self.assertLessEqual(abs(nb.x - b.x) + abs(nb.y - b.y), 2)  # top-left corner stays
                smaller = scaled(shape, "nw", Point(b.x + b.width // 2, b.y + b.height // 2))
                sb = shape_bounds(smaller)
                self.assertLess(smaller.font_size, size)
                self.assertLessEqual(abs(sb.right - b.right) + abs(sb.bottom - b.bottom), 2)  # bottom-right stays
                tiny = scaled(shape, "nw", Point(b.right + 500, b.bottom + 500))
                self.assertGreaterEqual(tiny.font_size, 6)
