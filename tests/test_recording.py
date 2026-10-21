import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from screenpilot.capture import Point, Rect
from screenpilot.core.errors import RecordingError
from screenpilot.core.policy import Policy
from screenpilot.recording import (
    BackendFrameSource,
    EffectOptions,
    EffectRenderer,
    FfmpegInfo,
    FfmpegSink,
    PillowGifSink,
    Recorder,
    RecordingOptions,
    RecordingService,
    State,
    probe,
)
from screenpilot.recording import effects as fx
from screenpilot.recording.ffmpeg import even, parse_dshow_devices, parse_encoders, parse_version, record_command
from screenpilot.samples import DemoBackend
from screenpilot.settings import Settings

FFMPEG = shutil.which("ffmpeg")


def ImageChops_difference(a, b):  # noqa: N802 - small local helper
    from PIL import ImageChops

    return ImageChops.difference(a, b)


def bgra(width, height, color=(40, 40, 40)):
    r, g, b = color
    return bytes((b, g, r, 0)) * (width * height)


def settings(tmp: Path, **values) -> Settings:
    s = Settings(tmp / "settings.json").load()
    for key, value in values.items():
        s.set(f"recording.{key}", value)
    return s


class OptionsTests(unittest.TestCase):
    def test_values_are_clamped_and_audio_rules(self):
        opts = RecordingOptions(format="avi", fps=500, quality=1, encoder="?", gif_max_width=10, countdown=99)
        self.assertEqual((opts.format, opts.fps, opts.quality, opts.encoder), ("mp4", 60, 16, "auto"))
        self.assertEqual((opts.gif_max_width, opts.countdown), (160, 10))
        self.assertTrue(RecordingOptions(microphone="Mic").has_audio)
        self.assertFalse(RecordingOptions(format="gif", microphone="Mic").has_audio)
        self.assertEqual(RecordingOptions(format="gif", gif_fps=8).frame_rate, 8)

    def test_from_settings_and_policy_strips_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(Path(tmp), fps=24, microphone="Microphone (USB)")
            self.assertEqual(RecordingOptions.from_settings(s).fps, 24)
            service = RecordingService(s, policy=Policy(disabled=frozenset({"record_audio"})))
            self.assertEqual(service.options().microphone, "")
            self.assertEqual(RecordingService(s).options().microphone, "Microphone (USB)")


class EffectsTests(unittest.TestCase):
    W, H = 200, 120

    def test_inactive_renderer_returns_the_same_frame(self):
        renderer = EffectRenderer(EffectOptions(highlight=False, clicks=False))
        frame = bgra(self.W, self.H)
        self.assertIs(renderer.apply(frame, self.W, self.H, (50, 50), 0.0), frame)

    def test_highlight_only_touches_the_cursor_area(self):
        renderer = EffectRenderer(EffectOptions(highlight=True, clicks=False))
        out = renderer.apply(bgra(self.W, self.H), self.W, self.H, (60, 60), 0.0)
        image = Image.frombytes("RGB", (self.W, self.H), out, "raw", "BGRX")
        r, g, b = image.getpixel((60, 60))
        self.assertGreater(r, 60)  # yellow tint: red and green up, blue unchanged
        self.assertGreater(g, 60)
        self.assertLessEqual(b, 41)
        self.assertEqual(image.getpixel((190, 110)), (40, 40, 40))

    def test_click_ripple_starts_on_press_and_fades(self):
        renderer = EffectRenderer(EffectOptions(highlight=False, clicks=True))
        renderer.feed_buttons((True, False), (100, 60), 0.0)
        renderer.feed_buttons((True, False), (100, 60), 0.05)  # still held: no second ripple
        self.assertEqual(len(renderer._ripples), 1)
        out = renderer.apply(bgra(self.W, self.H), self.W, self.H, (100, 60), 0.1)
        self.assertNotEqual(out, bgra(self.W, self.H))
        renderer.feed_buttons((False, False), (100, 60), fx.RIPPLE_SECONDS + 0.1)
        self.assertEqual(renderer._ripples, [])
        self.assertLess(fx.ripple_radius(0.0), fx.ripple_radius(0.5))

    def test_zoom_eases_in_and_out(self):
        renderer = EffectRenderer(EffectOptions(highlight=False, clicks=False, zoom_factor=2.0))
        frame = Image.linear_gradient("L").resize((self.W, self.H)).convert("RGBA").tobytes()
        renderer.toggle_zoom()
        for _ in range(40):
            out = renderer.apply(frame, self.W, self.H, (150, 80), 0.0)
        self.assertAlmostEqual(renderer.zoom, 2.0, places=1)
        self.assertNotEqual(out, frame)
        self.assertEqual(len(out), len(frame))
        renderer.toggle_zoom()
        for _ in range(60):
            out = renderer.apply(frame, self.W, self.H, (150, 80), 0.0)
        self.assertEqual(renderer.zoom, 1.0)


