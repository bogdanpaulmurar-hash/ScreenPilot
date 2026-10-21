"""Qt UI tests (offscreen). Skipped when PySide6 is not installed.

Set SCREENPILOT_UI_SHOTS=<dir> to save screenshots of every window (CI uploads
them so the UI can be reviewed without a Windows machine).
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QGuiApplication, QKeySequence, QPainter, QPixmap, QColor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover - headless CI without the ui extra
    HAVE_QT = False

from screenpilot.capture import Monitor, Point, Rect
from screenpilot.core.logging_setup import close_logging
from screenpilot.core.paths import AppPaths

from test_workflow import FakeClipboard, FakeShell

SHOTS = os.environ.get("SCREENPILOT_UI_SHOTS")


def shot(widget, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        QApplication.processEvents()
        widget.grab().save(str(Path(SHOTS) / f"{name}.png"))


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class UiTestCase(unittest.TestCase):
    theme_mode = "light"

    @classmethod
    def setUpClass(cls):
        from screenpilot.ui.theme import ThemeManager
        from screenpilot.ui.widgets import set_theme_manager

        cls.qt = QApplication.instance() or QApplication([])
        cls.themes = ThemeManager(cls.theme_mode)
        set_theme_manager(cls.themes)
        cls.themes.apply()

    def setUp(self):
        from screenpilot.app import create_app
        from screenpilot.samples import DemoBackend
        from screenpilot.ui.controller import AppController

        self._tmp = tempfile.TemporaryDirectory()
        screen = QGuiApplication.primaryScreen()
        geo = screen.geometry()
        self.screen_rect = Rect(geo.x(), geo.y(), geo.width(), geo.height())
        self.backend = DemoBackend(self.screen_rect, device_name=screen.name(), cursor=Point(geo.x() + 10, geo.y() + 10))
        self.window = self.backend.list_windows()[0]  # the front ERP window
        self.app = create_app(
            paths=AppPaths.under(Path(self._tmp.name), portable=True),
            backend=self.backend,
            clipboard=FakeClipboard(),
            shell=FakeShell(),
        )
        self.app.settings.set("capture.show_preview", False)  # tests opt in explicitly
        self.controller = AppController(self.app, register_hotkeys=False)
        self.notes = []
        self.controller.notify.connect(lambda title, body, err: self.notes.append((title, body, err)))
        self.controller.notify_action.connect(lambda title, body, err, _action: self.notes.append((title, body, err)))
        self.captured = []
        self.controller.captured.connect(lambda result, path, err: self.captured.append((result, path, err)))

    def tearDown(self):
        self.controller.shutdown()
        QApplication.processEvents()
        close_logging()
        self._tmp.cleanup()

    def select(self, overlay, a: QPoint, b: QPoint) -> None:
        QTest.mouseMove(overlay, a)
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
        QTest.mouseMove(overlay, b)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)

    def open_selector(self):
        self.controller.capture_region()
        overlay = self.controller.selector.overlays[0]
        QTest.qWaitForWindowExposed(overlay)
        return overlay


class RegionSelectorTests(UiTestCase):
    def test_match_screens_by_name_then_position(self):
        from PySide6.QtCore import QRect

        from screenpilot.ui.region_selector import match_screens

        class FakeScreen:
            def __init__(self, name, x):
                self._name, self._x = name, x

            def name(self):
                return self._name

            def geometry(self):
                return QRect(self._x, 0, 100, 100)

        a = Monitor(1, Rect(0, 0, 10, 10), Rect(0, 0, 10, 10), True, r"\\.\DISPLAY1")
        b = Monitor(2, Rect(-10, 0, 10, 10), Rect(-10, 0, 10, 10), False, "unknown")
        s1, s2 = FakeScreen(r"\\.\DISPLAY1", 0), FakeScreen("other", -100)
        pairs = match_screens([a, b], [s2, s1])
        self.assertEqual([(m.index, s.name()) for m, s in pairs], [(1, r"\\.\DISPLAY1"), (2, "other")])

    def test_drag_select_shows_action_bar_and_enter_runs_workflow(self):
        overlay = self.open_selector()
        QTest.mouseMove(overlay, QPoint(120, 110))
        shot(overlay, "01_selector_magnifier")
        self.assertFalse(overlay.action_bar.isVisible())
        self.select(overlay, QPoint(50, 40), QPoint(249, 139))
        self.assertTrue(overlay.action_bar.isVisible())
        shot(overlay, "02_selector_action_bar")
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.captured))
        result, path, error = self.captured[0]
        x0, y0 = self.screen_rect.x, self.screen_rect.y
        self.assertEqual(result.region, Rect(x0 + 50, y0 + 40, 200, 100))
        self.assertEqual(error, "")
        self.assertIsNotNone(path)
        self.assertEqual(self.notes[0][0], "Capture saved")
        self.assertFalse(self.controller.selector.active)

    def test_copy_only_from_action_bar_does_not_save(self):
        overlay = self.open_selector()
        self.select(overlay, QPoint(60, 60), QPoint(200, 160))
        QTest.mouseClick(overlay.action_bar.buttons["copy"], Qt.MouseButton.LeftButton)
        self.assertTrue(wait_until(lambda: self.captured))
        result, path, _ = self.captured[0]
        self.assertIsNone(path)
        self.assertIs(self.app.clipboard.image, result.image)
        self.assertEqual(self.app.history.entries(), [])

    def test_click_on_window_snaps(self):
        from unittest import mock

        # Frame detection has a 25 ms budget per mouse move; a slow CI runner can stop it
        # early and propose another frame. Give the test all the time it needs.
        patcher = mock.patch("screenpilot.capture.detect.SEARCH_BUDGET_S", 5.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        overlay = self.open_selector()
        b = self.window.bounds
        inside = QPoint(b.x - self.screen_rect.x + b.width // 2, b.y - self.screen_rect.y + 60)
        QTest.mouseMove(overlay, inside)
        shot(overlay, "03_selector_window_snap")
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, inside)
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.captured))
        self.assertEqual(self.captured[0][0].region, self.window.bounds)

    def test_auto_detected_frames_and_wheel_to_parent(self):
        overlay = self.open_selector()
        erp = self.window.bounds
        field_point = QPoint(erp.x - self.screen_rect.x + 300, erp.y - self.screen_rect.y + 70)
        target = Point(erp.x + 300, erp.y + 70)

        def field_found():
            QTest.mouseMove(overlay, field_point + QPoint(1, 0))
            QTest.mouseMove(overlay, field_point)
            rect = overlay.model.hover_rect()
            return rect is not None and rect.height == 27

        self.assertTrue(wait_until(field_found, timeout=5))
        shot(overlay, "03b_selector_auto_frame_field")
        overlay.model.cycle(+1)
        parent = overlay.model.hover_rect()
        self.assertTrue(parent.contains(target) and parent.height > 27)
        overlay.model.cycle(+10)
        self.assertEqual(overlay.model.hover_rect(), erp)
        overlay.model.cycle(-10)
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, field_point)
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.captured))
        self.assertEqual(self.captured[0][0].region.height, 27)

    def test_pin_with_p_key(self):
        overlay = self.open_selector()
        self.select(overlay, QPoint(300, 150), QPoint(520, 300))
        QTest.keyClick(overlay, Qt.Key.Key_P)
        self.assertTrue(wait_until(lambda: self.controller.pins))
        pin = self.controller.pins[0]
        QTest.qWaitForWindowExposed(pin)
        shot(pin, "04_pinned_capture")
        pin.close()

    def test_escape_cancels(self):
        overlay = self.open_selector()
        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        QApplication.processEvents()
        self.assertFalse(self.controller.selector.active)
        self.assertEqual(self.captured, [])

    def test_romanian_hint_renders(self):
        self.app.settings.set("general.language", "ro")
        overlay = self.open_selector()
        QTest.mouseMove(overlay, QPoint(400, 300))
        shot(overlay, "05_selector_ro")
        self.select(overlay, QPoint(100, 100), QPoint(300, 220))
        self.assertIn("Capturează", overlay.action_bar.buttons["default"].toolTip())
        QTest.keyClick(overlay, Qt.Key.Key_Escape)


class ScrollingUiTests(UiTestCase):
    def _fake_window(self):
        from test_scrolling import FakeScrollingWindow

        win = FakeScrollingWindow()
        self.backend.grab = win.grab  # the live screen now shows the scrolling page
        self.controller.scroller_factory = lambda: win
        return win

    def test_selector_s_key_runs_scrolling_capture(self):
        from test_scrolling import HEADER, PAGE_H

        overlay = self.open_selector()
        self.select(overlay, QPoint(0, 0), QPoint(419, 299))
        win = self._fake_window()
        QTest.keyClick(overlay, Qt.Key.Key_S)
        self.assertTrue(wait_until(lambda: self.captured, timeout=20))
        result, path, error = self.captured[0]
        self.assertEqual(result.mode, "scrolling")
        self.assertEqual(result.image.height, HEADER + PAGE_H)
        self.assertGreater(win.scrolls, 3)
        self.assertIsNotNone(path)
        self.assertIsNone(self.controller._scroll_indicator)

    def test_hub_scrolling_tile_and_preview_capture_more(self):
        self.app.settings.set("capture.show_preview", True)
        self.controller.trigger("capture.window")
        self.assertTrue(wait_until(lambda: self.controller._preview is not None))
        preview = self.controller._preview
        self.assertTrue(preview.more_button.isVisible())
        win = self._fake_window()
        self.controller.start_scrolling(Rect(0, 0, 420, 300), settle=lambda: None)
        indicator = self.controller._scroll_indicator
        self.assertIsNotNone(indicator)
        shot(indicator, "20_scroll_indicator")
        self.assertTrue(wait_until(lambda: len(self.captured) >= 2, timeout=20))
        self.assertEqual(self.captured[-1][0].mode, "scrolling")
        self.assertTrue(wait_until(lambda: self.controller._preview is not None))
        self.assertFalse(hasattr(self.controller._preview, "more_button"))
        shot(self.controller._preview, "21_preview_after_scrolling")
        del win


class CaptureModeTests(UiTestCase):
    def test_mode_hotkeys_open_coloured_selector_with_their_enter_action(self):
        from screenpilot.capture.profiles import set_mode_color

        set_mode_color(self.app.settings, "capture.copy", "#123456")
        self.controller.trigger("capture.copy")
        self.assertTrue(wait_until(lambda: self.controller.selector.active))
        overlay = self.controller.selector.overlays[0]
        self.assertEqual(overlay._accent.name(), "#123456")
        self.assertEqual(overlay.style.mode_label, "Copy only")
        QTest.mouseMove(overlay, QPoint(300, 300))
        self.select(overlay, QPoint(60, 60), QPoint(200, 160))
        shot(overlay, "23_mode_copy_selector")
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.captured))
        self.assertIsNone(self.captured[0][1])  # copy only: nothing saved

    def test_edit_mode_magenta_and_opens_editor(self):
        self.app.settings.set("selector.thickness", 4)
        self.controller.trigger("capture.edit")
        self.assertTrue(wait_until(lambda: self.controller.selector.active))
        overlay = self.controller.selector.overlays[0]
        self.assertEqual(overlay._accent.name(), "#e3008c")
        self.assertEqual(overlay.style.thickness, 4)
        QTest.mouseMove(overlay, QPoint(400, 300))
        shot(overlay, "24_mode_edit_selector")
        self.select(overlay, QPoint(60, 60), QPoint(300, 220))
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: any(k.startswith("editor-") for k in self.controller._windows)))

    def test_modes_settings_page(self):
        dialog = self.controller.show_settings()
        QTest.qWaitForWindowExposed(dialog)
        dialog.tabs.setCurrentIndex(2)
        QApplication.processEvents()
        keys, swatch = dialog.mode_rows["capture.edit"]
        self.assertEqual(keys.text(), "Ctrl+Shift+E")
        self.assertEqual(swatch.text(), "#E3008C")
        self.assertEqual(dialog.mode_rows["capture.region"][1].text(), "Accent")
        shot(dialog, "25_settings_modes")
        dialog._reset_mode_colors()
        dialog.close()


class EditorTests(UiTestCase):
    def _open(self):
        result = self.app.capture.capture_window()
        editor = self.controller.open_editor(result)
        QTest.qWaitForWindowExposed(editor)
        editor.resize(1000, 700)
        QApplication.processEvents()
        return editor

    def _drag(self, canvas, a, b, modifier=None):
        modifier = modifier if modifier is not None else Qt.KeyboardModifier.NoModifier
        t, s = canvas.target(), canvas.scale
        pa = QPoint(round(t.left() + a[0] * s), round(t.top() + a[1] * s))
        pb = QPoint(round(t.left() + b[0] * s), round(t.top() + b[1] * s))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, modifier, pa)
        QTest.mouseMove(canvas, pb)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, modifier, pb)

    def test_draw_tools_undo_select_move_and_done(self):
        editor = self._open()
        canvas = editor.canvas
        editor.set_tool("arrow")
        self._drag(canvas, (40, 300), (160, 90))
        editor.set_tool("rect")
        editor.set_color("#13a10e")
        self._drag(canvas, (165, 50), (620, 90))
        editor.set_tool("blur")
        self._drag(canvas, (170, 210), (630, 240))
        editor.set_tool("highlight")
        self._drag(canvas, (10, 190), (150, 220))
        editor.set_tool("step")
        t, s = canvas.target(), canvas.scale
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(round(t.left() + 300 * s), round(t.top() + 108 * s)))
        kinds = [sh.kind for sh in editor.doc.shapes]
        self.assertEqual(kinds[:4], ["arrow", "rect", "blur", "highlight"])
        self.assertIn("step", kinds)
        shot(editor, "26_editor")
        editor.undo()
        self.assertNotIn("step", [sh.kind for sh in editor.doc.shapes])
        editor.redo()
        # select the rectangle edge and move it
        editor.set_tool("select")
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(round(t.left() + 165 * s), round(t.top() + 70 * s)))
        self.assertEqual(editor.doc.shapes[canvas.selected].kind, "rect")
        before = editor.doc.shapes[canvas.selected].p1
        self._drag(canvas, (165, 70), (185, 80))
        self.assertEqual(editor.doc.shapes[canvas.selected].p1, Point(before.x + 20, before.y + 10))
        editor.set_color("#0067c0")
        self.assertEqual(editor.doc.shapes[canvas.selected].color, "#0067c0")
        canvas.delete_selected()
        editor.set_tool("crop")
        self._drag(canvas, (0, 0), (400, 300))
        self.assertIsNotNone(editor.doc.crop)
        editor.done()
        self.assertTrue(wait_until(lambda: self.captured))
        result = self.captured[0][0]
        self.assertTrue(result.metadata["edited"])
        self.assertEqual(result.image.width, editor.doc.crop.width)

    def test_dark_editor_screenshot_and_shortcuts(self):
        editor = self._open()
        QTest.keyClick(editor, Qt.Key.Key_R)
        self.assertEqual(editor.canvas.tool, "rect")
        QTest.keyClick(editor, Qt.Key.Key_B)
        self.assertEqual(editor.canvas.tool, "blur")
        editor.close()


class FakeOcrEngine:
    name = "fake"

    def __init__(self, backend):
        self.backend = backend

    def available(self):
        return True

    def languages(self):
        return ["en-US"]

    def recognize(self, image, language=None):
        from screenpilot.ocr import OcrLine, OcrResult, OcrWord

        words = [
            OcrLine((OcrWord("Customer", Rect(24, 60, 80, 18), 97.0), OcrWord("Contoso", Rect(178, 60, 70, 18), 95.0))),
            OcrLine((OcrWord("contact:", Rect(24, 100, 70, 18), 90.0), OcrWord("ana.pop@contoso.ro", Rect(178, 100, 160, 18), 88.0))),
        ]
        return OcrResult(tuple(words), language or "en-US", self.name)


class OcrUiTests(UiTestCase):
    def test_ocr_mode_copies_text_and_shows_window(self):
        self.controller.ocr_engine_factory = lambda: FakeOcrEngine(self.backend)
        self.controller.trigger("ocr.region")
        self.assertTrue(wait_until(lambda: self.controller.selector.active))
        overlay = self.controller.selector.overlays[0]
        erp = self.window.bounds
        self.select(overlay, QPoint(erp.x - self.screen_rect.x, erp.y - self.screen_rect.y),
                    QPoint(erp.right - self.screen_rect.x - 1, erp.bottom - self.screen_rect.y - 1))
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.app.clipboard.text is not None))
        self.assertEqual(self.app.clipboard.text, "Customer Contoso\ncontact: ana.pop@contoso.ro")
        window = next(w for k, w in self.controller._windows.items() if k.startswith("ocr-"))
        QTest.qWaitForWindowExposed(window)
        window.search.setText("contoso")
        self.assertEqual(window.matches.text(), "2 matches")
        shot(window, "27_ocr_window")
        window.copy_markdown()
        self.assertIn("Customer", self.app.clipboard.text)

    def test_editor_auto_blur(self):
        self.controller.ocr_engine_factory = lambda: FakeOcrEngine(self.backend)
        editor = self.controller.open_editor(self.app.capture.capture_window())
        QTest.qWaitForWindowExposed(editor)
        count = editor.auto_blur()
        self.assertEqual(count, 1)
        self.assertEqual(editor.doc.shapes[-1].kind, "blur")
        self.assertIn("e-mail", editor.hint.text())
        editor.close()


class ToolsTests(UiTestCase):
    def test_colour_picker_copies_hex(self):
        self.controller.trigger("tools.color")
        self.assertTrue(wait_until(lambda: self.controller.selector.active))
        overlay = self.controller.selector.overlays[0]
        erp = self.window.bounds
        approve = QPoint(erp.right - self.screen_rect.x - 30, erp.bottom - self.screen_rect.y - 21)
        QTest.mouseMove(overlay, approve)
        shot(overlay, "28_colour_picker")
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, approve)
        self.assertTrue(wait_until(lambda: self.app.clipboard.text is not None))
        self.assertEqual(self.app.clipboard.text, "#0078D4")  # the demo's accent button
        self.assertFalse(self.controller.selector.active)

    def test_ruler_copies_measurement_without_capturing(self):
        self.controller.trigger("tools.measure")
        self.assertTrue(wait_until(lambda: self.controller.selector.active))
        overlay = self.controller.selector.overlays[0]
        self.select(overlay, QPoint(100, 100), QPoint(399, 199))
        self.assertFalse(overlay.action_bar.isVisible())
        shot(overlay, "29_ruler")
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.app.clipboard.text is not None))
        self.assertEqual(self.app.clipboard.text, "300 × 100 px")
        self.assertEqual(self.captured, [])
        self.assertEqual(self.app.history.entries(), [])

    def test_interval_capture_runs_and_stops(self):
        self.controller.start_interval(Rect(self.screen_rect.x, self.screen_rect.y, 100, 80), seconds=0.05, count=3)
        self.assertTrue(wait_until(lambda: self.controller.interval is None, timeout=5))
        self.assertTrue(wait_until(lambda: len(self.app.history.entries()) == 3, timeout=5))
        self.assertIn("3", self.notes[-1][1])

    def test_tray_tools_and_recent_menu(self):
        from screenpilot.ui.tray import TrayIcon

        tray = TrayIcon(self.controller)
        self.controller.trigger("capture.window")
        self.assertTrue(wait_until(lambda: self.app.history.entries()))
        tray._fill_recent()
        actions = tray.recent_menu.actions()
        self.assertEqual(len(actions), 1)
        self.app.clipboard.image = None
        actions[0].trigger()
        self.assertIsNotNone(self.app.clipboard.image)
        self.assertIn("Ctrl+Shift+C", [a.text().split("\t")[-1] for a in tray._tool_actions])

    def test_policy_disables_feature(self):
        from screenpilot.core.policy import Policy

        self.app.policy = Policy(disabled=frozenset({"scrolling"}), message="IT")
        self.controller.trigger("capture.scrolling")
        self.assertFalse(self.controller.selector.active)
        self.assertIn("administrator", self.notes[-1][1])


class ProcedureTests(UiTestCase):
    def test_record_procedure_edit_and_export_all_formats(self):
        self.controller.trigger("procedure.toggle")
        self.assertIsNotNone(self.controller.procedure)
        bar = self.controller._session_bar
        QTest.qWaitForWindowExposed(bar)
        shot(bar, "30_procedure_bar")
        for _ in range(2):
            self.controller.trigger("capture.window")
            self.assertTrue(wait_until(lambda n=len(self.captured): len(self.captured) > n))
        self.assertTrue(wait_until(lambda: len(self.controller.procedure.steps) == 2))
        self.assertIn("2", bar.text.text())
        window = self.controller.finish_procedure()
        self.assertIsNone(self.controller.procedure)
        QTest.qWaitForWindowExposed(window)
        window.title.setText("Aprobare comandă")
        window.title.editingFinished.emit()
        window.list.setCurrentRow(0)
        window.step_title.setText("Deschide comanda")
        window.step_title.editingFinished.emit()
        window.step_desc.setPlainText("Caută comanda 10482.")
        window._move(1)
        self.assertEqual(window.session.steps[1].title, "Deschide comanda")
        window.resize(1100, 720)
        shot(window, "31_procedure_window")
        out = Path(self._tmp.name) / "export"
        out.mkdir()
        for fmt, ext in (("word", "docx"), ("pdf", "pdf"), ("html", "html"), ("md", "md")):
            target = window.export(fmt, out / f"proc.{ext}")
            self.assertTrue(target.exists() and target.stat().st_size > (100 if fmt == "md" else 1000), fmt)
        self.assertEqual((out / "proc.pdf").read_bytes()[:4], b"%PDF")
        window.close()

    def test_cancel_empty_procedure(self):
        self.controller.start_procedure()
        folder = self.controller.procedure.folder
        self.controller.cancel_procedure()
        self.assertIsNone(self.controller.procedure)
        self.assertFalse(folder.exists())


class StepEditorTests(UiTestCase):
    def test_number_steps_on_a_window_capture(self):
        overlay = self.open_selector()
        erp = self.window.bounds
        inside = QPoint(erp.x - self.screen_rect.x + 300, erp.y - self.screen_rect.y + 300)
        QTest.mouseMove(overlay, inside)
        self.assertTrue(wait_until(lambda: overlay.model.hover_rect() is not None))
        overlay.model.cycle(+10)  # whole window
        QTest.keyClick(overlay, Qt.Key.Key_N)
        self.assertTrue(wait_until(lambda: any(k.startswith("steps-") for k in self.controller._windows)))
        editor = next(w for k, w in self.controller._windows.items() if k.startswith("steps-"))
        QTest.qWaitForWindowExposed(editor)
        editor.resize(900, 640)
        QApplication.processEvents()
        canvas = editor.canvas

        def click_image(x, y, button=Qt.MouseButton.LeftButton):
            t = canvas._target()
            scale = t.width() / canvas.image.width
            pos = QPoint(round(t.left() + x * scale), round(t.top() + y * scale))
            QTest.mouseClick(canvas, button, Qt.KeyboardModifier.NoModifier, pos)

        # Customer field, Order date field (snapped to the detected element), then a free click.
        click_image(300, 70)
        click_image(300, 108)
        canvas.snap = False
        click_image(560, 625 - 176)
        self.assertEqual(len(editor.doc.steps), 3)
        self.assertIsNotNone(editor.doc.steps[0].frame)
        self.assertEqual(editor.doc.steps[0].frame.height, 27)
        self.assertIsNone(editor.doc.steps[2].frame)
        shot(editor, "22_step_editor")
        click_image(editor.doc.steps[1].point.x, editor.doc.steps[1].point.y, Qt.MouseButton.RightButton)
        self.assertEqual(len(editor.doc.steps), 2)
        editor.undo()
        self.assertEqual(len(editor.doc.steps), 3)
        editor.done()
        self.assertTrue(wait_until(lambda: self.captured))
        self.assertEqual(self.captured[0][0].metadata["steps"], 3)


class PreviewTests(UiTestCase):
    def test_quick_preview_after_capture(self):
        self.app.settings.set("capture.show_preview", True)
        self.controller.trigger("capture.window")
        self.assertTrue(wait_until(lambda: self.controller._preview is not None))
        preview = self.controller._preview
        QTest.qWaitForWindowExposed(preview)
        shot(preview, "06_quick_preview")
        self.assertTrue(preview.title.text().startswith("Saved"))
        self.assertTrue(preview.buttons["folder"].isEnabled())
        self.assertTrue(preview.buttons["edit"].isEnabled())
        self.assertEqual(self.notes, [])
        drag_file = preview._drag_file()
        self.assertTrue(drag_file.exists())
        preview.buttons["pin"].click()
        self.assertTrue(wait_until(lambda: self.controller.pins))

    def test_temp_file_for_copy_only_captures(self):
        result = self.app.capture.capture_window()
        path = self.controller.temp_file(result)
        self.assertTrue(path.exists())
        self.assertEqual(path.parent.name, "drag")


class ControllerTests(UiTestCase):
    def test_window_capture_runs_workflow_and_records_history(self):
        self.controller.trigger("capture.window")
        self.assertTrue(wait_until(lambda: self.notes))
        self.assertFalse(self.notes[0][2])
        self.assertEqual(len(self.app.history.entries()), 1)
        self.assertIsNotNone(self.app.clipboard.image)

    def test_workflow_failure_is_notified(self):
        self.app.settings.set("workflow.after_capture", [{"id": "copy_path"}])
        self.controller.trigger("capture.fullscreen")
        self.assertTrue(wait_until(lambda: self.notes))
        self.assertTrue(self.notes[0][2])

    def test_capture_error_is_notified_not_raised(self):
        self.backend.foreground = None
        self.controller.trigger("capture.window")
        self.assertEqual(self.notes[0][0], "Capture failed")

    def test_countdown_then_capture(self):
        calls = []
        self.controller.capture_delayed(1, then=lambda: calls.append(1))
        overlay = self.controller._countdown
        self.assertIsNotNone(overlay)
        shot(overlay, "07_countdown")
        self.assertTrue(wait_until(lambda: calls, timeout=4))

    def test_countdown_cancel(self):
        calls = []
        self.controller.capture_delayed(5, then=lambda: calls.append(1))
        self.controller._countdown.cancel()
        QTest.qWait(50)
        self.assertEqual(calls, [])
        self.assertIsNone(self.controller._countdown)

    def test_open_editor_without_any_capture_shows_the_picker(self):
        picker = self.controller.edit_last_capture()
        QTest.qWaitForWindowExposed(picker)
        shot(picker, "50_editor_picker_empty")
        self.assertFalse(picker.last_button.isEnabled())
        self.assertEqual(picker.recent_buttons, [])
        picker.close()
        self.assertFalse(self.notes)

    def test_editor_picker_last_recent_and_direct_mode(self):
        self.controller.trigger("capture.window")
        self.assertTrue(wait_until(lambda: self.app.history.entries()))
        self.controller.trigger("capture.fullscreen")
        self.assertTrue(wait_until(lambda: len(self.app.history.entries()) == 2 and len(self.captured) == 2))
        picker = self.controller.edit_last_capture()
        QTest.qWaitForWindowExposed(picker)
        shot(picker, "51_editor_picker")
        self.assertTrue(picker.last_button.isEnabled())
        self.assertEqual(len(picker.recent_buttons), 2)
        self.assertIs(picker.last, self.controller.last_result)
        editor = picker.choose(picker.last)
        self.assertIsNotNone(editor)
        self.assertEqual(editor.result.image.width, self.controller.last_result.image.width)
        editor.close()
        # "last" mode skips the picker
        self.app.settings.set("editor.open_mode", "last")
        from screenpilot.ui.editor_window import EditorWindow

        self.assertIsInstance(self.controller.edit_last_capture(), EditorWindow)

    def test_copy_only_capture_is_the_last_capture(self):
        overlay = self.open_selector()
        self.select(overlay, QPoint(60, 60), QPoint(200, 160))
        QTest.mouseClick(overlay.action_bar.buttons["copy"], Qt.MouseButton.LeftButton)
        self.assertTrue(wait_until(lambda: self.captured))
        self.assertIs(self.controller.last_capture(), self.captured[0][0])


class HubTests(UiTestCase):
    def test_hub_tiles_chips_and_capture(self):
        hub = self.controller.show_hub()
        QTest.qWaitForWindowExposed(hub)
        hub.resize(680, 560)
        self.assertEqual(hub.tiles["capture.region"].shortcut_text, "Ctrl+Shift+1")
        # Copy chip reflects and edits the after-capture chain.
        self.assertTrue(hub.copy_chip.isChecked())
        hub.copy_chip.click()
        self.assertNotIn("copy_image", [s["id"] for s in self.app.settings.get("workflow.after_capture")])
        hub.copy_chip.click()
        # Delay chip cycles 0 -> 3 -> 5.
        hub.delay_chip.click()
        hub.delay_chip.click()
        self.assertEqual(hub.delay, 5)
        hub.delay_chip.click()
        hub.delay_chip.click()
        self.assertEqual(hub.delay, 0)
        shot(hub, "08_hub_light_empty")
        hub.tiles["capture.window"].click()
        self.assertTrue(wait_until(lambda: self.captured))
        self.assertTrue(wait_until(lambda: hub.recent_row.count() > 0))
        self.app.settings.set("general.language", "ro")
        hub.show()
        shot(hub, "09_hub_light_recent_ro")


class EdgeBarTests(UiTestCase):
    def test_edge_bar_expands_hides_during_capture_and_returns(self):
        from screenpilot.ui.edge_bar import HANDLE, EdgeBar

        bar = EdgeBar(self.controller)
        self.controller.edge_bar = bar
        bar.start()
        QTest.qWaitForWindowExposed(bar)
        self.assertEqual(bar.height(), HANDLE.height())
        self.assertEqual(bar.y(), QGuiApplication.primaryScreen().geometry().y())
        shot(bar, "17_edge_bar_collapsed")
        bar.expand()
        self.assertTrue(wait_until(lambda: bar.height() > 40))
        QTest.qWait(250)
        shot(bar, "18_edge_bar_expanded")
        self.assertTrue(bar.buttons["record.video"].isEnabled())
        QTest.mouseClick(bar.buttons["capture.region"], Qt.MouseButton.LeftButton)
        self.assertFalse(bar.isVisible())  # never in the capture
        self.assertTrue(wait_until(lambda: self.controller.selector.active))
        QTest.keyClick(self.controller.selector.overlays[0], Qt.Key.Key_Escape)
        self.assertTrue(wait_until(lambda: bar.isVisible(), timeout=3))
        self.app.settings.set("general.edge_bar", False)
        self.assertFalse(bar.isVisible())
        self.app.settings.set("general.edge_bar", True)
        self.assertTrue(bar.isVisible())

    def test_hotkey_capture_hides_bar_first(self):
        from screenpilot.ui.edge_bar import EdgeBar

        bar = EdgeBar(self.controller)
        self.controller.edge_bar = bar
        bar.start()
        self.controller.trigger("capture.window")
        self.assertFalse(bar.isVisible())
        self.assertTrue(wait_until(lambda: self.captured))
        self.assertTrue(wait_until(lambda: bar.isVisible(), timeout=3))


class DarkThemeTests(UiTestCase):
    theme_mode = "dark"

    @classmethod
    def tearDownClass(cls):
        cls.themes.set_mode("light")

    def test_dark_hub_settings_and_selector(self):
        hub = self.controller.show_hub()
        QTest.qWaitForWindowExposed(hub)
        hub.resize(680, 520)
        shot(hub, "10_hub_dark")
        dialog = self.controller.show_settings()
        QTest.qWaitForWindowExposed(dialog)
        shot(dialog, "11_settings_dark")
        overlay = self.open_selector()
        self.select(overlay, QPoint(80, 80), QPoint(380, 260))
        shot(overlay, "12_selector_dark_theme")
        QTest.keyClick(overlay, Qt.Key.Key_Escape)

        from screenpilot.ui.edge_bar import EdgeBar

        bar = EdgeBar(self.controller)
        self.controller.edge_bar = bar
        bar.start()
        bar.expand()
        QTest.qWait(250)
        shot(bar, "19_edge_bar_dark")


class WindowsTests(UiTestCase):
    def test_icon_sheet_renders(self):
        from screenpilot.ui import icons

        cols, size, pad = 8, 32, 14
        rows = (len(icons.NAMES) + cols - 1) // cols
        sheet = QPixmap(cols * (size + pad) + pad, rows * (size + pad) + pad)
        sheet.fill(QColor("white"))
        p = QPainter(sheet)
        for i, name in enumerate(icons.NAMES):
            pix = icons.pixmap(name, "#1b1b1b", size, 1.0)
            self.assertFalse(pix.isNull())
            p.drawPixmap(pad + (i % cols) * (size + pad), pad + (i // cols) * (size + pad), pix)
        p.end()
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            sheet.save(str(Path(SHOTS) / "00_icons.png"))

    def test_tray_menu_follows_language(self):
        from screenpilot.ui.tray import TrayIcon

        tray = TrayIcon(self.controller)
        texts = [a.text() for a in tray.contextMenu().actions()]
        self.assertIn("Open ScreenPilot", texts)
        self.assertIn("Capture region\tCtrl+Shift+1", texts)
        self.app.settings.set("general.language", "ro")
        texts = [a.text() for a in tray.contextMenu().actions()]
        self.assertIn("Capturează o zonă\tCtrl+Shift+1", texts)
        self.assertIn("Ieșire", texts)
        self.assertIn("Ctrl+Shift+1", tray.toolTip())

    def test_settings_dialog_live_language_theme_and_hotkeys(self):
        from screenpilot.ui.settings_dialog import qt_sequence_to_hotkey

        dialog = self.controller.show_settings()
        QTest.qWaitForWindowExposed(dialog)
        self.assertEqual(dialog.windowTitle(), "Settings")
        shot(dialog, "13_settings_general_en")
        dialog.language.setCurrentIndex(dialog.language.findData("ro"))
        self.assertEqual(dialog.windowTitle(), "Setări")
        self.assertEqual(self.app.translator.language, "ro")
        self.controller.trigger("capture.window")
        self.assertTrue(wait_until(lambda: self.captured))
        for index, name in enumerate(("general", "capture", "modes", "after_capture", "recording", "hotkeys", "diagnostics")):
            dialog.tabs.setCurrentIndex(index)
            QApplication.processEvents()
            shot(dialog, f"14_settings_{name}_ro")
        self.assertEqual(dialog.tabs.tabText(4), "Înregistrare")
        self.assertEqual(dialog.tabs.tabText(5), "Taste rapide")
        self.assertGreater(dialog.timing_table.rowCount(), 0)

        dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("dark"))
        self.assertEqual(self.app.settings.get("general.theme"), "dark")

        dialog.hotkey_table.selectRow(0)
        action = dialog._selected_action()
        dialog._apply_hotkey(action, qt_sequence_to_hotkey(QKeySequence("Ctrl+Alt+Z")))
        self.assertEqual(str(self.app.hotkeys.binding(action)), "Ctrl+Alt+Z")
        self.assertTrue(self.app.paths.hotkeys_file.exists())
        dialog._apply_hotkey("capture.window", qt_sequence_to_hotkey(QKeySequence("Ctrl+Alt+Z")))
        self.assertIn("Ctrl+Alt+Z", dialog.conflicts.text())

        item = dialog.actions_list.item(0)
        item.setCheckState(Qt.CheckState.Unchecked)
        enabled = [s["id"] for s in self.app.settings.get("workflow.after_capture")]
        self.assertNotIn(item.data(Qt.ItemDataRole.UserRole), enabled)
        dialog.close()

    def test_qt_sequence_conversion(self):
        from screenpilot.ui.settings_dialog import qt_sequence_to_hotkey

        self.assertEqual(str(qt_sequence_to_hotkey(QKeySequence("Meta+Shift+P"))), "Shift+Win+P")
        self.assertEqual(str(qt_sequence_to_hotkey(QKeySequence("Print"))), "PrintScreen")

    def test_toggle_switch(self):
        from screenpilot.ui.widgets import ToggleSwitch

        toggle = ToggleSwitch()
        toggle.show()
        QTest.mouseClick(toggle, Qt.MouseButton.LeftButton)
        self.assertTrue(toggle.isChecked())
        toggle.setChecked(False)
        self.assertFalse(toggle.isChecked())

    def test_history_window_lists_captures(self):
        self.controller.trigger("capture.window")
        self.assertTrue(wait_until(lambda: self.app.history.entries()))
        window = self.controller.show_history()
        QTest.qWaitForWindowExposed(window)
        self.assertTrue(wait_until(lambda: window.list.count() == 1))
        shot(window, "15_history")
        window._copy_path()
        self.assertEqual(self.app.clipboard.text, self.app.history.entries()[0].path)
        window.close()

    def test_wizard_pages_and_completion_opens_hub(self):
        wizard = self.controller.show_wizard()
        QTest.qWaitForWindowExposed(wizard)
        for page in range(4):
            shot(wizard, f"16_wizard_page{page + 1}")
            if page < 3:
                wizard.next()
                QApplication.processEvents()
        wizard.accept()
        QApplication.processEvents()
        self.assertTrue(self.app.settings.get("general.first_run_completed"))
        self.assertIn("hub", self.controller._windows)


class RecordingUiTests(UiTestCase):
    def setUp(self):
        super().setUp()
        self.app.settings.set("recording.countdown", 0)
        self.app.settings.set("recording.gif_fps", 10)
        self.rec = self.controller.recording

    def wait_result(self):
        self.assertTrue(wait_until(lambda: self.rec.result_card is not None, timeout=20), self.notes)
        return self.rec.result_card

    def test_record_gif_from_selector_with_pause_zoom_and_result_card(self):
        self.controller.trigger("record.gif")
        overlay = self.controller.selector.overlays[0]
        QTest.qWaitForWindowExposed(overlay)
        shot(overlay, "40_record_selector")
        self.select(overlay, QPoint(40, 40), QPoint(439, 279))
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.rec.recording))
        self.assertTrue(self.controller.busy)
        bar, frame = self.rec.bar, self.rec.frame
        self.assertTrue(bar.isVisible() and frame.isVisible())
        QTest.qWait(300)
        shot(bar, "41_recording_bar")
        QTest.mouseClick(bar.buttons["pause"], Qt.MouseButton.LeftButton)
        self.assertTrue(wait_until(lambda: self.rec.recorder.state.value == "paused"))
        QTest.qWait(300)
        shot(bar, "42_recording_bar_paused")
        QTest.mouseClick(bar.buttons["pause"], Qt.MouseButton.LeftButton)
        self.assertTrue(wait_until(lambda: self.rec.recorder.state.value == "recording"))
        QTest.mouseClick(bar.buttons["zoom"], Qt.MouseButton.LeftButton)
        self.assertTrue(self.rec.recorder.effects.zoom_on)
        QTest.qWait(300)
        self.controller.trigger("record.gif")  # the same hotkey stops
        card = self.wait_result()
        shot(card, "43_recording_saved")
        self.assertTrue(card.result.path.exists())
        self.assertEqual(card.result.path.suffix, ".gif")
        self.assertEqual(card.result.rect.width, 400)
        self.assertNotIn("gif", card.buttons)  # already a GIF
        self.assertIsNone(self.rec.bar)
        self.assertFalse(self.controller.busy)
        self.assertFalse([n for n in self.notes if n[2]])

    def test_mp4_without_ffmpeg_falls_back_to_gif(self):
        self.app.recording.ffmpeg = lambda refresh=False: None
        self.assertTrue(self.rec.start(Rect(self.screen_rect.x, self.screen_rect.y, 320, 200), "mp4"))
        self.assertTrue(wait_until(lambda: self.rec.recording))
        self.assertTrue(self.rec.recorder.options.is_gif)
        self.assertIn(self.app.translator.t("recording.no_ffmpeg.title"), [n[0] for n in self.notes])
        self.rec.stop()
        self.assertEqual(self.wait_result().result.format, "gif")

    def test_countdown_can_be_cancelled_and_cancel_discards(self):
        self.app.settings.set("recording.countdown", 3)
        self.rec.start(Rect(self.screen_rect.x, self.screen_rect.y, 320, 200), "gif")
        self.assertTrue(self.rec.active)
        self.assertIsNotNone(self.rec.frame)
        shot(self.rec.frame, "44_recording_frame_waiting")
        self.rec.cancel()
        self.assertTrue(wait_until(lambda: not self.rec.active))
        self.assertIsNone(self.rec.frame)
        self.assertFalse(self.app.recording.busy)

        self.app.settings.set("recording.countdown", 0)
        self.rec.start(Rect(self.screen_rect.x, self.screen_rect.y, 320, 200), "gif")
        self.assertTrue(wait_until(lambda: self.rec.recording))
        QTest.qWait(200)
        self.rec.cancel()
        self.assertTrue(wait_until(lambda: not self.rec.active, timeout=10))
        self.assertIsNone(self.rec.result_card)
        self.assertEqual(list(self.app.recording.output_dir().glob("*.gif")), [])

    def test_draw_steps_and_arrow_while_recording(self):
        self.app.settings.set("recording.highlight_cursor", False)
        self.app.settings.set("recording.click_effects", False)
        rect = Rect(self.screen_rect.x, self.screen_rect.y, 400, 240)
        self.assertTrue(self.rec.start(rect, "gif"))
        self.assertTrue(wait_until(lambda: self.rec.recording))
        bar, ink = self.rec.bar, self.rec.ink
        self.assertIsNotNone(ink)
        self.assertTrue(bar.buttons["draw"].isVisible())
        QTest.mouseClick(bar.buttons["draw"], Qt.MouseButton.LeftButton)
        self.assertTrue(ink.drawing and bar.tools.isVisible())
        self.assertTrue(self.rec.recorder.ink.drawing)
        QTest.qWaitForWindowExposed(ink)
        QTest.mouseClick(ink, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
        QTest.mouseClick(ink, Qt.MouseButton.LeftButton, pos=QPoint(160, 60))
        self.assertEqual([v.item.number for v in self.rec.recorder.ink.views()], [1, 2])
        QTest.keyClick(ink, Qt.Key.Key_2)  # arrow tool
        self.assertEqual(ink.tool, "arrow")
        self.assertTrue(bar.tool_buttons["arrow"].isChecked())
        QTest.mousePress(ink, Qt.MouseButton.LeftButton, pos=QPoint(200, 150))
        QTest.mouseMove(ink, QPoint(300, 200))
        QTest.mouseRelease(ink, Qt.MouseButton.LeftButton, pos=QPoint(300, 200))
        self.assertEqual(len(self.rec.recorder.ink), 3)
        QTest.qWait(300)
        shot(bar, "46_recording_bar_drawing")
        QTest.mouseClick(bar.tool_buttons["step"], Qt.MouseButton.LeftButton)
        QTest.mouseClick(ink, Qt.MouseButton.RightButton, pos=QPoint(10, 10))  # right-click = undo
        self.assertEqual(len(self.rec.recorder.ink), 2)
        self.assertEqual(self.rec.escape(), "recording.ink")  # first Esc only puts the pen away
        self.assertFalse(ink.drawing or bar.tools.isVisible())
        QTest.qWait(200)
        self.rec.stop()
        card = self.wait_result()
        self.assertTrue(card.result.path.exists())
        self.assertIsNone(self.rec.ink)

    def test_policy_can_disable_annotations(self):
        from screenpilot.core.policy import Policy

        self.app.policy = Policy(disabled=frozenset({"record_annotations"}))
        self.assertTrue(self.rec.start(Rect(self.screen_rect.x, self.screen_rect.y, 320, 200), "gif"))
        self.assertTrue(wait_until(lambda: self.rec.recording))
        self.assertIsNone(self.rec.ink)
        self.assertFalse(self.rec.bar.buttons["draw"].isVisible())
        self.assertFalse(self.rec.toggle_draw())
        self.rec.cancel()
        self.assertTrue(wait_until(lambda: not self.rec.active, timeout=10))

    def test_video_editor_trims_annotates_and_saves_a_gif(self):
        from test_videoedit import make_gif

        src = make_gif(Path(self._tmp.name) / "clip.gif", frames=30, w=320, h=200)
        window = self.controller.open_video_editor(src)
        self.assertIsNotNone(window)
        QTest.qWaitForWindowExposed(window)
        self.assertTrue(wait_until(lambda: window.canvas._frame is not None))
        window.seek(1.0)
        window.set_tool("step")
        canvas = window.canvas
        center = canvas.target().center().toPoint()
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=center)
        self.assertEqual(len(window.project.annotations), 1)
        ann = window.project.annotations[0]
        self.assertAlmostEqual(ann.start, 1.0, delta=0.01)
        expected = min(window.canvas.duration_s, window.project.duration - 1.0)  # clipped at the end of the clip
        self.assertAlmostEqual(ann.end - ann.start, expected, delta=0.01)
        window.set_tool("blur")
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=center - QPoint(60, 40))
        QTest.mouseMove(canvas, center - QPoint(10, 10))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=center - QPoint(10, 10))
        self.assertEqual([a.kind for a in window.project.annotations], ["step", "blur"])
        # Drag the start handle to about a quarter of the video.
        tl = window.timeline
        QTest.mousePress(tl, Qt.MouseButton.LeftButton, pos=QPoint(round(tl._x(0)), 20))
        QTest.mouseMove(tl, QPoint(round(tl._x(0.75)), 20))
        QTest.mouseRelease(tl, Qt.MouseButton.LeftButton, pos=QPoint(round(tl._x(0.75)), 20))
        self.assertAlmostEqual(window.project.start, 0.75, delta=0.1)
        window.undo()
        self.assertEqual(window.project.start, 0.0)
        window.redo()
        window.seek(2.5)
        window.trim_end_here()
        self.assertAlmostEqual(window.project.output_duration, 1.75, delta=0.1)
        QTest.qWait(200)
        shot(window, "47_video_editor")
        window.export()
        self.assertTrue(wait_until(lambda: window._exported_path is not None, timeout=20), window.status.text())
        out = window._exported_path
        self.assertTrue(out.exists() and out.name == "clip_edited.gif")
        self.assertTrue(window.buttons["folder"].isVisible())
        window.show_folder()
        self.assertTrue(src.exists())
        window.close()  # exported: closes without asking

    def test_video_editor_from_the_recording_result_card(self):
        self.assertTrue(self.rec.start(Rect(self.screen_rect.x, self.screen_rect.y, 320, 200), "gif"))
        self.assertTrue(wait_until(lambda: self.rec.recording))
        QTest.qWait(400)
        self.rec.stop()
        card = self.wait_result()
        self.assertIn("edit", card.buttons)
        QTest.mouseClick(card.buttons["edit"], Qt.MouseButton.LeftButton)
        editors = [w for k, w in self.controller._windows.items() if k.startswith("video:")]
        self.assertEqual(len(editors), 1)
        editors[0].force_close()

    def test_forwarded_actions_from_devpilot_or_cli(self):
        from screenpilot.instance import InstanceServer, action_message, send

        self.assertFalse(self.controller.accept_remote(action_message("nope"))["ok"])
        server = InstanceServer(self.app.paths.data_dir, self.controller.accept_remote)
        self.assertTrue(server.start())
        try:
            self.assertEqual(send(self.app.paths.data_dir, action_message("app.show_hub")), {"ok": True})
            self.assertTrue(wait_until(lambda: "hub" in self.controller._windows))
            self.assertEqual(send(self.app.paths.data_dir, action_message("capture.region")), {"ok": True})
            self.assertTrue(wait_until(lambda: self.controller.selector.active, timeout=5))
            self.controller.selector.cancel()
            self.assertEqual(send(self.app.paths.data_dir, action_message("app.settings", page="recording")), {"ok": True})
            self.assertTrue(wait_until(lambda: "settings" in self.controller._windows))
        finally:
            server.stop()

    def test_notifications_without_a_tray_icon_use_a_toast(self):
        from screenpilot.ui.tray import TrayIcon

        tray = TrayIcon(self.controller)  # never shown: DevPilot owns the tray
        clicked = []
        self.controller.notify_action.emit("Title", "Body", False, lambda: clicked.append(1))
        QApplication.processEvents()
        toast = tray.last_toast
        self.assertTrue(toast.isVisible())
        shot(toast, "48_toast")
        QTest.mouseClick(toast, Qt.MouseButton.LeftButton)
        self.assertEqual(clicked, [1])

    def test_edge_bar_buttons_can_be_customised(self):
        from screenpilot.ui.edge_bar import EdgeBar

        bar = EdgeBar(self.controller)
        self.assertIn("capture.region", bar.buttons)
        self.assertNotIn("ocr.region", bar.buttons)
        editor = self.controller.customize_edge_bar()
        QTest.qWaitForWindowExposed(editor)
        ids = [editor.available.item(i).data(Qt.ItemDataRole.UserRole) for i in range(editor.available.count())]
        editor.available.setCurrentRow(ids.index("ocr.region"))
        editor.chosen.setCurrentRow(0)
        editor.add()  # after Region
        self.assertEqual(editor.chosen_ids()[:2], ["capture.region", "ocr.region"])
        editor.chosen.setCurrentRow(editor.chosen_ids().index("capture.window"))
        editor.remove()
        shot(editor, "49_edge_bar_editor")
        editor.save()
        self.assertEqual(self.app.settings.get("general.edge_bar_buttons")[:2], ["capture.region", "ocr.region"])
        QApplication.processEvents()
        self.assertIn("ocr.region", bar.buttons)
        self.assertNotIn("capture.window", bar.buttons)
        bar.expand()
        QTest.qWait(250)
        shot(bar, "50_edge_bar_custom")
        self.app.translator.set_language("ro")  # bindings of the rebuilt buttons follow the language
        self.app.translator.set_language("en")
        bar.close()

    def test_escape_closes_windows_opened_by_a_capture(self):
        result = self.app.capture.capture_region(Rect(self.screen_rect.x, self.screen_rect.y, 200, 120))
        editor = self.controller.open_editor(result)
        QTest.qWaitForWindowExposed(editor)
        QTest.keyClick(editor, Qt.Key.Key_Escape)  # nothing drawn: closes at once
        self.assertTrue(wait_until(lambda: not editor.isVisible()))
        steps = self.controller.open_steps(result)
        QTest.qWaitForWindowExposed(steps)
        QTest.keyClick(steps, Qt.Key.Key_Escape)
        self.assertTrue(wait_until(lambda: not steps.isVisible()))
        hub = self.controller.show_hub()
        QTest.qWaitForWindowExposed(hub)
        QTest.keyClick(hub, Qt.Key.Key_Escape)
        self.assertTrue(wait_until(lambda: not hub.isVisible()))
        self.app.settings.set("capture.show_preview", True)
        preview = self.controller.show_preview(result, None)
        self.assertTrue(preview.isVisible())
        self.assertEqual(self.controller.escape_pressed(), "preview")

    def test_version_is_visible(self):
        from screenpilot import __version__

        hub = self.controller.show_hub()
        self.assertIn(__version__, hub.version_label.text())
        dialog = self.controller.show_settings()
        self.assertIn(__version__, dialog.version_label.text())
        dialog.close()
        hub.close()

    def test_policy_blocks_recording_everywhere(self):
        from screenpilot.core.policy import Policy

        self.app.policy = Policy(disabled=frozenset({"recording"}))
        self.app.recording.policy = self.app.policy
        self.controller.trigger("record.video")
        self.assertFalse(self.controller.selector.active)
        self.assertIn(self.app.translator.t("policy.feature_disabled"), [n[1] for n in self.notes])
        self.assertFalse(self.rec.start(Rect(self.screen_rect.x, self.screen_rect.y, 320, 200), "gif"))

    def test_hub_and_settings_expose_recording(self):
        hub = self.controller.show_hub()
        self.assertIn("record.video", hub.tiles)
        self.assertIn("record.gif", hub.tiles)
        shot(hub, "45_hub_with_video")
        hub.close()
        dialog = self.controller.show_settings()
        dialog.tabs.setCurrentIndex(4)
        QApplication.processEvents()
        shot(dialog, "46_settings_recording_en")
        self.assertIn("FFmpeg", dialog.ffmpeg_status.text())
        dialog.close()



class SelectorOptionTests(UiTestCase):
    def test_without_action_bar_release_captures_immediately(self):
        self.app.settings.set("selector.action_bar", False)
        overlay = self.open_selector()
        self.select(overlay, QPoint(50, 40), QPoint(249, 139))
        self.assertTrue(wait_until(lambda: self.captured))
        result, path, _ = self.captured[0]
        self.assertEqual((result.region.width, result.region.height), (200, 100))
        self.assertIsNotNone(path)  # Region still runs the after-capture actions
        self.assertFalse(self.controller.selector.active)

    def test_region_copy_only_setting(self):
        self.app.settings.set("capture.region_action", "copy")
        overlay = self.open_selector()
        self.select(overlay, QPoint(60, 60), QPoint(200, 160))
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.captured))
        result, path, _ = self.captured[0]
        self.assertIsNone(path)
        self.assertIs(self.app.clipboard.image, result.image)
        self.assertEqual(self.app.history.entries(), [])

    def test_settings_pages_show_the_new_options(self):
        dialog = self.controller.show_settings("capture")
        QApplication.processEvents()
        self.assertEqual(dialog.nav.currentRow(), dialog.page_index["capture"])
        shot(dialog, "52_settings_capture_new_options")
        dialog.show_page("recording")
        self.assertEqual(dialog.nav.currentRow(), dialog.page_index["recording"])
        dialog.close()


class EscapeTests(UiTestCase):
    def test_escape_cancels_selector_even_without_focus(self):
        self.open_selector()
        self.assertEqual(self.controller.escape_pressed(), "selector")
        self.assertFalse(self.controller.selector.active)
        self.assertEqual(self.controller.escape_pressed(), "ignored")  # same press, already handled

    def test_escape_cancels_countdown_and_stops_interval(self):
        calls = []
        self.controller.capture_delayed(5, then=lambda: calls.append(1))
        self.assertEqual(self.controller.escape_pressed(), "countdown")
        QTest.qWait(50)
        self.assertIsNone(self.controller._countdown)
        self.controller._last_esc_handled = 0.0
        self.controller.start_interval(Rect(self.screen_rect.x, self.screen_rect.y, 50, 40), seconds=60, count=0)
        self.assertEqual(self.controller.escape_pressed(), "interval")
        self.assertIsNone(self.controller.interval)
        self.assertEqual(calls, [])


class RecordingEscapeTests(UiTestCase):
    def setUp(self):
        super().setUp()
        self.app.settings.set("recording.countdown", 0)
        self.app.settings.set("recording.gif_fps", 10)
        self.rec = self.controller.recording
        self.answers = []
        self.controller.ask_continue = lambda *a, **k: self.answers.pop(0)

    def _start(self):
        self.assertTrue(self.rec.start(Rect(self.screen_rect.x, self.screen_rect.y, 320, 200), "gif"))
        self.assertTrue(wait_until(lambda: self.rec.recording))

    def test_escape_pauses_then_continue_resumes(self):
        self._start()
        self.answers.append("continue")
        self.assertEqual(self.controller.escape_pressed(), "recording.continue")
        self.assertTrue(wait_until(lambda: self.rec.recorder.state.value == "recording"))
        self.answers.append("stop")
        self.controller._last_esc_handled = 0.0
        self.assertEqual(self.controller.escape_pressed(), "recording.stop")
        self.assertTrue(wait_until(lambda: self.rec.result_card is not None, timeout=20), self.notes)
        self.assertTrue(self.rec.result_card.result.path.exists())

    def test_escape_discard(self):
        self._start()
        self.answers.append("cancel")
        self.assertEqual(self.controller.escape_pressed(), "recording.cancel")
        self.assertTrue(wait_until(lambda: not self.rec.active, timeout=10))
        self.assertIsNone(self.rec.result_card)

    def test_escape_can_be_turned_off(self):
        self.app.settings.set("recording.esc_pauses", False)
        self._start()
        self.assertEqual(self.controller.escape_pressed(), "recording.ignored")
        self.assertEqual(self.rec.recorder.state.value, "recording")
        self.rec.stop()
        self.assertTrue(wait_until(lambda: self.rec.result_card is not None, timeout=20))


class CopyAndCloseTests(UiTestCase):
    def test_editor_copy_and_close_copies_without_saving(self):
        result = self.app.capture.capture_window()
        editor = self.controller.open_editor(result)
        QTest.qWaitForWindowExposed(editor)
        self.assertIn("copy_close", editor.buttons)
        self.assertIn("Ctrl+Shift+Enter", editor.buttons["copy_close"].toolTip())
        shot(editor, "53_editor_copy_close")
        self.app.clipboard.image = None
        QTest.mouseClick(editor.buttons["copy_close"], Qt.MouseButton.LeftButton)
        self.assertIsNotNone(self.app.clipboard.image)
        self.assertTrue(wait_until(lambda: not any(k.startswith("editor-") for k in self.controller._windows)))
        QTest.qWait(100)
        self.assertEqual(self.captured, [])  # Done (save) was not run
        self.assertEqual(self.app.history.entries(), [])

    def test_step_editor_copy_and_close(self):
        result = self.app.capture.capture_window()
        steps = self.controller.open_steps(result)
        QTest.qWaitForWindowExposed(steps)
        self.app.clipboard.image = None
        steps.copy_and_close()
        self.assertIsNotNone(self.app.clipboard.image)
        self.assertTrue(wait_until(lambda: not any(k.startswith("steps-") for k in self.controller._windows)))


if __name__ == "__main__":
    unittest.main()
