import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from screenpilot.capture import CapturedImage, CaptureResult, Rect
from screenpilot.core.errors import ConfigurationError
from screenpilot.core.policy import Policy, load_policy
from screenpilot.editor.watermark import render_text, watermark_image
from screenpilot.settings import Settings


class PolicyTests(unittest.TestCase):
    def test_load_first_valid_policy_and_lock_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp, "bad.json")
            bad.write_text("{oops", encoding="utf-8")
            good = Path(tmp, "policy.json")
            good.write_text(
                json.dumps({"locked": {"capture.output_dir": "X:/share"}, "disabled_features": ["run_script", "nope"], "message": "IT"}),
                encoding="utf-8",
            )
            with self.assertLogs("screenpilot.core.policy", level="WARNING"):
                policy = load_policy([Path(tmp, "missing.json"), bad, good])
        self.assertTrue(policy.active)
        self.assertFalse(policy.allows("run_script"))
        self.assertTrue(policy.allows("ocr"))
        settings = Settings(None)
        settings.locked = dict(policy.locked)
        self.assertEqual(settings.get("capture.output_dir"), "X:/share")
        with self.assertRaises(ConfigurationError):
            settings.set("capture.output_dir", "C:/mine")
        self.assertFalse(load_policy([]).active)

    def test_app_drops_run_script_when_disabled(self):
        from screenpilot.app import create_app
        from screenpilot.core.logging_setup import close_logging
        from screenpilot.core.paths import AppPaths

        from fixtures import make_backend
        from test_workflow import FakeClipboard, FakeShell

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                paths=AppPaths.under(Path(tmp), portable=True),
                backend=make_backend(),
                clipboard=FakeClipboard(),
                shell=FakeShell(),
                policy=Policy(locked={"capture.image_format": "jpg"}, disabled=frozenset({"run_script"})),
            )
            result = app.capture.capture_region(Rect(0, 0, 20, 20))
            steps, ctx = app.run_after_capture(result, [{"id": "save"}, {"id": "run_script", "command": ["x"]}])
            self.assertEqual([s.action_id for s in steps], ["save"])
            self.assertEqual(ctx.saved_path.suffix, ".jpg")
            app.shutdown()
            close_logging()


class WatermarkTests(unittest.TestCase):
    def test_render_text_tokens(self):
        from datetime import datetime

        text = render_text("{date} {time} ok", datetime(2026, 10, 19, 8, 5))
        self.assertEqual(text, "2026-10-19 08:05 ok")

    def test_positions_change_pixels_only_where_expected(self):
        image = CapturedImage.from_pil(Image.new("RGB", (400, 200), "white"))
        br = watermark_image(image, "Confidential", position="bottom-right")
        self.assertNotEqual(br.to_pil().crop((250, 150, 400, 200)).getextrema(), ((255, 255), (255, 255), (255, 255)))
        self.assertEqual(br.pixel(10, 10), (255, 255, 255))
        tiled = watermark_image(image, "Confidential", position="tiled", opacity=80)
        self.assertNotEqual(tiled.to_pil().getextrema(), ((255, 255), (255, 255), (255, 255)))

    def test_workflow_applies_watermark_when_enabled(self):
        from screenpilot.app import create_app
        from screenpilot.core.logging_setup import close_logging
        from screenpilot.core.paths import AppPaths

        from fixtures import make_backend
        from test_workflow import FakeClipboard, FakeShell

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(paths=AppPaths.under(Path(tmp), portable=True), backend=make_backend(), clipboard=FakeClipboard(), shell=FakeShell())
            app.settings.set("watermark.enabled", True)
            app.settings.set("watermark.text", "TEST")
            result = CaptureResult(CapturedImage.from_pil(Image.new("RGB", (300, 120), "white")), "region", Rect(0, 0, 300, 120))
            steps, ctx = app.run_after_capture(result, [{"id": "copy_image"}])
            self.assertTrue(ctx.result.metadata["watermarked"])
            self.assertNotEqual(ctx.result.image.to_pil().getextrema(), ((255, 255), (255, 255), (255, 255)))
            app.shutdown()
            close_logging()


if __name__ == "__main__":
    unittest.main()
