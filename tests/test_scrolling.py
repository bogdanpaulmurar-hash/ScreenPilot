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


class RealWorldScrollingTests(unittest.TestCase):
    """Situations seen on real machines that the exact row match could not handle."""

    def test_selector_overlay_still_visible_on_the_first_grab(self):
        win = FakeScrollingWindow()
        clock = {"t": 0.0}

        def sleep(s):
            clock["t"] += s

        def grab(rect):
            img = win.view()
            if clock["t"] < 0.4:  # dimmed frozen desktop of the selector, gone a moment later
                img = Image.eval(img, lambda v: v // 2)
            return CapturedImage.from_pil(img)

        image, reason = capture_scrolling(grab, Rect(0, 0, W, VIEW_H), win, sleep=sleep)
        self.assertEqual(reason, "end")
        self.assertEqual(image.height, HEADER + PAGE_H)

    def test_hover_highlight_under_the_parked_cursor(self):
        win = FakeScrollingWindow()

        def grab(rect):
            img = win.view()
            d = ImageDraw.Draw(img)
            mid = VIEW_H // 2
            d.rectangle((0, mid - 12, W - SCROLLBAR - 1, mid + 12), fill=(200, 225, 255))  # row under the cursor
            return CapturedImage.from_pil(img)

        image, reason = capture_scrolling(grab, Rect(0, 0, W, VIEW_H), win, sleep=lambda s: None)
        self.assertEqual(reason, "end")
        self.assertGreaterEqual(image.height, HEADER + PAGE_H - 30)

    def test_rendering_noise_after_each_scroll(self):
        win = FakeScrollingWindow()

        def grab(rect):
            img = win.view()
            # anti-aliasing / colour rounding differs slightly from frame to frame
            k = win.scrolls % 3
            img = Image.eval(img, lambda v: min(255, v + k) if v > 128 else max(0, v - k))
            return CapturedImage.from_pil(img)

        image, reason = capture_scrolling(grab, Rect(0, 0, W, VIEW_H), win, sleep=lambda s: None)
        self.assertEqual(reason, "end")
        self.assertEqual(image.height, HEADER + PAGE_H)

    def test_unrelated_frames_still_stop(self):
        win = FakeScrollingWindow()
        other = Image.new("RGB", (W, VIEW_H), "white")
        d = ImageDraw.Draw(other)
        for y in range(0, VIEW_H, 7):
            d.line((0, y, W, y), fill=((y * 37) % 255, (y * 11) % 255, 0))
        calls = {"n": 0}

        def grab(rect):
            calls["n"] += 1
            return CapturedImage.from_pil(win.view() if win.scrolls == 0 else other)

        _, reason = capture_scrolling(grab, Rect(0, 0, W, VIEW_H), win, sleep=lambda s: None)
        self.assertEqual(reason, "no_match")


class ManualScrollingTests(unittest.TestCase):
    def test_user_scrolls_and_presses_esc(self):
        from screenpilot.capture.scrolling import capture_scrolling_manual

        win = FakeScrollingWindow(step=90)
        state = {"t": 0.0, "polls": 0}

        def sleep(s):
            state["t"] += s

        def grab(rect):
            state["polls"] += 1
            if state["polls"] > 5 and state["polls"] % 2 == 0 and win.offset < win.max_offset:
                win.scroll(rect, 1)  # the user turns the wheel now and then
            return CapturedImage.from_pil(win.view())

        win.stop_requested = lambda: win.offset >= win.max_offset and state["polls"] > 60
        image, reason = capture_scrolling_manual(grab, Rect(0, 0, W, VIEW_H), win, sleep=sleep, clock=lambda: state["t"])
        self.assertEqual(reason, "done")
        self.assertEqual(image.height, HEADER + PAGE_H)

    def test_nothing_scrolled(self):
        from screenpilot.capture.scrolling import capture_scrolling_manual

        win = FakeScrollingWindow()
        state = {"n": 0}

        def stop():
            state["n"] += 1
            return state["n"] > 3

        win.stop_requested = stop
        image, reason = capture_scrolling_manual(win.grab, Rect(0, 0, W, VIEW_H), win, sleep=lambda s: None, clock=lambda: 0.0)
        self.assertEqual(reason, "end")
        self.assertEqual(image.height, VIEW_H)


class OkButtonTests(unittest.TestCase):
    """The OK button on the progress pill finishes like Esc (core side: ``finish`` / ``hold``)."""

    def test_finish_stops_and_keeps_without_asking(self):
        win = FakeScrollingWindow()
        asked = []
        image, reason = capture_scrolling(
            win.grab, Rect(0, 0, W, VIEW_H), win, settle=lambda: None,
            on_stop=lambda: asked.append(1) or "cancel", finish=lambda: win.scrolls >= 2,
        )
        self.assertEqual(reason, "stopped")
        self.assertEqual(image.height, VIEW_H + 2 * win.step)
        self.assertEqual(asked, [])  # no pause question for OK

    def test_esc_still_works_with_finish_wired(self):
        win = FakeScrollingWindow()
        win.stop_after = 1
        _, reason = capture_scrolling(
            win.grab, Rect(0, 0, W, VIEW_H), win, settle=lambda: None, on_stop=lambda: "cancel", finish=lambda: False
        )
        self.assertEqual(reason, "cancelled")

    def test_hold_pauses_scrolling_until_ok(self):
        win = FakeScrollingWindow()
        state = {"held": 0}

        def sleep(s):
            state["held"] += 1

        def finish():
            return state["held"] >= 5

        image, reason = capture_scrolling(
            win.grab, Rect(0, 0, W, VIEW_H), win, settle=lambda: None, sleep=sleep,
            hold=lambda: win.scrolls >= 1, finish=finish,
        )
        self.assertEqual(reason, "stopped")
        self.assertEqual(win.scrolls, 1)  # nothing scrolled while the mouse rested on OK
        self.assertEqual(image.height, VIEW_H + win.step)

    def test_manual_finish(self):
        from screenpilot.capture.scrolling import capture_scrolling_manual

        win = FakeScrollingWindow()
        state = {"n": 0}

        def grab(rect):
            state["n"] += 1
            if state["n"] in (3, 5):
                win.scroll(rect, 1)
            return CapturedImage.from_pil(win.view())

        win.stop_requested = lambda: False
        image, reason = capture_scrolling_manual(
            grab, Rect(0, 0, W, VIEW_H), win, sleep=lambda s: None, clock=lambda: 0.0, finish=lambda: state["n"] >= 8
        )
        self.assertEqual(reason, "done")
        self.assertEqual(image.height, VIEW_H + 2 * win.step)

    def test_service_drops_hold_in_manual_mode(self):
        from screenpilot.capture import CaptureService, FakeBackend, Monitor
        from screenpilot.core.events import EventBus

        win = FakeScrollingWindow()
        win.stop_requested = lambda: True
        backend = FakeBackend([Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), True)])
        backend.grab = win.grab
        service = CaptureService(backend, EventBus())
        _, reason = service.capture_scrolling(
            Rect(0, 0, W, VIEW_H), win, manual=True, sleep=lambda s: None, hold=lambda: True, finish=lambda: False
        )
        self.assertEqual(reason, "end")


