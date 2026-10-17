import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from screenpilot.branding import ICO_SIZES, render_icon, write_ico
from screenpilot.platform.autostart import VALUE_NAME, Autostart, launch_command, windowless_python
from screenpilot.recording.ffmpeg import candidate_paths


class FakeRegistry:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


class BrandingTests(unittest.TestCase):
    def test_icon_sizes_and_ico_file(self):
        for size in (16, 32, 256):
            image = render_icon(size)
            self.assertEqual(image.size, (size, size))
            self.assertEqual(image.getpixel((0, 0))[3], 0)  # transparent corner
            self.assertEqual(image.getpixel((size // 2, size // 2))[:3][0] > 180, True)  # red dot in the middle
        with tempfile.TemporaryDirectory() as tmp:
            ico = write_ico(Path(tmp) / "a" / "screenpilot.ico")
            with Image.open(ico) as img:
                self.assertEqual(img.format, "ICO")
                self.assertTrue({(n, n) for n in (16, 32, 48, 256)} <= set(img.info["sizes"]))
        self.assertIn(16, ICO_SIZES)


class AutostartTests(unittest.TestCase):
    def test_enable_sync_and_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "python.exe"
            pythonw = Path(tmp) / "pythonw.exe"
            python.write_bytes(b"")
            pythonw.write_bytes(b"")
            self.assertEqual(windowless_python(str(python)), pythonw)
            registry = FakeRegistry()
            autostart = Autostart(registry, executable=str(python))
            self.assertFalse(autostart.is_enabled())
            autostart.sync(True)
            command = registry.values[VALUE_NAME]
            self.assertIn("pythonw.exe", command)
            self.assertTrue(command.endswith("-m screenpilot gui --autostart"))
            registry.values[VALUE_NAME] = '"C:\\old\\pythonw.exe" -m screenpilot gui --autostart'
            self.assertTrue(autostart.is_enabled())
            self.assertFalse(autostart.is_current())
            autostart.sync(True)  # moved installation is repaired
            self.assertTrue(autostart.is_current())
            autostart.sync(False)
            self.assertNotIn(VALUE_NAME, registry.values)

    def test_paths_with_spaces_are_quoted(self):
        command = launch_command(executable="C:\\Users\\Ana Pop\\venv\\Scripts\\pythonw.exe")
        self.assertTrue(command.startswith('"C:\\Users\\Ana Pop'))
        self.assertNotIn("--autostart", command)

    def test_unsupported_platform_is_a_no_op(self):
        autostart = Autostart(registry=None) if sys.platform != "win32" else None
        if autostart is not None:
            autostart.set_enabled(True)
            self.assertFalse(autostart.is_enabled())

    def test_ffmpeg_is_also_looked_up_in_the_data_folder(self):
        paths = candidate_paths("", None, [Path("/data/ScreenPilot")])
        self.assertIn(Path("/data/ScreenPilot") / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"), paths)


class CliTests(unittest.TestCase):
    def test_setup_and_gui_arguments(self):
        from screenpilot.__main__ import build_parser

        args = build_parser().parse_args(["setup", "--start-menu", "--autostart", "on"])
        self.assertTrue(args.start_menu)
        self.assertEqual(args.autostart, "on")
        self.assertTrue(build_parser().parse_args(["gui", "--autostart"]).autostart)


if __name__ == "__main__":
    unittest.main()
