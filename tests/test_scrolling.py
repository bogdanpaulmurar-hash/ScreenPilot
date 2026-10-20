import unittest

from PIL import Image, ImageDraw

from screenpilot.capture import CapturedImage, Rect
from screenpilot.capture.scrolling import ScrollStitcher, StitchError, capture_scrolling, find_shift

W, VIEW_H, HEADER, PAGE_H, SCROLLBAR = 420, 300, 40, 1500, 14


def make_page() -> Image.Image:
    page = Image.new("RGB", (W, PAGE_H), "white")
    d = ImageDraw.Draw(page)
    for i in range(0, PAGE_H, 30):
        d.rectangle((10, i + 4, 10 + (i * 7) % 300 + 40, i + 20), fill=((i * 3) % 255, 80, 160))
        d.text((20, i + 6), f"Line {i // 30}", fill="white")
    # some blank stretches (repeated identical rows) to make matching realistic
    d.rectangle((0, 600, W, 700), fill="white")
    return page


class FakeScrollingWindow:
    """A window with a sticky header, scrollable body and a moving scrollbar thumb."""

    def __init__(self, step: int = 100):
        self.page = make_page()
        self.offset = 0
        self.step = step
        self.scrolls = 0
        self.stop_after: int | None = None

    @property
    def max_offset(self) -> int:
        return PAGE_H - (VIEW_H - HEADER)

    def view(self) -> Image.Image:
        img = Image.new("RGB", (W, VIEW_H), "white")
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, W, HEADER - 1), fill=(30, 30, 60))
        d.text((10, 12), "Sticky header", fill="white")
        img.paste(self.page.crop((0, self.offset, W, self.offset + VIEW_H - HEADER)), (0, HEADER))
        thumb = HEADER + int((VIEW_H - HEADER - 40) * self.offset / max(1, self.max_offset))
        d.rectangle((W - SCROLLBAR, HEADER, W, VIEW_H), fill=(230, 230, 230))
        d.rectangle((W - SCROLLBAR + 3, thumb, W - 3, thumb + 40), fill=(120, 120, 120))
        return img

    def grab(self, rect: Rect) -> CapturedImage:
        return CapturedImage.from_pil(self.view())

    def scroll(self, rect: Rect, notches: int) -> None:
        self.scrolls += 1
        self.offset = min(self.max_offset, self.offset + self.step * notches)

    def stop_requested(self) -> bool:
        return self.stop_after is not None and self.scrolls >= self.stop_after


def expected_full() -> Image.Image:
    win = FakeScrollingWindow()
    out = Image.new("RGB", (W, HEADER + PAGE_H), "white")
    header = win.view().crop((0, 0, W, HEADER))
    out.paste(header, (0, 0))
    out.paste(win.page, (0, HEADER))
    return out


class ScrollingTests(unittest.TestCase):
    def test_find_shift_simple(self):
        rows = [bytes([i]) for i in range(100)]
        self.assertEqual(find_shift(rows[0:50], rows[20:70]), 20)
        self.assertEqual(find_shift(rows[0:50], rows[0:50]), 0)
        self.assertIsNone(find_shift(rows[0:50], [bytes([200 + i % 50]) for i in range(50)]))

    def test_full_page_is_reconstructed_with_sticky_header_once(self):
        win = FakeScrollingWindow()
        image, reason = capture_scrolling(win.grab, Rect(0, 0, W, VIEW_H), win, settle=lambda: None)
        self.assertEqual(reason, "end")
        self.assertEqual((image.width, image.height), (W, HEADER + PAGE_H))
        result = image.to_pil()
        expected = expected_full()
        # Compare everything except the scrollbar strip (it legitimately shows the last thumb position).
        box = (0, 0, W - SCROLLBAR, HEADER + PAGE_H)
        self.assertEqual(result.crop(box).tobytes(), expected.crop(box).tobytes())

    def test_stop_and_limits(self):
        win = FakeScrollingWindow()
        win.stop_after = 2
        image, reason = capture_scrolling(win.grab, Rect(0, 0, W, VIEW_H), win, settle=lambda: None)
        self.assertEqual(reason, "stopped")
        self.assertEqual(image.height, VIEW_H + 2 * win.step)
        win = FakeScrollingWindow()
        image, reason = capture_scrolling(win.grab, Rect(0, 0, W, VIEW_H), win, settle=lambda: None, max_height=700)
        self.assertEqual(reason, "max_height")
        self.assertEqual(image.height, 700)

    def test_non_scrollable_region_ends_immediately(self):
        win = FakeScrollingWindow()
        win.step = 0
        image, reason = capture_scrolling(win.grab, Rect(0, 0, W, VIEW_H), win, settle=lambda: None)
        self.assertEqual(reason, "end")
        self.assertEqual(image.height, VIEW_H)
        self.assertEqual(win.scrolls, 2)

    def test_frame_size_change_is_an_error(self):
        win = FakeScrollingWindow()
        stitcher = ScrollStitcher(win.grab(Rect(0, 0, W, VIEW_H)))
        with self.assertRaises(StitchError):
            stitcher.add(CapturedImage.from_pil(Image.new("RGB", (10, 10))))


