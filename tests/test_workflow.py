import struct
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from screenpilot.capture import CaptureService, Rect
from screenpilot.core.events import EventBus
from screenpilot.platform.dib import dib_from_image
from screenpilot.workflow import (
    ActionRegistry,
    StepStatus,
    Workflow,
    WorkflowContext,
    builtin_actions,
    render_filename,
    sanitize_filename,
)
from screenpilot.workflow.actions import EVENT_OPEN_EDITOR, RunScriptAction

from fixtures import make_backend


class FakeClipboard:
    def __init__(self):
        self.text = None
        self.image = None

    def set_text(self, text):
        self.text = text

    def set_image(self, image):
        self.image = image


class FakeShell:
    def __init__(self):
        self.revealed = []

    def reveal(self, path):
        self.revealed.append(path)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.bus = EventBus()
        service = CaptureService(make_backend(), self.bus)
        self.result = service.capture_window()
        self.clipboard = FakeClipboard()
        self.shell = FakeShell()

    def tearDown(self):
        self._tmp.cleanup()

    def ctx(self, **kw):
        return WorkflowContext(self.result, self.out, self.clipboard, self.shell, self.bus, **kw)

    def test_default_chain_save_copy_path_reveal(self):
        ctx = self.ctx()
        steps = [{"id": "save"}, {"id": "copy_image"}, {"id": "copy_path"}, {"id": "open_folder"}]
        results = Workflow(steps).run(ctx, ActionRegistry())
        self.assertEqual([r.status for r in results], [StepStatus.OK] * 4)
        self.assertTrue(ctx.saved_path.exists())
        self.assertEqual(self.clipboard.text, str(ctx.saved_path))
        self.assertIs(self.clipboard.image, self.result.image)
        self.assertEqual(self.shell.revealed, [ctx.saved_path])

    def test_failure_does_not_stop_chain_unless_configured(self):
        steps = [{"id": "copy_path"}, {"id": "nope"}, {"id": "save"}]
        with self.assertLogs("screenpilot.workflow", level="ERROR"):
            results = Workflow(steps).run(self.ctx(), ActionRegistry())
        self.assertEqual([r.status for r in results], [StepStatus.FAILED, StepStatus.FAILED, StepStatus.OK])
        with self.assertLogs("screenpilot.workflow", level="ERROR"):
            results = Workflow(steps, stop_on_error=True).run(self.ctx(), ActionRegistry())
        self.assertEqual(len(results), 1)

    def test_save_as_skipped_without_ui_and_uses_chosen_path(self):
        results = Workflow([{"id": "save_as"}]).run(self.ctx(), ActionRegistry())
        self.assertEqual(results[0].status, StepStatus.SKIPPED)
        target = self.out / "custom" / "doc.jpg"
        ctx = self.ctx(ask_save_path=lambda suggested: target)
        Workflow([{"id": "save_as"}]).run(ctx, ActionRegistry())
        self.assertEqual(ctx.saved_path, target)
        self.assertEqual(target.read_bytes()[:2], b"\xff\xd8")  # JPEG magic

    def test_save_never_overwrites(self):
        ctx1, ctx2 = self.ctx(filename_template="same"), self.ctx(filename_template="same")
        Workflow([{"id": "save"}]).run(ctx1, ActionRegistry())
        Workflow([{"id": "save"}]).run(ctx2, ActionRegistry())
        self.assertEqual(ctx2.saved_path.name, "same_1.png")

    def test_open_editor_publishes_event(self):
        seen = []
        self.bus.subscribe(EVENT_OPEN_EDITOR, lambda e: seen.append(e.payload["result"]))
        Workflow([{"id": "open_editor"}]).run(self.ctx(), ActionRegistry())
        self.assertEqual(seen, [self.result])

    def test_run_script_without_shell_and_placeholders(self):
        calls = []

        def runner(args, **kw):
            calls.append((args, kw))
            return subprocess.CompletedProcess(args, 0, "", "")

        registry = ActionRegistry([*builtin_actions(), RunScriptAction(runner)])
        ctx = self.ctx()
        steps = [{"id": "save"}, {"id": "run_script", "command": ["tool.exe", "--in", "{path}", "{mode}"]}]
        results = Workflow(steps).run(ctx, registry)
        self.assertEqual(results[1].status, StepStatus.OK)
        args, kw = calls[0]
        self.assertEqual(args, ["tool.exe", "--in", str(ctx.saved_path), "window"])
        self.assertNotIn("shell", kw)

    def test_run_script_validation_and_exit_code(self):
        registry = ActionRegistry(
            [RunScriptAction(lambda args, **kw: subprocess.CompletedProcess(args, 2, "", "boom"))]
        )
        with self.assertLogs("screenpilot.workflow", level="ERROR"):
            results = Workflow([{"id": "run_script", "command": "rm -rf /"}, {"id": "run_script", "command": ["x"]}]).run(
                self.ctx(), registry
            )
        self.assertEqual([r.status for r in results], [StepStatus.FAILED, StepStatus.FAILED])
        self.assertIn("code 2", results[1].message)


class FilenameTests(unittest.TestCase):
    def test_render_and_sanitize(self):
        result = CaptureService(make_backend()).capture_window()
        name = render_filename("{date}_{time}_{mode}_{window}_{unknown}", result, datetime(2026, 10, 16, 9, 5, 7))
        self.assertEqual(name, "2026-10-16_09-05-07_window_Report.docx - Word_{unknown}")
        self.assertEqual(sanitize_filename('a<b>:c/d\\e|f?g*h"'), "a_b__c_d_e_f_g_h_")
        self.assertEqual(sanitize_filename("CON"), "_CON")
        self.assertEqual(sanitize_filename("..."), "capture")


class DibTests(unittest.TestCase):
    def test_dib_layout_is_bottom_up_24bit_padded(self):
        image = CaptureService(make_backend()).capture_region(Rect(0, 0, 3, 2)).image
        dib = dib_from_image(image)
        size, width, height, planes, bpp = struct.unpack_from("<IiiHH", dib)
        self.assertEqual((size, width, height, planes, bpp), (40, 3, 2, 1, 24))
        row_len = 12  # 3 px * 3 bytes = 9, padded to 12
        self.assertEqual(len(dib), 40 + 2 * row_len)
        first_row = dib[40 : 40 + 3]  # bottom image row (y=1), pixel x=0, as B,G,R
        r, g, b = image.pixel(0, 1)
        self.assertEqual(tuple(first_row), (b, g, r))


if __name__ == "__main__":
    unittest.main()
