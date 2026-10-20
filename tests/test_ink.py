"""Live recording annotations (ink): model, rendering, effects and recorder integration."""

import time
import unittest
from pathlib import Path

from screenpilot.capture import Point, Rect
from screenpilot.recording import BackendFrameSource, EffectOptions, EffectRenderer, Recorder, RecordingOptions
from screenpilot.recording.ink import FADE_OUT_SECONDS, InkLayer, InkRenderer, composite, render_patch
from screenpilot.samples import DemoBackend

RED = (0x38, 0x34, 0xD1)  # #d13438 as B, G, R


def white_frame(w: int, h: int) -> bytearray:
    return bytearray(b"\xff\xff\xff\xff" * (w * h))


def pixel(buf: bytes, w: int, x: int, y: int) -> tuple[int, int, int]:
    i = (y * w + x) * 4
    return buf[i], buf[i + 1], buf[i + 2]


def close(a, b, tol=40) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class InkLayerTests(unittest.TestCase):
    def test_steps_are_numbered_and_undo_gives_the_number_back(self):
        layer = InkLayer(400, 300)
        self.assertEqual(layer.add_step(Point(10, 10)).number, 1)
        self.assertEqual(layer.add_step(Point(50, 10)).number, 2)
        self.assertTrue(layer.undo())
        self.assertEqual(layer.add_step(Point(50, 10)).number, 2)
        layer.clear()
        self.assertEqual(len(layer), 0)
        self.assertEqual(layer.add_step(Point(5, 5)).number, 3)  # numbering continues after "clear"

    def test_tiny_drags_and_empty_text_are_ignored(self):
        layer = InkLayer(400, 300)
        self.assertIsNone(layer.add(layer.make("arrow", Point(10, 10), Point(12, 11))))
        self.assertIsNone(layer.add(layer.make("text", Point(10, 10), text="  ")))
        self.assertIsNotNone(layer.add(layer.make("rect", Point(10, 10), Point(80, 60))))
        self.assertEqual(len(layer), 1)

    def test_points_are_clamped_and_screen_coordinates_converted(self):
        layer = InkLayer(200, 100, origin=Point(1000, 500))
        self.assertEqual(layer.from_screen(Point(1010, 520)), Point(10, 20))
        shape = layer.make("arrow", Point(-50, 40), Point(900, 900))
        self.assertEqual((shape.p1, shape.p2), (Point(0, 40), Point(199, 99)))

    def test_fade_out_and_removal(self):
        clock = Clock()
        layer = InkLayer(200, 100, fade_seconds=3, clock=clock)
        layer.add_step(Point(20, 20))
        self.assertEqual(layer.views()[0].opacity, 1.0)
        clock.now += 3 - FADE_OUT_SECONDS / 2
        self.assertLess(layer.views()[0].opacity, 1.0)
        clock.now += 1
        self.assertEqual(layer.views(), [])
        self.assertTrue(layer.empty)

    def test_draft_is_visible_and_replaced_by_the_final_shape(self):
        layer = InkLayer(300, 200)
        layer.set_draft(layer.make("arrow", Point(10, 10), Point(100, 80)))
        self.assertEqual(len(layer.views()), 1)
        self.assertEqual(len(layer), 0)
        layer.add(layer.make("arrow", Point(10, 10), Point(120, 90)))
        self.assertEqual(len(layer.views()), 1)