class FfmpegParsingTests(unittest.TestCase):
    def test_version_and_encoders(self):
        self.assertEqual(parse_version("ffmpeg version 7.1-full_build-www.gyan.dev Copyright"), "7.1-full_build-www.gyan.dev")
        listing = (
            "Encoders:\n V..... = Video\n ------\n V....D libx264              libx264 H.264\n"
            " V..... h264_mf              H264 via MediaFoundation\n A....D aac                  AAC\n"
        )
        self.assertEqual(parse_encoders(listing), {"libx264", "h264_mf"})

    def test_dshow_devices_both_layouts(self):
        new = (
            '[dshow @ 0001] "Integrated Camera" (video)\n'
            '[dshow @ 0001]   Alternative name "@device_pnp_\\\\?\\usb#vid"\n'
            '[dshow @ 0001] "Microphone Array (Realtek(R) Audio)" (audio)\n'
            '[dshow @ 0001] "Stereo Mix (Realtek(R) Audio)" (audio)\n'
        )
        old = (
            "[dshow @ 02] DirectShow video devices (some may be both video and audio devices)\n"
            '[dshow @ 02]  "USB Camera"\n'
            "[dshow @ 02] DirectShow audio devices\n"
            '[dshow @ 02]  "Headset Microphone (Jabra)"\n'
            '[dshow @ 02]     Alternative name "@device_cm_{33D9A762}"\n'
        )
        self.assertEqual(
            [n for n, k in parse_dshow_devices(new) if k == "audio"],
            ["Microphone Array (Realtek(R) Audio)", "Stereo Mix (Realtek(R) Audio)"],
        )
        self.assertEqual(parse_dshow_devices(old), [("USB Camera", "video"), ("Headset Microphone (Jabra)", "audio")])

    def test_record_command_orders_audio_first_and_mixes(self):
        args = record_command(
            Path("ffmpeg"), width=640, height=360, fps=30, output=Path("o.mp4"), encoder="libx264", quality=23,
            microphone="Mic", system_audio="Stereo Mix", audio_offset_ms=-120,
        )
        joined = " ".join(args)
        self.assertLess(joined.index("audio=Mic"), joined.index("-i -"))
        self.assertIn("amix=inputs=2", joined)
        self.assertIn("[2:v]scale=out_color_matrix=bt709", joined)
        self.assertIn("-colorspace bt709", joined)
        self.assertIn("-shortest", args)
        self.assertIn("-0.120", args)
        silent = record_command(Path("ffmpeg"), width=640, height=360, fps=30, output=Path("o.mp4"), encoder="h264_mf", quality=23)
        self.assertNotIn("-shortest", silent)
        self.assertIn("-map", silent)
        self.assertIn("[0:v]scale=out_color_matrix=bt709", " ".join(silent))  # colour conversion also without audio
        self.assertEqual((even(641), even(640), even(1)), (640, 640, 2))

    def test_encoder_failure_reason_and_patient_retry(self):
        info = FfmpegInfo(Path("ffmpeg"), "8.0", set())  # encoder list could not be read
        attempts = []

        def runner(args, timeout=0):
            encoder = args[args.index("-c:v") + 1]
            attempts.append((encoder, timeout))
            if encoder == "libx264" and timeout < 60:
                raise subprocess.TimeoutExpired(args, timeout)  # first start held by the antivirus
            if encoder == "libx264":
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 1, "", "Unknown encoder\nConversion failed!")

        self.assertEqual(info.choose_encoder("auto", runner), "libx264")  # software tried although not listed
        self.assertEqual(attempts[-1], ("libx264", 90))
        self.assertEqual(info.errors["mpeg4"], "Conversion failed!")

        broken = FfmpegInfo(Path("ffmpeg"), "8.0", {"libx264"})
        with self.assertRaises(RecordingError) as caught:
            broken.choose_encoder("auto", lambda args, timeout=0: subprocess.CompletedProcess(args, 1, "", "Error: access denied"))
        self.assertIn("access denied", caught.exception.params["reason"])

    def test_encoder_choice_skips_listed_but_broken_hardware(self):
        info = FfmpegInfo(Path("ffmpeg"), "7.0", {"h264_nvenc", "h264_mf", "libx264"})
        calls = []

        def runner(args, timeout=0):
            encoder = args[args.index("-c:v") + 1]
            calls.append(encoder)
            return subprocess.CompletedProcess(args, 0 if encoder == "h264_mf" else 1, "", "")

        self.assertEqual(info.choose_encoder("auto", runner), "h264_mf")
        self.assertEqual(info.choose_encoder("software", runner), "h264_mf")  # libx264 "fails" in this fake
        self.assertEqual(calls, ["h264_nvenc", "h264_mf", "libx264"])  # probes are cached
        with self.assertRaises(RecordingError):
            FfmpegInfo(Path("ffmpeg"), "7.0", {"h264_nvenc"}).choose_encoder("auto", runner)
        info.mark_broken("h264_mf")  # failed in a real recording: never chosen again
        with self.assertRaises(RecordingError):
            info.choose_encoder("auto", runner)

    def test_probe_uses_the_real_encoder_options(self):
        info = FfmpegInfo(Path("ffmpeg"), "7.0", {"h264_mf"})
        seen = []
        info.works("h264_mf", lambda args, timeout=0: seen.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
        self.assertIn("-hw_encoding", seen[0])


class FakeProc:
    def __init__(self):
        self.written = []
        self.closed = threading.Event()
        proc = self

        class Stdin:
            def write(self, data):
                proc.written.append(bytes(data))

            def close(self):
                proc.closed.set()

        class Stderr:
            def readline(self):
                proc.closed.wait(5)
                return b""

        self.stdin, self.stderr = Stdin(), Stderr()

    def wait(self, timeout=None):
        self.closed.wait(timeout)
        return 0

    def kill(self):
        self.closed.set()


class FfmpegSinkTests(unittest.TestCase):
    def test_constant_frame_rate_duplicates_missing_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            procs = []

            def popen(args, **kwargs):
                procs.append(FakeProc())
                Path(args[-1]).write_bytes(b"x")
                return procs[-1]

            sink = FfmpegSink(FfmpegInfo(Path("ffmpeg"), "7"), Path(tmp) / "v.mp4", width=4, height=2, fps=10, popen=popen)
            a, b, c = bgra(4, 2, (1, 1, 1)), bgra(4, 2, (2, 2, 2)), bgra(4, 2, (3, 3, 3))
            sink.start(0.0)
            sink.write(a, 0.0)
            sink.write(b, 0.03)  # same slot as a: replaces nothing, kept as newest
            sink.write(c, 0.42)  # slot 4: slots 1..3 repeat the newest frame
            self.assertTrue(sink.wait_ready(2))
            out = sink.finish()
            self.assertEqual(out.name, "v.mp4")
            self.assertEqual(procs[0].written, [a, b, b, b, c])
            self.assertEqual((sink.frames_written, sink.frames_duplicated), (5, 3))

    def test_rejects_odd_sizes_and_wrong_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                FfmpegSink(FfmpegInfo(Path("ffmpeg"), "7"), Path(tmp) / "v.mp4", width=5, height=2, fps=10)


class GifSinkTests(unittest.TestCase):
    def test_merges_duplicates_and_writes_durations(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = PillowGifSink(Path(tmp) / "a.gif", width=320, height=200, fps=10, max_width=160)
            red, blue = bgra(320, 200, (200, 0, 0)), bgra(320, 200, (0, 0, 200))
            for i, frame in enumerate([red, red, red, blue, blue, red]):
                sink.write(frame, i * 0.1)
            self.assertEqual((sink.frames_written, sink.frames_merged), (3, 3))
            self.assertEqual(sink.durations(), [300, 200, 100])
            path = sink.finish()
            with Image.open(path) as gif:
                self.assertEqual(gif.size, (160, 100))
                self.assertEqual(gif.n_frames, 3)
                self.assertEqual(gif.info.get("loop"), 0)

    def test_full_resolution_frames_are_pixel_exact_and_streamed(self):
        """Patches of changed pixels are written as they arrive and decode to the exact frames."""
        from PIL import ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            w, h = 480, 270
            base = Image.new("RGB", (w, h), "white")
            draw = ImageDraw.Draw(base)
            for y in range(0, h, 18):
                draw.text((6, y), f"Row {y} · 0123456789 ABCDEF", fill=(25, 25, 25))
            draw.rectangle((300, 60, 420, 200), fill=(30, 120, 200))
            frames = []
            for i in range(6):
                frame = base.copy()
                ImageDraw.Draw(frame).ellipse((20 + i * 30, 120, 44 + i * 30, 144), fill=(255, 200, 0))
                frames.append(frame)
            out = Path(tmp) / "f.gif"
            sink = PillowGifSink(out, width=w, height=h, fps=10)
            for i, frame in enumerate(frames):
                r, g, b = frame.split()
                raw = Image.merge("RGBA", (b, g, r, Image.new("L", (w, h), 255))).tobytes()
                sink.write(raw, i * 0.1)
            self.assertTrue(sink.part.exists())  # streamed while recording
            self.assertFalse(out.exists())
            sink.finish()
            self.assertFalse(sink.part.exists())
            with Image.open(out) as gif:
                self.assertEqual((gif.size, gif.n_frames), ((w, h), 6))
                for i, expected in enumerate(frames):
                    gif.seek(i)
                    diff = ImageChops_difference(gif.convert("RGB"), expected)
                    self.assertIsNone(diff.getbbox(), f"frame {i} differs")
                gif.seek(1)
                self.assertEqual(gif.info.get("duration"), 100)

    def test_abort_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = PillowGifSink(Path(tmp) / "x.gif", width=64, height=64, fps=10)
            sink.write(bgra(64, 64, (10, 20, 30)), 0.0)
            sink.write(bgra(64, 64, (90, 20, 30)), 0.1)
            self.assertTrue(sink.part.exists())
            sink.abort()
            self.assertFalse(sink.part.exists())
            self.assertFalse(sink.output.exists())

    def test_memory_cap_marks_the_sink_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = PillowGifSink(Path(tmp) / "a.gif", width=64, height=64, fps=10)
            sink.max_frames = 3
            for i in range(5):
                sink.write(bgra(64, 64, (i * 40, 0, 0)), i * 0.1)
            self.assertTrue(sink.full)
            self.assertEqual(sink.frames_dropped, 2)
            sink.abort()  # closes the .part file (Windows cannot delete an open file)


class MovingCursorBackend(DemoBackend):
    def __init__(self, bounds):
        super().__init__(bounds)
        self.t0 = time.monotonic()

    def cursor_position(self) -> Point:
        t = time.monotonic() - self.t0
        return Point(int(100 + 300 * (t % 1)), 200)


class RecorderTests(unittest.TestCase):
    def test_gif_recording_end_to_end_with_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = MovingCursorBackend(Rect(0, 0, 1280, 800))
            source = BackendFrameSource(backend)
            opts = RecordingOptions(format="gif", gif_fps=10)
            out = Path(tmp) / "r.gif"
            events = []

            class Bus:
                def publish(self, name, **payload):
                    events.append((name, payload.get("state")))

            recorder = Recorder(
                source, lambda w, h: PillowGifSink(out, width=w, height=h, fps=10, max_width=480), opts, bus=Bus()
            )
            recorder.start(Rect(101, 51, 641, 401))  # odd size is made even
            self.assertEqual((recorder.rect.width, recorder.rect.height), (640, 400))
            time.sleep(0.5)
            recorder.pause()
            time.sleep(0.3)
            self.assertEqual(recorder.state, State.PAUSED)
            paused_at = recorder.elapsed
            time.sleep(0.2)
            self.assertAlmostEqual(recorder.elapsed, paused_at, delta=0.01)
            recorder.resume()
            time.sleep(0.4)
            result = recorder.stop(wait=True, timeout=10)
            self.assertIsNotNone(result, recorder.error)
            self.assertEqual(recorder.state, State.DONE)
            self.assertTrue(0.6 < result.duration < 1.6, result.duration)
            self.assertGreater(result.frames_captured, 4)
            self.assertTrue(out.exists())
            names = [e[0] for e in events]
            self.assertIn("recording.finished", names)
            self.assertIn("paused", [e[1] for e in events])

    def test_cancel_removes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.gif"
            recorder = Recorder(
                BackendFrameSource(DemoBackend(Rect(0, 0, 640, 400))),
                lambda w, h: PillowGifSink(out, width=w, height=h, fps=10),
                RecordingOptions(format="gif"),
            )
            recorder.start(Rect(0, 0, 320, 200))
            time.sleep(0.2)
            recorder.cancel()
            self.assertTrue(recorder.join(5))
            self.assertEqual(recorder.state, State.CANCELLED)
            self.assertFalse(out.exists())

    def test_too_small_area_is_refused(self):
        recorder = Recorder(BackendFrameSource(DemoBackend(Rect(0, 0, 640, 400))), None, RecordingOptions())
        with self.assertRaises(RecordingError):
            recorder.start(Rect(0, 0, 10, 10))


class ServiceTests(unittest.TestCase):
    def test_mp4_without_ffmpeg_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("screenpilot.recording.ffmpeg.shutil.which", return_value=None):
            service = RecordingService(
                settings(Path(tmp), ffmpeg_path=str(Path(tmp) / "missing.exe")),
                default_dir=lambda: Path(tmp),
                ffmpeg_search_dirs=lambda: [],  # CI machines may have ffmpeg in the package-manager folders
            )
            self.assertIsNone(service.ffmpeg())
            with self.assertRaises(RecordingError) as ctx:
                service.prepare()
            self.assertEqual(ctx.exception.message_key, "recording.error.no_ffmpeg")
            service.current = None
            gif = service.prepare(RecordingOptions(format="gif"))  # GIF needs no ffmpeg
            self.assertEqual(gif.encoder, "gif")

    def test_policy_blocks_recording_and_paths_are_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(Path(tmp), output_dir=str(Path(tmp) / "videos"))
            blocked = RecordingService(s, policy=Policy(disabled=frozenset({"recording"})))
            with self.assertRaises(RecordingError):
                blocked.prepare(RecordingOptions(format="gif"))
            service = RecordingService(s)
            first = service.output_path("mp4")
            first.parent.mkdir(parents=True)
            first.write_bytes(b"")
            self.assertNotEqual(service.output_path("mp4", now=None), first)
            self.assertEqual(first.parent, Path(tmp) / "videos")


    def test_ffmpeg_installed_while_running_is_found_without_restart(self):
        """A missing ffmpeg is not cached: installing it later works at the next recording."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch("screenpilot.recording.ffmpeg.shutil.which", return_value=None):
            folder = Path(tmp) / "WinGet" / "Links"
            probed = []

            def prober(path):
                probed.append(path)
                return FfmpegInfo(path, "7.1")

            service = RecordingService(
                settings(Path(tmp)), default_dir=lambda: Path(tmp), ffmpeg_prober=prober, ffmpeg_search_dirs=lambda: [folder]
            )
            self.assertIsNone(service.ffmpeg())
            folder.mkdir(parents=True)
            exe = folder / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
            exe.write_bytes(b"")
            info = service.ffmpeg()
            self.assertIsNotNone(info)
            self.assertEqual(info.path, exe)
            self.assertIs(service.ffmpeg(), info)  # found: cached, probed once
            self.assertEqual(len(probed), 1)
            exe.unlink()
            self.assertIsNone(service.ffmpeg())  # uninstalled: noticed too

    def test_candidate_paths_order_and_configured_folder(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("screenpilot.recording.ffmpeg.shutil.which", return_value=None):
            from screenpilot.recording.ffmpeg import candidate_paths

            exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
            configured = Path(tmp) / "tools"
            configured.mkdir()
            paths = candidate_paths(f'"{configured}"', Path(tmp) / "app", search_dirs=lambda: [Path(tmp) / "winget", Path(tmp) / "winget"])
            self.assertEqual(paths[0], configured / exe)  # a folder (quotes stripped) means the exe inside it
            self.assertEqual(paths[1], Path(tmp) / "app" / exe)
            self.assertEqual(paths[-1], Path(tmp) / "winget" / exe)
            self.assertEqual(len(paths), len(set(paths)))  # no duplicates


class CliRecordTests(unittest.TestCase):
    def test_record_command_writes_a_gif_and_info_runs(self):
        import contextlib
        import io

        from screenpilot.__main__ import _record, _record_info, build_parser
        from screenpilot.app import create_app
        from screenpilot.core.paths import AppPaths
        from test_workflow import FakeClipboard, FakeShell

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                paths=AppPaths.under(Path(tmp), portable=True),
                backend=DemoBackend(Rect(0, 0, 1280, 800)),
                clipboard=FakeClipboard(),
                shell=FakeShell(),
            )
            try:
                out = Path(tmp) / "cli.gif"
                args = build_parser().parse_args(
                    ["record", "--rect", "0,0,320,200", "--seconds", "0.5", "--out", str(out), "--fps", "8"]
                )
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(_record(app, args), 0)
                    _record_info(app)
                self.assertTrue(out.exists())
                self.assertIn("cli.gif", buffer.getvalue())
                self.assertIn("FFmpeg", buffer.getvalue())
            finally:
                app.shutdown()


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class RealFfmpegTests(unittest.TestCase):
    def duration(self, path: Path) -> float:
        proc = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
        h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", proc.stderr).groups()
        return int(h) * 3600 + int(m) * 60 + float(s)

    def test_probe_finds_a_working_encoder(self):
        info = probe(Path(FFMPEG))
        self.assertNotEqual(info.version, "unknown")
        self.assertTrue(info.choose_encoder("auto"))

    def test_mp4_recording_with_pause_joins_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(Path(tmp), fps=15, output_dir=str(Path(tmp) / "rec"))
            backend = MovingCursorBackend(Rect(0, 0, 1280, 800))
            service = RecordingService(s, backend=backend, source_factory=lambda: BackendFrameSource(backend))
            recorder = service.start(Rect(200, 100, 480, 270))
            time.sleep(1.0)
            recorder.pause()
            time.sleep(0.4)
            recorder.resume()
            time.sleep(1.0)
            recorder.toggle_zoom()
            time.sleep(0.3)
            result = recorder.stop(wait=True, timeout=60)
            self.assertIsNotNone(result, recorder.error)
            self.assertTrue(result.path.exists())
            self.assertEqual(result.path.suffix, ".mp4")
            self.assertEqual(list(result.path.parent.glob("*.part*")), [])
            self.assertAlmostEqual(self.duration(result.path), result.duration, delta=0.5)
            self.assertFalse(service.busy)
            gif = service.to_gif(result.path, fps=8, max_width=240)
            with Image.open(gif) as image:
                self.assertEqual(image.size[0], 240)
                self.assertGreater(image.n_frames, 3)


if __name__ == "__main__":
    unittest.main()
