import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from screenpilot.core.diagnostics import Diagnostics, export_bundle


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self._tmp.name) / "logs"
        self.logs.mkdir()
        self.diag = Diagnostics(self.logs)

    def tearDown(self):
        self.diag.close()
        self._tmp.cleanup()

    def test_timings_summary_and_journal(self):
        with self.assertLogs("screenpilot.core.diagnostics", level="WARNING"):
            for ms in (10, 20, 30, 40, 1000):
                self.diag.record("capture.grab", ms, mode="region")
        with self.diag.timed("workflow.run", steps=2) as info:
            info["extra"] = 1
        summary = self.diag.summary()
        grab = summary["capture.grab"]
        self.assertEqual((grab["count"], grab["min"], grab["median"], grab["max"], grab["last"]), (5, 10, 30, 1000, 1000))
        self.assertEqual(grab["p95"], 1000)
        self.assertIn("workflow.run", summary)
        lines = (self.logs / "perf.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(json.loads(lines[-1])["extra"], 1)

    def test_slow_operation_warns_and_errors_are_tagged(self):
        with self.assertLogs("screenpilot.core.diagnostics", level="WARNING"):
            self.diag.record("capture.freeze", 900)
        with self.assertRaises(ValueError):
            with self.diag.timed("action.save"):
                raise ValueError("disk full")
        self.assertEqual(self.diag.timings()[-1].extra["error"], "ValueError")

    def test_thread_exception_goes_to_crash_log(self):
        import sys

        old_sys, old_thread = sys.excepthook, threading.excepthook
        try:
            self.diag.install_crash_handlers()

            def boom():
                raise RuntimeError("worker exploded")

            with self.assertLogs("screenpilot.core.diagnostics", level="CRITICAL"):
                t = threading.Thread(target=boom, name="Worker-X")
                t.start()
                t.join()
        finally:
            sys.excepthook, threading.excepthook = old_sys, old_thread
            self.diag.close()
        crash = (self.logs / "crash.log").read_text(encoding="utf-8")
        self.assertIn("Worker-X", crash)
        self.assertIn("worker exploded", crash)

    def test_export_bundle_contents(self):
        (self.logs / "screenpilot.log").write_text("hello C:\\Users\\someone\\x", encoding="utf-8")
        (self.logs / "screenpilot.log.1").write_text("older", encoding="utf-8")
        self.diag.record("capture.grab", 12)
        target = export_bundle(
            Path(self._tmp.name) / "out" / "diag.zip",
            log_dir=self.logs,
            settings={"general": {"language": "ro"}},
            diagnostics=self.diag,
            extra={"monitors": ["m1"]},
        )
        with zipfile.ZipFile(target) as zf:
            names = set(zf.namelist())
            self.assertTrue({"logs/screenpilot.log", "logs/screenpilot.log.1", "logs/perf.jsonl", "system.json", "settings.json", "README.txt"} <= names)
            system = json.loads(zf.read("system.json"))
            self.assertIn("capture.grab", system["timings"])
            self.assertEqual(system["monitors"], ["m1"])


class AppDiagnosticsTests(unittest.TestCase):
    def test_app_records_capture_and_exports(self):
        from screenpilot.app import create_app
        from screenpilot.capture import Rect
        from screenpilot.core.logging_setup import close_logging
        from screenpilot.core.paths import AppPaths

        from fixtures import make_backend
        from test_workflow import FakeClipboard, FakeShell

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                paths=AppPaths.under(Path(tmp), portable=True), backend=make_backend(), clipboard=FakeClipboard(), shell=FakeShell()
            )
            app.capture.freeze()
            result = app.capture.capture_region(Rect(0, 0, 10, 10))
            app.run_after_capture(result)
            names = set(app.diagnostics.summary())
            self.assertTrue({"capture.freeze", "capture.grab", "workflow.run", "action.save"} <= names)
            app.settings.set("diagnostics.verbose_logging", True)
            import logging

            self.assertEqual(logging.getLogger("screenpilot").level, logging.DEBUG)
            target = app.export_diagnostics(Path(tmp) / "d.zip")
            with zipfile.ZipFile(target) as zf:
                self.assertIn("hotkeys", json.loads(zf.read("system.json")))
            app.shutdown()
            close_logging()


if __name__ == "__main__":
    unittest.main()
