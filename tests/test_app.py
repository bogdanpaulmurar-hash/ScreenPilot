import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from screenpilot import __main__ as cli
from screenpilot.app import create_app
from screenpilot.capture import CaptureService, Rect
from screenpilot.core.logging_setup import close_logging
from screenpilot.core.paths import AppPaths
from screenpilot.i18n import Translator

from fixtures import make_backend
from test_workflow import FakeClipboard, FakeShell


class AppTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = AppPaths.under(Path(self._tmp.name), portable=True)

    def tearDown(self):
        close_logging()  # Windows cannot delete an open log file
        self._tmp.cleanup()

    def make(self, **kw):
        return create_app(paths=self.paths, backend=make_backend(), clipboard=FakeClipboard(), shell=FakeShell(), **kw)

    def test_capture_and_default_workflow(self):
        app = self.make()
        result = app.capture.capture_region(Rect(0, 0, 20, 10))
        steps, ctx = app.run_after_capture(result)
        self.assertEqual([s.action_id for s in steps], ["save", "copy_image"])
        self.assertTrue(ctx.saved_path.is_relative_to(self.paths.captures_dir))
        self.assertIs(app.clipboard.image, result.image)
        self.assertEqual(app.history.entries()[0].path, str(ctx.saved_path))

    def test_history_limit_setting_is_live(self):
        app = self.make()
        self.assertEqual(app.history.limit, 100)
        app.settings.set("history.limit", 20)
        self.assertEqual(app.history.limit, 20)

    def test_language_setting_switches_translator_live(self):
        app = self.make()
        self.assertEqual(app.translator.t("settings.title"), "Settings")
        app.settings.set("general.language", "ro")
        self.assertEqual(app.translator.t("settings.title"), "Setări")

    def test_state_persists_across_restarts(self):
        app = self.make()
        app.settings.set("general.language", "ro")
        app.hotkeys.assign("capture.region", "Ctrl+Alt+Q")
        app.shutdown()
        app2 = self.make()
        self.assertEqual(app2.translator.language, "ro")
        self.assertEqual(str(app2.hotkeys.binding("capture.region")), "Ctrl+Alt+Q")

    def test_services_resolvable_for_automation(self):
        app = self.make()
        self.assertIs(app.services.get(CaptureService), app.capture)
        self.assertIs(app.services.get(Translator), app.translator)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"SCREENPILOT_HOME": tmp}), mock.patch(
            "screenpilot.app.container.default_backend", make_backend
        ), mock.patch("screenpilot.app.container.default_clipboard", FakeClipboard), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
            files = sorted(p.name for p in Path(tmp, "captures").glob("*"))
        return code, out.getvalue(), err.getvalue(), files

    def test_version(self):
        code, out, _, _ = self.run_cli("version")
        self.assertEqual(code, 0)
        self.assertIn("ScreenPilot 0.3.0", out)

    def test_monitors_in_romanian(self):
        code, out, _, _ = self.run_cli("--lang", "ro", "monitors")
        self.assertEqual(code, 0)
        self.assertIn("Monitoare detectate:", out)
        self.assertIn("Monitor 2: 2560×1440 la (-2560, -360)", out)
        self.assertIn("[principal]", out)

    def test_capture_region_runs_workflow(self):
        code, out, _, files = self.run_cli("capture", "region", "--rect", "0,0,30,20")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(files), 1)
        self.assertIn("Save: done", out)

    def test_capture_to_explicit_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.png"
            code, out, _, _ = self.run_cli("capture", "monitor", "--index", "1", "--out", str(target))
            self.assertEqual(code, 0)
            self.assertTrue(target.exists())
            self.assertIn("1920×1080", out)

    def test_capture_error_is_reported_not_raised(self):
        code, _, err, _ = self.run_cli("--lang", "ro", "capture", "monitor", "--index", "7")
        self.assertEqual(code, 1)
        self.assertIn("Captura a eșuat", err)

    def test_hotkeys_listing(self):
        code, out, _, _ = self.run_cli("hotkeys")
        self.assertIn("Ctrl+Shift+1  →  Capture region", out)


if __name__ == "__main__":
    unittest.main()