class FileListWindow:
    """A file list like Explorer: similar rows, the row under the mouse highlighted, a focused item."""

    ROW, TOP = 43, 40

    def __init__(self, view_h: int, items: int, step_rows: int = 3, hover_y: int | None = None):
        self.view_h, self.items = view_h, items
        self.offset, self.scrolls = 0, 0
        self.step = step_rows * self.ROW
        self.hover_y = view_h // 2 if hover_y is None else hover_y

    @property
    def max_offset(self) -> int:
        return max(0, self.items * self.ROW - (self.view_h - self.TOP))

    @property
    def full_height(self) -> int:
        return self.TOP + self.items * self.ROW

    def view(self) -> Image.Image:
        img = Image.new("RGB", (W, self.view_h), "white")
        d = ImageDraw.Draw(img)
        for i in range(self.items):
            y = self.TOP + i * self.ROW - self.offset
            if y + self.ROW < self.TOP or y > self.view_h:
                continue
            if y <= self.hover_y < y + self.ROW:
                d.rectangle((10, y + 2, W - 40, y + self.ROW - 3), fill=(229, 243, 255))
            if i == 14:
                d.rectangle((10, y + 2, W - 40, y + self.ROW - 3), outline=(0, 0, 0))
            d.rectangle((16, y + 12, 30, y + 28), fill=(240, 200, 80))
            d.text((40, y + 14), f"file {i}.txt", fill=(0, 0, 0))
            d.text((200, y + 14), "27.10.2026", fill=(80, 80, 80))
        d.rectangle((0, 0, W, self.TOP - 1), fill=(245, 245, 245))
        d.text((40, 12), "Name     Date modified", fill=(90, 90, 90))
        return img

    def grab(self, rect: Rect) -> CapturedImage:
        return CapturedImage.from_pil(self.view())

    def scroll(self, rect: Rect, notches: int) -> None:
        self.scrolls += 1
        self.offset = min(self.max_offset, self.offset + self.step * notches)

    def stop_requested(self) -> bool:
        return False


