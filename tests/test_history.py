import tempfile
import unittest
from pathlib import Path

from screenpilot.capture import CaptureService, FakeBackend, Monitor, Rect
from screenpilot.core.errors import CaptureError
from screenpilot.core.events import EventBus
from screenpilot.history import EVENT_HISTORY_CHANGED, CaptureHistory
from screenpilot.workflow import ActionRegistry, Workflow, WorkflowContext

from fixtures import make_backend
from test_workflow import FakeClipboard, FakeShell


class FrozenCaptureTests(unittest.TestCase):
    def test_region_from_frozen_crops_desktop_coordinates(self):
        bus = EventBus()
        completed = []
        bus.subscribe("capture.completed", lambda e: completed.append(e.payload["result"]))
        # A small fake desktop so the frozen image carries the coordinate pattern.
        small = FakeBackend([Monitor(1, Rect(-100, -50, 300, 200), Rect(-100, -50, 300, 200), True)])
        service = CaptureService(small, bus)
        frozen = service.freeze()
        self.assertEqual(completed, [])  # freezing is not a capture
        result = service.region_from_frozen(frozen, Rect(-10, -5, 20, 10))
        self.assertEqual(result.image.pixel(0, 0), FakeBackend.color_at(-10, -5))
        self.assertEqual(result.image.pixel(19, 9), FakeBackend.color_at(9, 4))
        self.assertTrue(result.metadata["frozen"])
        self.assertEqual(completed, [result])
        self.assertEqual(service.repeat_last().region, Rect(-10, -5, 20, 10))
        self.assertEqual(len(small.grabs), 2)  # freeze + live repeat

    def test_selection_outside_frozen_desktop(self):
        service = CaptureService(make_backend())

        frozen = service.freeze()
        with self.assertRaises(CaptureError):
            service.region_from_frozen(frozen, Rect(5000, 5000, 10, 10))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bus = EventBus()
        self.service = CaptureService(make_backend(), self.bus)

    def tearDown(self):
        self._tmp.cleanup()

    def run_workflow(self):
        result = self.service.capture_window()
        ctx = WorkflowContext(result, self.root / "captures", FakeClipboard(), FakeShell(), self.bus)
        Workflow([{"id": "save"}]).run(ctx, ActionRegistry())
        return ctx.saved_path

    def test_records_saved_captures_with_thumbnail_and_persists(self):
        history = CaptureHistory(self.root / "data")
        history.attach(self.bus)
        changes = []
        self.bus.subscribe(EVENT_HISTORY_CHANGED, lambda e: changes.append(1))
        path = self.run_workflow()
        entries = history.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, str(path))
        self.assertEqual(entries[0].title, "Report.docx - Word")
        self.assertTrue(Path(entries[0].thumbnail).exists())
        self.assertEqual(changes, [1])
        reloaded = CaptureHistory(self.root / "data").load()
        self.assertEqual(reloaded.entries(), entries)

    def test_missing_files_pruned_and_limit_enforced(self):
        history = CaptureHistory(self.root / "data", limit=2)
        history.attach(self.bus)
        paths = [self.run_workflow() for _ in range(3)]
        self.assertEqual([e.path for e in history.entries()], [str(paths[2]), str(paths[1])])
        self.assertEqual(len(list((self.root / "data" / "thumbnails").glob("*.png"))), 2)
        paths[2].unlink()
        self.assertEqual([e.path for e in CaptureHistory(self.root / "data").load().entries()], [str(paths[1])])

    def test_remove_clear_search_and_corrupt_file(self):
        history = CaptureHistory(self.root / "data")
        history.attach(self.bus)
        self.run_workflow()
        self.assertEqual(len(history.search("word")), 1)
        self.assertEqual(history.search("nothing"), [])
        history.remove(history.entries()[0].id)
        self.assertEqual(history.entries(), [])
        history.file.write_text("{broken", encoding="utf-8")
        with self.assertLogs("screenpilot.history", level="ERROR"):
            self.assertEqual(CaptureHistory(self.root / "data").load().entries(), [])


if __name__ == "__main__":
    unittest.main()