if __name__ == "__main__":
    unittest.main()


class ServiceScrollingTests(unittest.TestCase):
    def test_service_publishes_scrolling_capture(self):
        from screenpilot.capture import CaptureService, FakeBackend, Monitor
        from screenpilot.core.events import EventBus

        win = FakeScrollingWindow()
        backend = FakeBackend([Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), True)])
        backend.grab = win.grab
        bus = EventBus()
        seen = []
        bus.subscribe("*", lambda e: seen.append(e.name))
        service = CaptureService(backend, bus)
        result, reason = service.capture_scrolling(Rect(0, 0, W, VIEW_H), win, settle=lambda: None)
        self.assertEqual(reason, "end")
        self.assertEqual(result.mode, "scrolling")
        self.assertEqual(result.image.height, HEADER + PAGE_H)
        self.assertIn("capture.scroll_progress", seen)
        self.assertEqual(seen[-1], "capture.completed")

    def test_cancelled_scrolling_returns_no_result(self):
        from screenpilot.capture import CaptureService, FakeBackend, Monitor
        from screenpilot.core.events import EventBus

        win = FakeScrollingWindow()
        win.stop_after = 1
        backend = FakeBackend([Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), True)])
        backend.grab = win.grab
        bus = EventBus()
        seen = []
        bus.subscribe("*", lambda e: seen.append(e.name))
        service = CaptureService(backend, bus)
        result, reason = service.capture_scrolling(Rect(0, 0, W, VIEW_H), win, settle=lambda: None, on_stop=lambda: "cancel")
        self.assertIsNone(result)
        self.assertEqual(reason, "cancelled")
        self.assertEqual(seen[-1], "capture.cancelled")
        self.assertNotIn("capture.completed", seen)


class EscDuringScrollingTests(unittest.TestCase):
    def _run(self, answers):
        win = FakeScrollingWindow()
        win.stop_after = 2
        asked = []

        def on_stop():
            asked.append(win.scrolls)
            answer = answers.pop(0)
            if answer == "continue":
                win.stop_after = None  # Esc released
            return answer

        image, reason = capture_scrolling(
            win.grab, Rect(0, 0, W, VIEW_H), win, settle=lambda: None, on_stop=on_stop, sleep=lambda s: None
        )
        return image, reason, asked, win

    def test_continue_resumes_until_the_end(self):
        image, reason, asked, win = self._run(["continue"])
        self.assertEqual(reason, "end")
        self.assertEqual(asked, [2])
        self.assertEqual(image.height, HEADER + PAGE_H)

    def test_stop_keeps_what_was_captured(self):
        image, reason, asked, _ = self._run(["stop"])
        self.assertEqual(reason, "stopped")
        self.assertGreater(image.height, VIEW_H)

    def test_cancel_reports_cancelled(self):
        _, reason, _, _ = self._run(["cancel"])
        self.assertEqual(reason, "cancelled")

    def test_without_callback_esc_stops_as_before(self):
        win = FakeScrollingWindow()
        win.stop_after = 1
        _, reason = capture_scrolling(win.grab, Rect(0, 0, W, VIEW_H), win, settle=lambda: None)
        self.assertEqual(reason, "stopped")