class FileListScrollingTests(unittest.TestCase):
    """Explorer-like lists repeated rows at the end of the list (hover highlight + partial last scroll)."""

    def test_automatic_no_rows_twice(self):
        for view_h in (300, 337, 411, 600, 900):
            for items in (21, 30):
                with self.subTest(view_h=view_h, items=items):
                    win = FileListWindow(view_h, items)
                    image, reason = capture_scrolling(win.grab, Rect(0, 0, W, view_h), win, settle=lambda: None)
                    self.assertEqual(reason, "end")
                    self.assertEqual(image.height, win.full_height)  # one row too many = a row twice

    def test_manual_no_rows_twice(self):
        from screenpilot.capture.scrolling import capture_scrolling_manual

        for view_h in (300, 522, 596, 744):
            with self.subTest(view_h=view_h):
                win = FileListWindow(view_h, 30, step_rows=3, hover_y=view_h // 3)
                polls = {"n": 0}

                def grab(rect, win=win, polls=polls):
                    polls["n"] += 1
                    if polls["n"] > 3 and polls["n"] % 3 == 0 and win.offset < win.max_offset:
                        win.scroll(rect, 1)  # the user turns the wheel with the mouse on the list
                    return CapturedImage.from_pil(win.view())

                win.stop_requested = lambda win=win, polls=polls: win.offset >= win.max_offset and polls["n"] > 10
                image, reason = capture_scrolling_manual(
                    grab, Rect(0, 0, W, view_h), win, sleep=lambda s: None, clock=lambda: 0.0
                )
                self.assertEqual(image.height, win.full_height)



class FloatingBoxWindow(FakeScrollingWindow):
    """A chat: the page scrolls under a floating input box (content shows beside and below it)
    whose text caret blinks."""

    def __init__(self, box_h: int = 90, gap: int = 14, step: int = 70):
        super().__init__(step=step)
        self.box_h, self.gap, self.grabs = box_h, gap, 0

    def view(self) -> Image.Image:
        img = super().view()
        d = ImageDraw.Draw(img)
        y1 = VIEW_H - self.gap
        y0 = y1 - self.box_h
        d.rectangle((60, y0, W - 90, y1), outline=(190, 190, 190), fill="white")
        d.text((75, y0 + 14), "Ask Claude to edit...", fill=(120, 120, 120))
        d.text((75, y1 - 24), "+   Model        Auto", fill=(60, 60, 60))
        self.grabs += 1
        if self.grabs % 2:
            d.line((73, y0 + 10, 73, y0 + 26), fill=(0, 0, 0), width=2)  # blinking caret
        return img


class FloatingBoxScrollingTests(unittest.TestCase):
    def _border_rows(self, image) -> list[int]:
        pil = image.to_pil().convert("RGB")
        px = pil.load()
        return [y for y in range(pil.height) if all(px[x, y] == (190, 190, 190) for x in range(200, 260))]

    def test_box_is_kept_once_at_the_end(self):
        for box, gap, step in ((90, 14, 70), (60, 6, 50), (90, 6, 50), (60, 14, 70)):
            with self.subTest(box=box, gap=gap, step=step):
                win = FloatingBoxWindow(box, gap, step)
                image, reason = capture_scrolling(win.grab, Rect(0, 0, W, VIEW_H), win, sleep=lambda s: None)
                self.assertEqual(reason, "end")
                self.assertAlmostEqual(image.height, HEADER + PAGE_H, delta=box + gap + step)
                borders = self._border_rows(image)
                self.assertEqual(len(borders), 2, borders)  # top and bottom border of ONE box
                self.assertGreaterEqual(borders[0], image.height - box - gap - 2)  # at the very end

    def test_the_box_block_shows_the_end_of_the_page_not_the_start(self):
        """Around and behind the box the page is visible; that block must come from the last
        frame (end of the page), not from the first (a faded section from elsewhere)."""
        win = FloatingBoxWindow(90, 14, 70)
        image, _ = capture_scrolling(win.grab, Rect(0, 0, W, VIEW_H), win, sleep=lambda s: None)
        final = win.view().convert("RGB")
        result = image.to_pil().convert("RGB")
        block = 90 + 14
        strip = (0, 0, 50, block)  # page content left of the box
        got = result.crop((0, result.height - block, 50, result.height))
        want = final.crop((0, VIEW_H - block, 50, VIEW_H))
        self.assertEqual(got.tobytes(), want.tobytes())

    def test_manual_mode_keeps_the_box_once(self):
        from screenpilot.capture.scrolling import capture_scrolling_manual

        win = FloatingBoxWindow(90, 14, 40)
        polls = {"n": 0}

        def grab(rect):
            polls["n"] += 1
            if polls["n"] > 3 and polls["n"] % 3 == 0 and win.offset < win.max_offset:
                win.scroll(rect, 1)
            return CapturedImage.from_pil(win.view())

        win.stop_requested = lambda: win.offset >= win.max_offset and polls["n"] > 10
        image, reason = capture_scrolling_manual(grab, Rect(0, 0, W, VIEW_H), win, sleep=lambda s: None, clock=lambda: 0.0)
        self.assertEqual(len(self._border_rows(image)), 2)

    def test_blinking_caret_does_not_stop_the_view_standing_still(self):
        from screenpilot.capture.scrolling import frames_close, grab_stable

        win = FloatingBoxWindow()
        a, b = win.grab(None), win.grab(None)  # caret on, caret off
        self.assertNotEqual(a.bgra, b.bgra)
        self.assertTrue(frames_close(a, b))
        waited = {"t": 0.0}

        def sleep(s):
            waited["t"] += s

        grab_stable(win.grab, Rect(0, 0, W, VIEW_H), sleep)
        self.assertLess(waited["t"], 0.6)  # settles after two grabs instead of the 1.2 s limit

    def test_a_real_change_is_not_a_caret(self):
        from screenpilot.capture.scrolling import frames_close

        win = FloatingBoxWindow()
        a = win.grab(None)
        win.scroll(None, 1)
        self.assertFalse(frames_close(a, win.grab(None)))
