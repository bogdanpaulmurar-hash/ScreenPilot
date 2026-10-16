"""Qt UI tests (offscreen). Skipped when PySide6 is not installed.

Set SCREENPILOT_UI_SHOTS=<dir> to save screenshots of every window (CI uploads
them so the UI can be reviewed without a Windows machine).
"""

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
        overlay = self.open_selector()
        b = self.window.bounds
        inside = QPoint(b.x - self.screen_rect.x + b.width // 2, b.y - self.screen_rect.y + 60)
        QTest.mouseMove(overlay, inside)
        shot(overlay, "03_selector_window_snap")
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, inside)
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.captured))
        self.assertEqual(self.captured[0][0].region, self.window.bounds)

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
        self.assertFalse(preview.buttons["edit"].isEnabled())
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

    def test_not_available_actions_notify(self):
        self.controller.trigger("ocr.region")
        self.assertIn("future", self.notes[0][1])


class HubTests(UiTestCase):
    def test_hub_tiles_chips_and_capture(self):
        hub = self.controller.show_hub()
        QTest.qWaitForWindowExposed(hub)
        hub.resize(560, 560)
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


class DarkThemeTests(UiTestCase):
    theme_mode = "dark"

    @classmethod
    def tearDownClass(cls):
        cls.themes.set_mode("light")

    def test_dark_hub_settings_and_selector(self):
        hub = self.controller.show_hub()
        QTest.qWaitForWindowExposed(hub)
        hub.resize(560, 520)
        shot(hub, "10_hub_dark")
        dialog = self.controller.show_settings()
        QTest.qWaitForWindowExposed(dialog)
        shot(dialog, "11_settings_dark")
        overlay = self.open_selector()
        self.select(overlay, QPoint(80, 80), QPoint(380, 260))
        shot(overlay, "12_selector_dark_theme")
        QTest.keyClick(overlay, Qt.Key.Key_Escape)


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
        for index, name in enumerate(("general", "capture", "after_capture", "hotkeys")):
            dialog.tabs.setCurrentIndex(index)
            QApplication.processEvents()
            shot(dialog, f"14_settings_{name}_ro")
        self.assertEqual(dialog.tabs.tabText(3), "Taste rapide")

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


if __name__ == "__main__":
    unittest.main()
