import unittest

from screenpilot.capture import Point, Rect, WindowInfo
from screenpilot.capture.selection import SelectionModel, State

BOUNDS = Rect(-1920, 0, 1920, 1080)  # a monitor left of the primary
TOP = WindowInfo(1, "Dialog", Rect(-1500, 200, 400, 300))
BACK = WindowInfo(2, "Main", Rect(-1800, 100, 1200, 800))
OFFSCREEN = WindowInfo(3, "Other monitor", Rect(100, 100, 50, 50))


def model(**kw):
    return SelectionModel(BOUNDS, [TOP, BACK, OFFSCREEN], **kw)


class SelectionModelTests(unittest.TestCase):
    def test_hover_snaps_to_topmost_window_on_this_monitor(self):
        m = model()
        self.assertEqual(len(m.windows), 2)
        m.move(Point(-1400, 300))
        self.assertEqual(m.hover_rect(), TOP.bounds)
        m.move(Point(-1700, 150))
        self.assertEqual(m.hover_rect(), BACK.bounds)
        self.assertIsNone(model(snap=False).hover_rect())

    def test_click_selects_hovered_window(self):
        m = model()
        m.move(Point(-1400, 300))
        m.press(Point(-1400, 300))
        m.release(Point(-1398, 301))  # tiny jitter is still a click
        self.assertEqual(m.result(), TOP.bounds)
        self.assertIs(m.state, State.ADJUSTING)

    def test_click_on_empty_desktop_selects_nothing(self):
        m = model()
        m.move(Point(-50, 1000))
        m.press(Point(-50, 1000))
        m.release(Point(-50, 1000))
        self.assertIsNone(m.result())
        self.assertIs(m.state, State.IDLE)

    def test_drag_any_direction_is_pixel_exact_and_clamped(self):
        m = model()
        m.press(Point(-100, 500))
        m.move(Point(-300, 400))
        self.assertIsNone(m.hover_rect())  # no snapping highlight while dragging
        m.release(Point(-300, 400))
        self.assertEqual(m.result(), Rect(-300, 400, 201, 101))
        m2 = model()
        m2.press(Point(-10, 10))
        m2.release(Point(500, -500))  # beyond monitor edge
        self.assertEqual(m2.result(), Rect(-10, 0, 10, 11))

    def test_move_selection_by_dragging_inside_stays_in_bounds(self):
        m = model()
        m.press(Point(-200, 200))
        m.release(Point(-101, 299))  # 100x100
        m.press(Point(-150, 250))
        self.assertIs(m.state, State.MOVING)
        m.release(Point(5000, 250))
        self.assertEqual(m.result(), Rect(-100, 200, 100, 100))

    def test_resize_with_handles(self):
        m = model()
        m.press(Point(-500, 500))
        m.release(Point(-401, 599))
        self.assertEqual(m.hit_test(Point(-401, 599)), "se")
        m.press(Point(-401, 599))
        self.assertIs(m.state, State.RESIZING)
        m.release(Point(-301, 649))
        self.assertEqual(m.result(), Rect(-500, 500, 200, 150))
        # dragging a handle past the opposite edge flips cleanly
        m.press(Point(-500, 575))  # west handle
        m.release(Point(-250, 575))
        self.assertEqual(m.result(), Rect(-301, 500, 52, 150))

    def test_keyboard_nudge_move_and_resize(self):
        m = model()
        start = m.cursor
        m.nudge(5, -3)
        self.assertEqual(m.cursor, Point(start.x + 5, start.y - 3))
        m.press(Point(-200, 200))
        m.release(Point(-101, 299))
        m.nudge(10, 0)
        self.assertEqual(m.result(), Rect(-190, 200, 100, 100))
        m.nudge(-5, 20, resize=True)
        self.assertEqual(m.result(), Rect(-190, 200, 95, 120))
        m.nudge(-500, 0, resize=True)
        self.assertEqual(m.result().width, 1)

    def test_select_all_and_reset(self):
        m = model()
        m.select_all()
        self.assertEqual(m.result(), BOUNDS)
        m.reset()
        self.assertIsNone(m.result())
        self.assertEqual(m.handle_rects(), {})


if __name__ == "__main__":
    unittest.main()
