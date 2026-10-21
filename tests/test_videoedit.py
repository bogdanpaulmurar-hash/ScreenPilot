"""Video editing: project model, info parsing, FFmpeg command, real exports (MP4 when FFmpeg exists, GIF always)."""

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

from screenpilot.capture import Point
from screenpilot.core.errors import RecordingError
from screenpilot.recording.ffmpeg import probe
from screenpilot.recording.gif import PillowGifSink
from screenpilot.videoedit import GifReader, VideoEditService, VideoInfo, VideoProject, parse_info
from screenpilot.videoedit.export import build_filter, parse_progress
from screenpilot.videoedit.media import FfmpegFrameReader

FFMPEG = shutil.which("ffmpeg")
RED = (0xD1, 0x34, 0x38)

FFMPEG_I_OUTPUT = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'rec.mp4':
  Metadata:
    major_brand     : isom
  Duration: 00:01:02.53, start: 0.000000, bitrate: 1234 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 1920x1080, 900 kb/s, 30 fps, 30 tbr, 15360 tbn (default)
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 160 kb/s (default)
At least one output file must be specified
"""


def close(a, b, tol=45) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def project(duration=10.0, w=640, h=360) -> VideoProject:
    return VideoProject(Path("v.mp4"), VideoInfo(w, h, duration))


class FakeRecording:
    """RecordingService stand-in: real FFmpeg probe when available."""

    def __init__(self):
        self._info = probe(Path(FFMPEG)) if FFMPEG else None

    def ffmpeg(self, refresh=False):
        return self._info

    def options(self):
        from screenpilot.recording import RecordingOptions

        return RecordingOptions(encoder="software", quality=20)


class ModelTests(unittest.TestCase):
    def test_trim_is_clamped_and_keeps_a_minimum_length(self):
        p = project(10)
        p.set_trim(2, 8)
        self.assertEqual((p.start, p.end, p.output_duration), (2, 8, 6))
        p.set_trim(start=9)  # beyond the end: kept just before it
        self.assertAlmostEqual(p.start, 7.8)
        p.set_trim(end=99)
        self.assertIsNone(p.trim_end)  # end of the video
        self.assertTrue(p.undo() and p.undo())
        self.assertEqual((p.start, p.end), (2, 8))

    def test_steps_are_numbered_by_time_and_renumber_when_moved(self):
        p = project()
        late = p.add(p.make_shape("step", Point(10, 10)), 5, 7)
        early = p.add(p.make_shape("step", Point(50, 10)), 1, 3)
        self.assertEqual(p.step_numbers(), {early.id: 1, late.id: 2})
        p.update(late.id, start=0.5, end=2)
        self.assertEqual(p.step_numbers(), {late.id: 1, early.id: 2})
        p.undo()
        self.assertEqual(p.step_numbers()[early.id], 1)

    def test_active_at_puts_blur_first_and_in_output_shifts_times(self):
        p = project()
        arrow = p.add(p.make_shape("arrow", Point(1, 1), Point(90, 90)), 1, 4)
        blur = p.add(p.make_shape("blur", Point(0, 0), Point(100, 50)), 2, 9)
        self.assertEqual([a.id for a in p.active_at(3)], [blur.id, arrow.id])
        self.assertEqual(p.active_at(4.0), [p.get(blur.id)])  # end is exclusive
        p.set_trim(3, 8)
        out = {a.id: (a.start, a.end) for a in p.in_output()}
        self.assertEqual(out, {arrow.id: (0, 1), blur.id: (0, 5)})

    def test_times_and_points_are_clamped(self):
        p = project(5, 200, 100)
        a = p.add(p.make_shape("rect", Point(-10, 50), Point(999, 999)), 4.95, 99)
        self.assertEqual((a.shape.p1, a.shape.p2), (Point(0, 50), Point(199, 99)))
        self.assertAlmostEqual(a.end - a.start, 0.2)
        self.assertLessEqual(a.end, 5)

    def test_project_round_trips_through_json(self):
        p = project()
        p.set_trim(1, 9)
        p.add(p.make_shape("text", Point(20, 30), text="Click here"), 2, 4)
        p.add(p.make_shape("blur", Point(0, 0), Point(40, 40)), 0, 10)
        data = json.loads(json.dumps(p.to_dict()))
        again = project().apply_dict(data)
        self.assertEqual(again.to_dict(), p.to_dict())
        self.assertFalse(again.can_undo)


class InfoAndCommandTests(unittest.TestCase):
    def test_parse_ffmpeg_info(self):
        info = parse_info(FFMPEG_I_OUTPUT)
        self.assertEqual((info.width, info.height, info.fps, info.codec), (1920, 1080, 30.0, "h264"))
        self.assertAlmostEqual(info.duration, 62.53)
        self.assertTrue(info.has_audio)
        with self.assertRaises(RecordingError):
            parse_info("garbage")

    def test_filter_graph_has_blur_then_overlays_with_time_ranges(self):
        p = project()
        blur = p.add(p.make_shape("blur", Point(10, 10), Point(111, 61)), 1, 3)
        step = p.add(p.make_shape("step", Point(50, 50)), 2, 5)
        graph = build_filter([(p.get(blur.id), None), (p.get(step.id), 0)], [(30, 30)], 640, 360)
        self.assertLess(graph.index("boxblur"), graph.index("[1:v]"))
        self.assertIn("crop=102:52:10:10", graph)  # even size for 4:2:0
        self.assertIn("between(t,1.000,3.000)", graph)
        self.assertIn("overlay=30:30:enable='between(t,2.000,5.000)'", graph)
        self.assertTrue(graph.endswith("format=yuv420p[vout]"))

    def test_progress_lines(self):
        self.assertAlmostEqual(parse_progress("out_time_us=2500000", 10), 0.25)
        self.assertAlmostEqual(parse_progress("out_time_ms=5000000", 10), 0.5)
        self.assertEqual(parse_progress("progress=end", 10), 1.0)
        self.assertIsNone(parse_progress("frame=12", 10))
        self.assertIsNone(parse_progress("out_time_us=N/A", 10))


def make_gif(path: Path, frames=10, w=160, h=100, fps=10) -> Path:
    sink = PillowGifSink(path, width=w, height=h, fps=fps, max_width=0)
    sink.start(0)
    for i in range(frames):
        img = Image.new("RGB", (w, h), (255, 255, 255))
        img.paste((0, 120, 0), (i * 10 % w, 0, i * 10 % w + 10, 10))
        sink.write(img.convert("RGBA").tobytes("raw", "BGRA"), i / fps)
    return sink.finish()


class GifEditTests(unittest.TestCase):
    def test_gif_trim_and_step_without_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_gif(Path(tmp) / "rec.gif", frames=20)
            service = VideoEditService(None)  # no FFmpeg at all
            p = service.open(src)
            self.assertEqual(p.info.kind, "gif")
            self.assertAlmostEqual(p.duration, 2.0, delta=0.05)
            p.set_trim(0.5, 1.5)
            step = p.add(p.make_shape("step", Point(80, 60)), 0.8, 1.2)
            frame = service.preview(p, 1.0)
            self.assertTrue(close(frame.getpixel((80 - 12, 60)), RED), frame.getpixel((68, 60)))
            progress = []
            out = service.export(p, progress=progress.append)
            self.assertEqual(out.name, "rec_edited.gif")
            self.assertEqual(progress[-1], 1.0)
            edited = GifReader(out)
            self.assertAlmostEqual(edited.info.duration, 1.0, delta=0.06)
            self.assertTrue(close(edited.frame(0.4).getpixel((68, 60)), RED))  # 0.8 s in the source
            self.assertFalse(close(edited.frame(0.05).getpixel((68, 60)), RED))
            edited.close()
            self.assertIsNotNone(step)
            self.assertTrue(src.exists())  # the source is never touched

    def test_export_refuses_to_overwrite_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_gif(Path(tmp) / "a.gif")
            service = VideoEditService(None)
            with self.assertRaises(RecordingError):
                service.export(service.open(src), src)

    def test_cancel_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_gif(Path(tmp) / "a.gif")
            service = VideoEditService(None)
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(RecordingError):
                service.export(service.open(src), cancel=cancel)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["a.gif"])

    def test_policy_can_disable_video_editing(self):
        from screenpilot.core.policy import Policy

        service = VideoEditService(None, policy=Policy(disabled=frozenset({"video_edit"})))
        with self.assertRaises(RecordingError) as caught:
            service.open(Path("x.gif"))
        self.assertEqual(caught.exception.message_key, "policy.feature_disabled")


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class Mp4EditTests(unittest.TestCase):
    def make_mp4(self, path: Path, seconds=4) -> Path:
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=c=white:s=320x240:r=25:d={seconds}",
                "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
            ],
            check=True,
        )
        return path

    def test_trim_annotate_blur_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_mp4(Path(tmp) / "rec.mp4")
            service = VideoEditService(FakeRecording())
            p = service.open(src)
            self.assertEqual((p.info.width, p.info.height), (320, 240))
            self.assertTrue(p.info.has_audio)
            p.set_trim(1.0, 3.0)
            p.add(p.make_shape("step", Point(100, 100)), 1.5, 2.5)
            p.add(p.make_shape("rect", Point(180, 40), Point(300, 200)), 0, 4)
            p.add(p.make_shape("blur", Point(0, 0), Point(60, 60)), 0, 4)
            preview = service.preview(p, 2.0)
            self.assertTrue(close(preview.getpixel((88, 100)), RED))
            events = []

            class Bus:
                def publish(self, name, **payload):
                    events.append(name)

            service.bus = Bus()
            progress = []
            out = service.export(p, progress=progress.append)
            self.assertTrue(out.exists())
            self.assertEqual(progress[-1], 1.0)
            self.assertEqual(events, ["video.exported"])
            info = service.open(out).info
            self.assertAlmostEqual(info.duration, 2.0, delta=0.15)
            self.assertTrue(info.has_audio)
            reader = FfmpegFrameReader(out, Path(FFMPEG), info)
            at_step = reader.frame(1.0)  # 2.0 s in the source
            self.assertTrue(close(at_step.getpixel((88, 100)), RED), at_step.getpixel((88, 100)))
            self.assertTrue(close(at_step.getpixel((180, 120)), RED))  # rectangle edge
            before = reader.frame(0.2)  # 1.2 s: step not shown yet
            self.assertTrue(close(before.getpixel((88, 100)), (255, 255, 255)))
            self.assertFalse(list(Path(tmp).glob("*.part")))

    def test_cancel_kills_ffmpeg_and_removes_the_part_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_mp4(Path(tmp) / "rec.mp4", seconds=6)
            service = VideoEditService(FakeRecording())
            p = service.open(src)
            cancel = threading.Event()
            with self.assertRaises(RecordingError):
                service.export(p, progress=lambda v: cancel.set(), cancel=cancel)
            self.assertEqual(sorted(x.name for x in Path(tmp).iterdir()), ["rec.mp4"])

    def test_cli_video_edit_and_project_file(self):
        import contextlib
        import io

        from screenpilot.__main__ import _video_edit, build_parser
        from screenpilot.app import create_app
        from screenpilot.capture import Rect
        from screenpilot.core.paths import AppPaths
        from screenpilot.samples import DemoBackend
        from test_workflow import FakeClipboard, FakeShell

        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_mp4(Path(tmp) / "demo.mp4")
            app = create_app(
                paths=AppPaths.under(Path(tmp) / "app", portable=True),
                backend=DemoBackend(Rect(0, 0, 800, 600)),
                clipboard=FakeClipboard(),
                shell=FakeShell(),
            )
            app.recording.ffmpeg = lambda refresh=False: FakeRecording().ffmpeg()
            try:
                project_file = Path(tmp) / "p.json"
                args = build_parser().parse_args([
                    "video-edit", str(src), "--trim", "1,", "--step", "1.5,3,100,100",
                    "--text", "1.5,3,130,80,Click, then save", "--save-project", str(project_file),
                ])
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(_video_edit(app, args), 0)
                self.assertIn("demo_edited.mp4", out.getvalue())
                data = json.loads(project_file.read_text(encoding="utf-8"))
                self.assertEqual(data["trim"], [1.0, None])
                self.assertEqual(data["annotations"][1]["text"], "Click, then save")  # commas in the text are kept
                again = build_parser().parse_args(["video-edit", str(project_file), "--out", str(Path(tmp) / "again.mp4")])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(_video_edit(app, again), 0)
                self.assertAlmostEqual(app.video.open(Path(tmp) / "again.mp4").duration, 3.0, delta=0.15)
            finally:
                app.shutdown()


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class SalvageTests(unittest.TestCase):
    def test_recording_killed_from_outside_is_still_playable(self):
        import time as _time

        from screenpilot.recording.ffmpeg import FfmpegSink
        from screenpilot.videoedit import VideoEditService

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rec.mp4"
            info = probe(Path(FFMPEG))
            sink = FfmpegSink(info, out, width=320, height=240, fps=10, encoder="libx264")
            sink.start(0.0)
            frame = bytes([0, 0, 255, 255]) * (320 * 240)  # red, BGRA
            for i in range(120):  # 12 s: several keyframes, so several finished fragments
                sink.write(frame, i / 10)
                _time.sleep(0.01)
            _time.sleep(2.0)  # let ffmpeg encode and write them
            sink._proc.kill()  # what security software does
            path = sink.salvage()
            self.assertEqual(path, out)
            self.assertGreater(VideoEditService(FakeRecording()).open(out).duration, 0.5)
            self.assertFalse(list(Path(tmp).glob("*.part*")))