class InkRenderingTests(unittest.TestCase):
    def test_step_is_burned_into_the_frame(self):
        w, h = 320, 200
        layer = InkLayer(w, h)
        layer.add_step(Point(100, 80))
        buf = white_frame(w, h)
        self.assertTrue(InkRenderer().apply(buf, w, h, layer))
        self.assertTrue(close(pixel(buf, w, 100 - 12, 80), RED), pixel(buf, w, 88, 80))  # inside the disc, beside the digit
        self.assertEqual(pixel(buf, w, 10, 10), (255, 255, 255))  # far away: untouched

    def test_patches_are_cached_and_dropped_with_the_item(self):
        layer = InkLayer(320, 200)
        layer.add(layer.make("rect", Point(10, 10), Point(100, 80)))
        renderer = InkRenderer()
        first = renderer.patches(layer)
        self.assertIs(renderer.patches(layer)[0], first[0])  # same object: rendered once
        layer.undo()
        self.assertEqual(renderer.patches(layer), [])

    def test_patch_is_clipped_to_the_frame(self):
        layer = InkLayer(100, 100)
        item = layer.add_step(Point(0, 0))
        patch = render_patch(item, 1.0, Rect(0, 0, 100, 100))
        self.assertGreaterEqual(patch.rect.x, 0)
        buf = white_frame(100, 100)
        composite(buf, 100, 100, patch)  # must not raise at the frame edge

    def test_long_arrow_is_composited_as_thin_strips(self):
        from screenpilot.recording.ink import split_patch

        layer = InkLayer(1200, 800)
        item = layer.add(layer.make("arrow", Point(20, 20), Point(1180, 780)))
        patch = render_patch(item, 1.0, Rect(0, 0, 1200, 800))
        strips = split_patch(patch)
        covered = sum(s.rect.width * s.rect.height for s in strips)
        self.assertLess(covered, patch.rect.width * patch.rect.height / 4)
        a, b = white_frame(1200, 800), white_frame(1200, 800)
        composite(a, 1200, 800, patch)
        for s in strips:
            composite(b, 1200, 800, s)
        self.assertEqual(a, b)  # same pixels as compositing the whole patch

    def test_dragged_shape_ends_up_exact_after_the_throttle(self):
        layer = InkLayer(400, 300)
        renderer = InkRenderer()
        layer.set_draft(layer.make("arrow", Point(10, 10), Point(100, 100)))
        first = renderer.strips(layer)
        layer.set_draft(layer.make("arrow", Point(10, 10), Point(300, 250)))
        self.assertEqual(renderer.strips(layer), first)  # throttled: previous drawing reused
        time.sleep(0.1)
        self.assertNotEqual(renderer.strips(layer), first)  # then rendered at its real size

    def test_burn_in_off_and_empty_layer_draw_nothing(self):
        layer = InkLayer(64, 64)
        buf = white_frame(64, 64)
        self.assertFalse(InkRenderer().apply(buf, 64, 64, layer))
        layer.add_step(Point(30, 30))
        layer.burn_in = False
        self.assertFalse(InkRenderer().apply(buf, 64, 64, layer))

    def test_small_annotations_are_cheap_on_a_large_frame(self):
        w, h = 1920, 1080
        layer = InkLayer(w, h)
        for i in range(5):
            layer.add_step(Point(200 + i * 300, 500))
        renderer = InkRenderer()
        buf = white_frame(w, h)
        renderer.apply(buf, w, h, layer)  # warm the cache
        started = time.perf_counter()
        for _ in range(10):
            renderer.apply(buf, w, h, layer)
        per_frame_ms = (time.perf_counter() - started) * 100
        self.assertLess(per_frame_ms, 15, per_frame_ms)


class InkEffectsTests(unittest.TestCase):
    def test_ink_is_drawn_without_other_effects_and_suppresses_click_ripples_while_drawing(self):
        renderer = EffectRenderer(EffectOptions(highlight=False, clicks=True, zoom_factor=2.0))
        self.assertTrue(renderer.active)  # clicks on
        renderer.options.clicks = False
        self.assertFalse(renderer.active)
        layer = InkLayer(200, 120)
        renderer.ink = layer
        self.assertFalse(renderer.active)  # empty ink costs nothing
        layer.add_step(Point(60, 60))
        self.assertTrue(renderer.active)
        out = renderer.apply(bytes(white_frame(200, 120)), 200, 120, None, 0.0)
        self.assertTrue(close(pixel(out, 200, 48, 60), RED))

        renderer.options.clicks = True
        layer.drawing = True
        renderer.feed_buttons((True, False), (10, 10), 0.0)
        self.assertEqual(renderer._ripples, [])
        layer.drawing = False
        renderer.feed_buttons((False, False), (10, 10), 0.1)
        renderer.feed_buttons((True, False), (10, 10), 0.2)
        self.assertEqual(len(renderer._ripples), 1)


class FrameSink:
    def __init__(self) -> None:
        self.output = Path("memory")
        self.frames: list[bytes] = []
        self.frames_written = self.frames_dropped = self.frames_duplicated = 0

    def start(self, ts=0.0):
        pass

    def wait_ready(self, timeout=0):
        return True

    def write(self, frame, ts):
        self.frames.append(frame)
        self.frames_written += 1

    def pause(self):
        pass

    def resume(self, ts):
        pass

    def finish(self):
        return self.output

    def abort(self):
        pass


class InkRecorderTests(unittest.TestCase):
    def test_a_step_added_during_the_recording_is_in_later_frames(self):
        sink = FrameSink()
        opts = RecordingOptions(format="gif", gif_fps=20, highlight_cursor=False, click_effects=False, ink_fade_seconds=0)
        recorder = Recorder(BackendFrameSource(DemoBackend(Rect(0, 0, 640, 400))), lambda w, h: sink, opts)
        recorder.start(Rect(100, 50, 320, 200))
        self.assertEqual(recorder.ink.origin, Point(100, 50))
        time.sleep(0.2)
        before = len(sink.frames)
        recorder.ink.add_step(recorder.ink.from_screen(Point(260, 150)))
        time.sleep(0.3)
        recorder.stop(wait=True, timeout=10)
        self.assertGreater(len(sink.frames), before + 1)
        last = sink.frames[-1]
        self.assertTrue(close(pixel(last, 320, 160 - 12, 100), RED), pixel(last, 320, 148, 100))
        self.assertFalse(close(pixel(sink.frames[0], 320, 148, 100), RED))

    def test_options_validate_ink_settings(self):
        opts = RecordingOptions(ink_color="red", ink_fade_seconds=999)
        self.assertEqual((opts.ink_color, opts.ink_fade_seconds), ("#d13438", 120))


if __name__ == "__main__":
    unittest.main()
